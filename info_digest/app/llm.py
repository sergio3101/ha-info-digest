"""Слой языковых моделей: провайдеры и цепочка с автоматическим переключением (FR-LLM).

Задачи вызывают цепочку, а не конкретную модель. Если провайдер отказал
(нет денег или доступа, лимиты, ошибки сервера, тайм-аут, мусор вместо JSON),
он уходит на «остывание» и запрос идёт к следующему. Раз в 15 минут
сервис снова пробует основной провайдер.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import aiohttp

log = logging.getLogger(__name__)

COOLDOWN_S = 15 * 60
RATE_LIMIT_COOLDOWN_S = 2 * 60
TIMEOUT_S = 60


class LLMError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # auth, payment, rate_limit, server, timeout, bad_request, network


@dataclass
class Completion:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Provider(Protocol):
    name: str

    async def complete(self, system: str, user: str, max_tokens: int) -> Completion: ...

    async def close(self) -> None: ...


class OpenAICompatProvider:
    """Любой OpenAI-совместимый /chat/completions: Yandex AI Studio, OpenAI, OpenRouter, vLLM и т. п."""

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        *,
        auth_scheme: str = "Bearer",
        extra_headers: dict[str, str] | None = None,
        temperature: float = 0.1,
        proxy: str | None = None,
    ) -> None:
        self.name = name
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.temperature = temperature
        self.proxy = proxy
        self.headers = {"Authorization": f"{auth_scheme} {api_key}", "Content-Type": "application/json"}
        self.headers.update(extra_headers or {})
        self._session: aiohttp.ClientSession | None = None

    async def complete(self, system: str, user: str, max_tokens: int) -> Completion:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S))
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.temperature,
            "max_tokens": max_tokens,
        }
        try:
            async with self._session.post(self.url, json=body, headers=self.headers, proxy=self.proxy) as r:
                text = await r.text()
                if r.status >= 400:
                    raise LLMError(_kind_for_status(r.status, text), f"HTTP {r.status}: {text[:300]}")
                data = await r.json(content_type=None)
        except TimeoutError as e:
            raise LLMError("timeout", f"нет ответа за {TIMEOUT_S} с") from e
        except aiohttp.ClientError as e:
            raise LLMError("network", f"сетевая ошибка: {e}") from e
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("bad_request", f"неожиданный ответ: {str(data)[:300]}") from e
        usage = data.get("usage") or {}
        return Completion(
            content=content,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


def _kind_for_status(status: int, body: str) -> str:
    low = body.lower()
    if status in (401, 403):
        return "auth"
    if status == 402 or "balance" in low or "billing" in low or "quota" in low:
        return "payment"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server"
    return "bad_request"


def yandex_provider(api_key: str, folder_id: str, model: str) -> OpenAICompatProvider:
    """Yandex AI Studio через OpenAI-совместимый API."""
    return OpenAICompatProvider(
        "yandex",
        "https://ai.api.cloud.yandex.net/v1",
        api_key,
        f"gpt://{folder_id}/{model}",
        auth_scheme="Api-Key",
        extra_headers={"OpenAI-Project": folder_id, "x-folder-id": folder_id},
    )


@dataclass
class ProviderState:
    cooldown_until: float = 0.0
    last_error: str | None = None
    last_error_at: str | None = None
    last_ok_at: str | None = None


class LLMChain:
    def __init__(
        self,
        providers: list[Provider],
        *,
        usage_get: Callable[[str], int] | None = None,
        usage_add: Callable[[str, int], Any] | None = None,
        monthly_token_limit: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.providers = providers
        self.state = {p.name: ProviderState() for p in providers}
        self.monthly_token_limit = monthly_token_limit
        self._usage_get = usage_get or (lambda key: 0)
        self._usage_add = usage_add or (lambda key, n: None)
        self._clock = clock
        self.active: str | None = providers[0].name if providers else None

    @staticmethod
    def usage_key(name: str, now: datetime | None = None) -> str:
        return f"llm_tokens:{(now or datetime.now(UTC)).strftime('%Y-%m')}:{name}"

    def usage(self, name: str) -> int:
        return self._usage_get(self.usage_key(name))

    def _available(self, p: Provider) -> bool:
        if self._clock() < self.state[p.name].cooldown_until:
            return False
        return not (self.monthly_token_limit and self.usage(p.name) >= self.monthly_token_limit)

    def _fail(self, p: Provider, message: str, cooldown: float) -> None:
        st = self.state[p.name]
        st.cooldown_until = self._clock() + cooldown
        st.last_error = message
        st.last_error_at = datetime.now(UTC).isoformat(timespec="seconds")
        log.warning("Модель %s недоступна: %s", p.name, message)

    async def run(self, system: str, user: str, parse: Callable[[str], Any], max_tokens: int = 4000):
        """Возвращает (результат parse, имя провайдера) или (None, None), если не сработал никто."""
        for p in self.providers:
            if not self._available(p):
                continue
            for attempt in (1, 2):
                try:
                    comp = await p.complete(system, user, max_tokens)
                except LLMError as e:
                    cd = RATE_LIMIT_COOLDOWN_S if e.kind == "rate_limit" else COOLDOWN_S
                    self._fail(p, f"{e.kind}: {e}", cd)
                    break
                self._usage_add(self.usage_key(p.name), comp.total_tokens)
                try:
                    result = parse(comp.content)
                except ValueError as e:
                    if attempt == 2:
                        self._fail(p, f"невалидный ответ дважды подряд: {e}", COOLDOWN_S)
                    continue
                st = self.state[p.name]
                st.last_ok_at = datetime.now(UTC).isoformat(timespec="seconds")
                if self.active != p.name:
                    log.info("Разметка переключилась на модель %s", p.name)
                    self.active = p.name
                return result, p.name
        if self.providers and self.active is not None:
            log.warning("Ни одна модель не доступна — разметка только правилами")
            self.active = None
        return None, None

    async def close(self) -> None:
        for p in self.providers:
            await p.close()
