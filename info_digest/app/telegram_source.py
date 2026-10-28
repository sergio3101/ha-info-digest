"""Сборщик сообщений из групп Telegram через Telethon (MTProto).

Работает от отдельного аккаунта, который уже состоит в группах. Сборщик только
читает: ничего не отправляет, не ставит реакций и сам в группы не вступает.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telethon import TelegramClient, events, utils
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from . import __version__
from .db import Database, Message
from .util import Options, author_hash, parse_proxy

log = logging.getLogger(__name__)

GAP_FILL_INTERVAL = timedelta(minutes=10)


class State:
    NOT_CONFIGURED = "not_configured"
    CONNECTING = "connecting"
    NEED_LOGIN = "need_login"
    CODE_SENT = "code_sent"
    NEED_PASSWORD = "need_password"
    RUNNING = "running"
    ERROR = "error"


class TelegramCollector:
    def __init__(self, opts: Options, db: Database, data_dir: Path, salt: str) -> None:
        self.opts = opts
        self.db = db
        self.salt = salt
        self.session_path = data_dir / "telegram"
        self.client: TelegramClient | None = None
        self.state = State.NOT_CONFIGURED if not opts.telegram_configured else State.CONNECTING
        self.error: str | None = None
        self.sources: dict[int, int] = {}  # peer_id -> source_id
        self.source_errors: dict[str, str] = {}
        self._phone_code_hash: str | None = None
        self._tasks: list[asyncio.Task] = []

    # --- жизненный цикл ------------------------------------------------

    async def start(self) -> None:
        if not self.opts.telegram_configured:
            log.warning("Telegram не настроен: заполните api_id, api_hash и номер в настройках дополнения")
            return
        try:
            self.client = TelegramClient(
                str(self.session_path),
                int(self.opts.telegram_api_id),
                self.opts.telegram_api_hash,
                proxy=parse_proxy(self.opts.telegram_proxy),
                device_model="Info Digest",
                system_version="Home Assistant add-on",
                app_version=__version__,
                flood_sleep_threshold=120,
            )
            await self.client.connect()
            if await self.client.is_user_authorized():
                await self._start_collection()
            else:
                self.state = State.NEED_LOGIN
                log.info("Нужен вход в Telegram: откройте страницу дополнения в панели HA")
        except Exception as e:  # noqa: BLE001 — показываем любую ошибку на странице статуса
            self._fail(f"Не удалось подключиться к Telegram: {e}")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self.client:
            await self.client.disconnect()

    def _fail(self, message: str) -> None:
        self.state = State.ERROR
        self.error = message
        log.error(message)

    # --- вход ----------------------------------------------------------

    async def send_code(self) -> None:
        assert self.client
        try:
            sent = await self.client.send_code_request(self.opts.telegram_phone)
        except FloodWaitError as e:
            self.error = f"Telegram просит подождать {e.seconds} с перед новым кодом"
            return
        self._phone_code_hash = sent.phone_code_hash
        self.state = State.CODE_SENT
        self.error = None
        log.info("Код входа отправлен в Telegram")

    async def submit_code(self, code: str) -> None:
        assert self.client
        try:
            await self.client.sign_in(
                phone=self.opts.telegram_phone, code=code.strip(), phone_code_hash=self._phone_code_hash
            )
        except SessionPasswordNeededError:
            self.state = State.NEED_PASSWORD
            self.error = None
            return
        except PhoneCodeInvalidError:
            self.error = "Неверный код, попробуйте ещё раз"
            return
        except PhoneCodeExpiredError:
            self.state = State.NEED_LOGIN
            self.error = "Код устарел, запросите новый"
            return
        self.error = None
        await self._start_collection()

    async def submit_password(self, password: str) -> None:
        assert self.client
        try:
            await self.client.sign_in(password=password)
        except PasswordHashInvalidError:
            self.error = "Неверный облачный пароль"
            return
        self.error = None
        await self._start_collection()

    async def log_out(self) -> None:
        """Завершает сессию сборщика в Telegram и возвращает страницу входа."""
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        if self.client:
            self.client.remove_event_handler(self._on_message)
            await self.client.log_out()
        self.sources.clear()
        self.client = None
        log.info("Сессия Telegram завершена")
        await self.start()

    # --- сбор ----------------------------------------------------------

    async def _start_collection(self) -> None:
        assert self.client
        me = await self.client.get_me()
        log.info("Вход в Telegram выполнен (id аккаунта %s)", me.id)

        entities = []
        for ref in self.opts.telegram_chats:
            try:
                entity = await self.client.get_entity(ref)
            except Exception as e:  # noqa: BLE001
                self.source_errors[ref] = f"Группа не найдена или аккаунт в ней не состоит: {e}"
                log.error("Источник %s: %s", ref, self.source_errors[ref])
                continue
            peer_id = utils.get_peer_id(entity)
            title = getattr(entity, "title", None) or ref
            source_id = self.db.ensure_source("telegram", ref, title, peer_id)
            self.sources[peer_id] = source_id
            self.source_errors.pop(ref, None)
            entities.append(entity)
            await self._backfill(entity, source_id)

        if not entities:
            self._fail("Ни одна группа из настроек не доступна аккаунту-сборщику")
            return

        self.client.add_event_handler(self._on_message, events.NewMessage(chats=entities))
        self.client.add_event_handler(self._on_message, events.MessageEdited(chats=entities))
        self._tasks.append(asyncio.create_task(self._gap_filler(entities)))
        self.state = State.RUNNING
        log.info("Сбор запущен: %d групп(ы)", len(entities))

    async def _backfill(self, entity, source_id: int) -> None:
        """Догружает пропущенное. Если группа новая — историю за initial_backfill_hours."""
        assert self.client
        last_id = self.db.last_msg_id(source_id)
        saved = 0
        if last_id is None:
            cutoff = datetime.now(UTC) - timedelta(hours=self.opts.initial_backfill_hours)
            async for msg in self.client.iter_messages(entity):
                if msg.date < cutoff:
                    break
                saved += self._save(source_id, msg)
        else:
            async for msg in self.client.iter_messages(entity, min_id=last_id, reverse=True):
                saved += self._save(source_id, msg)
        if saved:
            log.info("Источник %s: догружено %d сообщений", source_id, saved)

    async def _gap_filler(self, entities) -> None:
        """Раз в 10 минут добирает сообщения, которые могли потеряться при обрыве связи."""
        while True:
            await asyncio.sleep(GAP_FILL_INTERVAL.total_seconds())
            for entity in entities:
                source_id = self.sources.get(utils.get_peer_id(entity))
                if source_id is None:
                    continue
                try:
                    await self._backfill(entity, source_id)
                except Exception as e:  # noqa: BLE001
                    log.warning("Догрузка источника %s не удалась: %s", source_id, e)

    async def _on_message(self, event) -> None:
        source_id = self.sources.get(event.chat_id)
        if source_id is not None:
            self._save(source_id, event.message)

    def _save(self, source_id: int, msg) -> int:
        """Сохраняет одно сообщение; служебные и пустые пропускает. Возвращает 1, если запись новая."""
        if getattr(msg, "action", None) is not None:
            return 0
        text = msg.message or ""
        has_media = msg.media is not None
        if not text and not has_media:
            return 0
        reply_to = msg.reply_to.reply_to_msg_id if msg.reply_to else None
        is_new = self.db.upsert_message(
            Message(
                source_id=source_id,
                msg_id=msg.id,
                date=msg.date,
                text=text,
                has_media=has_media,
                reply_to=reply_to,
                author_hash=author_hash(self.salt, msg.sender_id),
                is_forward=msg.fwd_from is not None,
                edited_at=msg.edit_date,
            )
        )
        return int(is_new)
