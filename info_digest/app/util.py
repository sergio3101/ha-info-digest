"""Вспомогательные функции без зависимости от Telethon (их удобно тестировать)."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

OPTIONS_PATH = Path("/data/options.json")


@dataclass
class Options:
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telegram_phone: str | None = None
    telegram_chats: list[str] = field(default_factory=list)
    telegram_proxy: str | None = None
    retention_days: int = 30
    initial_backfill_hours: int = 24
    yandex_folder_id: str | None = None
    yandex_api_key: str | None = None
    yandex_model: str = "aliceai-llm-flash"
    llm_mode: str = "candidates"
    llm_monthly_token_limit: int = 5_000_000
    event_window_hours: int = 3
    log_level: str = "info"

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_api_id and self.telegram_api_hash and self.telegram_phone)

    @property
    def yandex_configured(self) -> bool:
        return bool(self.yandex_folder_id and self.yandex_api_key)


def load_options(path: Path = OPTIONS_PATH) -> Options:
    """Читает настройки, которые Supervisor кладёт в /data/options.json."""
    if not path.exists():
        return Options()
    raw = json.loads(path.read_text(encoding="utf-8"))
    known = Options.__dataclass_fields__.keys()
    # Ключи часто вставляют с пробелами по краям — убираем; пустые поля считаем незаполненными.
    cleaned = {k: v.strip() if isinstance(v, str) else v for k, v in raw.items() if k in known}
    opts = Options(**{k: v for k, v in cleaned.items() if v not in (None, "")})
    # api_id в форме HA — строка (чтобы поле было видно пустым), а Telethon нужен int.
    api_id = str(opts.telegram_api_id or "").strip()
    opts.telegram_api_id = int(api_id) if api_id.isdigit() else None
    opts.telegram_chats = [c for c in (normalize_chat_ref(c) for c in opts.telegram_chats) if c]
    return opts


_TME = re.compile(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/", re.IGNORECASE)


def normalize_chat_ref(ref: str) -> str:
    """'https://t.me/chatsudaka', '@chatsudaka', 'chatsudaka' → 'chatsudaka'.

    Ссылки-приглашения (t.me/+..., t.me/joinchat/...) возвращаются как есть:
    по ним можно найти группу, только если аккаунт уже в ней состоит.
    """
    ref = (ref or "").strip()
    if not ref:
        return ""
    if _TME.match(ref):
        rest = _TME.sub("", ref).strip("/")
        if rest.startswith("+") or rest.lower().startswith("joinchat/"):
            return ref
        ref = rest.split("/")[0].split("?")[0]
    return ref.lstrip("@")


def parse_proxy(url: str | None) -> dict | None:
    """'socks5://user:pass@host:1080' → словарь прокси в формате Telethon (python-socks)."""
    if not url:
        return None
    p = urlparse(url.strip())
    if p.scheme not in ("socks5", "socks4", "http") or not p.hostname or not p.port:
        raise ValueError(f"Неверный адрес прокси: ожидается socks5://хост:порт, получено {url!r}")
    proxy = {"proxy_type": p.scheme, "addr": p.hostname, "port": p.port, "rdns": True}
    if p.username:
        proxy["username"] = p.username
    if p.password:
        proxy["password"] = p.password
    return proxy


def load_or_create_salt(path: Path) -> str:
    """Соль для обезличивания авторов; создаётся один раз и хранится в данных дополнения."""
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    salt = secrets.token_hex(16)
    path.write_text(salt, encoding="utf-8")
    path.chmod(0o600)
    return salt


def author_hash(salt: str, sender_id: int | None) -> str | None:
    """Обезличенный идентификатор автора: позволяет считать независимых авторов,
    но не позволяет узнать, кто это."""
    if sender_id is None:
        return None
    return hashlib.sha256(f"{salt}:{sender_id}".encode()).hexdigest()[:16]
