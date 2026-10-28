"""Страница дополнения в панели HA (через Ingress): статус сбора и вход в Telegram.

Ingress пропускает к странице только пользователей, вошедших в HA, поэтому
отдельной авторизации здесь нет. Порт наружу не публикуется.
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime

from aiohttp import web

from . import __version__
from .db import Database
from .telegram_source import State, TelegramCollector

log = logging.getLogger(__name__)

STATE_TEXT = {
    State.NOT_CONFIGURED: "Не настроено: заполните api_id, api_hash и номер во вкладке «Конфигурация»",
    State.CONNECTING: "Подключение к Telegram…",
    State.NEED_LOGIN: "Нужен вход в Telegram",
    State.CODE_SENT: "Код отправлен в Telegram",
    State.NEED_PASSWORD: "Нужен облачный пароль",
    State.RUNNING: "Сбор идёт",
    State.ERROR: "Ошибка",
}

CSS = """
:root { --bg:#fafafa; --fg:#1c1c1c; --muted:#6b6b6b; --card:#fff; --line:#e3e3e3; --accent:#03a9f4; --bad:#c62828; --ok:#2e7d32; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#111; --fg:#e8e8e8; --muted:#9a9a9a; --card:#1c1c1c; --line:#333; --accent:#4fc3f7; --bad:#ef5350; --ok:#66bb6a; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; }
main { max-width:760px; margin:0 auto; }
h1 { font-size:20px; margin:0 0 4px; }
.muted { color:var(--muted); font-size:13px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:16px; margin:16px 0; }
.state { font-weight:600; }
.state.running { color:var(--ok); }
.state.error { color:var(--bad); }
.err { color:var(--bad); margin-top:8px; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-weight:500; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
form { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }
input { padding:8px 10px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg); font-size:15px; }
button { padding:8px 14px; border:0; border-radius:6px; background:var(--accent); color:#fff; font-size:15px; cursor:pointer; }
button.secondary { background:transparent; color:var(--muted); border:1px solid var(--line); }
"""


def _fmt_time(value: str | None) -> str:
    if not value:
        return "—"
    dt = datetime.fromisoformat(value).astimezone()
    return dt.strftime("%d.%m %H:%M")


def _login_block(c: TelegramCollector) -> str:
    phone = html.escape(c.opts.telegram_phone or "")
    if c.state == State.NEED_LOGIN:
        return f"""
        <p>Аккаунт-сборщик: <b>{phone}</b>. Telegram пришлёт код в приложение на этом аккаунте.</p>
        <form method="post" action="login/send_code"><button>Получить код</button></form>"""
    if c.state == State.CODE_SENT:
        return """
        <form method="post" action="login/code">
          <input name="code" inputmode="numeric" autocomplete="one-time-code" placeholder="Код из Telegram" required>
          <button>Войти</button>
        </form>
        <form method="post" action="login/send_code"><button class="secondary">Отправить код ещё раз</button></form>"""
    if c.state == State.NEED_PASSWORD:
        return """
        <form method="post" action="login/password">
          <input name="password" type="password" autocomplete="current-password" placeholder="Облачный пароль" required>
          <button>Войти</button>
        </form>"""
    if c.state == State.RUNNING:
        return """
        <form method="post" action="logout"
              onsubmit="return confirm('Завершить сессию сборщика в Telegram? Для продолжения сбора понадобится войти заново.')">
          <button class="secondary">Выйти из Telegram</button>
        </form>"""
    return ""


def render_page(c: TelegramCollector, db: Database) -> str:
    state_cls = {State.RUNNING: "running", State.ERROR: "error"}.get(c.state, "")
    err = f'<div class="err">{html.escape(c.error)}</div>' if c.error else ""

    rows = []
    for s in db.stats():
        rows.append(
            f"<tr><td>{html.escape(s.title or s.ref)}</td><td>{html.escape(s.kind)}</td>"
            f'<td class="num">{s.last_24h}</td><td class="num">{s.total}</td>'
            f"<td>{_fmt_time(s.last_message_at)}</td></tr>"
        )
    for ref, msg in c.source_errors.items():
        rows.append(f'<tr><td>{html.escape(ref)}</td><td colspan="4" class="err">{html.escape(msg)}</td></tr>')
    table = (
        "<table><tr><th>Источник</th><th>Тип</th><th>За 24 ч</th><th>Всего</th><th>Последнее</th></tr>"
        + "".join(rows)
        + "</table>"
        if rows
        else '<p class="muted">Источников пока нет.</p>'
    )

    now = datetime.now(UTC).astimezone().strftime("%d.%m.%Y %H:%M")
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Info Digest</title><style>{CSS}</style></head>
<body><main>
  <h1>Info Digest</h1>
  <div class="muted">Версия {__version__} · обновлено {now}</div>
  <section class="card">
    <div>Telegram: <span class="state {state_cls}">{html.escape(STATE_TEXT.get(c.state, c.state))}</span></div>
    {err}
    {_login_block(c)}
  </section>
  <section class="card">{table}</section>
</main></body></html>"""


def build_app(collector: TelegramCollector, db: Database) -> web.Application:
    routes = web.RouteTableDef()

    def back(request: web.Request) -> web.HTTPSeeOther:
        return web.HTTPSeeOther(request.headers.get("X-Ingress-Path", "") + "/")

    @routes.get("/")
    async def index(request: web.Request) -> web.Response:
        return web.Response(text=render_page(collector, db), content_type="text/html")

    @routes.post("/login/send_code")
    async def send_code(request: web.Request):
        if collector.client and collector.state in (State.NEED_LOGIN, State.CODE_SENT):
            await collector.send_code()
        raise back(request)

    @routes.post("/login/code")
    async def code(request: web.Request):
        form = await request.post()
        if collector.state == State.CODE_SENT:
            await collector.submit_code(str(form.get("code", "")))
        raise back(request)

    @routes.post("/login/password")
    async def password(request: web.Request):
        form = await request.post()
        if collector.state == State.NEED_PASSWORD:
            await collector.submit_password(str(form.get("password", "")))
        raise back(request)

    @routes.post("/logout")
    async def logout(request: web.Request):
        if collector.state == State.RUNNING:
            await collector.log_out()
        raise back(request)

    @routes.get("/api/health")
    async def health(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "version": __version__,
                "telegram": collector.state,
                "error": collector.error,
                "sources": [
                    {
                        "ref": s.ref,
                        "title": s.title,
                        "kind": s.kind,
                        "last_24h": s.last_24h,
                        "total": s.total,
                        "last_message_at": s.last_message_at,
                    }
                    for s in db.stats()
                ],
            }
        )

    app = web.Application()
    app.add_routes(routes)
    return app
