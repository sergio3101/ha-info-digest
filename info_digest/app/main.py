"""Точка входа дополнения."""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from aiohttp import web

from . import __version__
from .db import Database
from .telegram_source import TelegramCollector
from .util import load_options, load_or_create_salt
from .web import build_app

DATA_DIR = Path("/data")
WEB_PORT = 8099
CLEANUP_INTERVAL_S = 24 * 3600

log = logging.getLogger("info_digest")


async def cleanup_loop(db: Database, retention_days: int) -> None:
    while True:
        removed = db.cleanup(retention_days)
        if removed:
            log.info("Удалено %d сообщений старше %d дней", removed, retention_days)
        await asyncio.sleep(CLEANUP_INTERVAL_S)


async def run() -> None:
    opts = load_options()
    logging.basicConfig(
        level=getattr(logging, opts.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Telethon на уровне DEBUG слишком многословен.
    logging.getLogger("telethon").setLevel(logging.WARNING)
    log.info("Info Digest %s запускается", __version__)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = Database(DATA_DIR / "info_digest.db")
    salt = load_or_create_salt(DATA_DIR / "salt")
    collector = TelegramCollector(opts, db, DATA_DIR, salt)

    runner = web.AppRunner(build_app(collector, db), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
    log.info("Страница дополнения доступна через панель HA")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    cleanup_task = asyncio.create_task(cleanup_loop(db, opts.retention_days))
    await collector.start()
    await stop.wait()

    log.info("Остановка")
    cleanup_task.cancel()
    await collector.stop()
    await runner.cleanup()
    db.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
