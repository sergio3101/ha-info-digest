import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.db import Database, Message
from app.ha import Publisher, build_states
from app.intents import INTENTS_FILE, build_intents, clean_phrase, render_yaml, say_template
from app.labeler import Labeler
from app.llm import LLMChain
from app.taxonomy import DEFAULT_PHRASES, DEFAULT_TAXONOMY

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 28, 11, 20, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.seed_taxonomy(DEFAULT_TAXONOMY)
    d.fill_default_phrases(DEFAULT_PHRASES)
    yield d
    d.close()


def fill(db):
    sid = db.ensure_source("telegram", "c")
    msgs = [("Пожар на рынке, много дыма", 30, "a1"), ("Подтверждаю, пожар на рынке", 25, "a2"),
            ("Опять отключили свет на Гагарина", 20, "a3"), ("Продам велосипед", 10, "a4")]
    for i, (t, m, a) in enumerate(msgs, 1):
        db.upsert_message(Message(sid, i, NOW - timedelta(minutes=m), t, author_hash=a))
    asyncio.run(Labeler(db, LLMChain([])).run_once(now=NOW))


class FakeCollector:
    state = "running"


class FakeClient:
    available = True

    def __init__(self):
        self.states = {}
        self.services = []

    async def set_state(self, entity_id, state, attributes):
        self.states[entity_id] = (state, attributes)

    async def call_service(self, domain, service, data=None):
        self.services.append(f"{domain}.{service}")


def test_build_states(db):
    fill(db)
    states = build_states(db, NOW, MSK, True, {"telegram": "running"})
    text = states["sensor.city_digest_6h"][1]["text"]
    assert "Пожар на рынке" in text and "пишут, что" in text  # пожар подтверждён двумя, свет — одним
    assert "Продам" not in text
    classes = states["sensor.city_digest_classes"][1]
    assert classes["power"].startswith("Электричество, за последние 24 часа 1 событие")
    assert classes["water"] == "Вода: за последние 24 часа сообщений нет."
    assert classes["utilities"].startswith("ЖКХ, за последние 24 часа: электричество:")
    assert "flood" not in classes and "ads" not in classes and "noise" not in classes
    assert states["binary_sensor.city_digest_healthy"][0] == "on"


def test_health_off_when_silent_by_day(db):
    fill(db)
    p = Publisher(db, FakeClient(), FakeCollector())
    p.tz = MSK
    assert p.health(NOW)[0] is True
    assert p.health(NOW + timedelta(hours=2))[0] is False  # 16:20 по Москве, чат молчит больше часа
    assert p.health(NOW + timedelta(hours=11))[0] is True  # 01:20 ночью тишина нормальна
    FakeCollector.state = "need_login"
    try:
        assert p.health(NOW)[0] is False
    finally:
        FakeCollector.state = "running"


def test_publish_sends_all_sensors(db):
    fill(db)
    client = FakeClient()
    p = Publisher(db, client, FakeCollector())
    asyncio.run(p.publish(NOW))
    assert set(client.states) == {"sensor.city_digest_6h", "sensor.city_digest_night",
                                  "sensor.city_digest_classes", "binary_sensor.city_digest_healthy"}


def test_intents_file_written_and_reload_only_on_change(db, tmp_path):
    (tmp_path / "configuration.yaml").write_text(f"yandex_station_intents: !include {INTENTS_FILE}\n")
    client = FakeClient()
    p = Publisher(db, client, FakeCollector(), config_dir=tmp_path)
    asyncio.run(p.sync_intents())
    content = (tmp_path / INTENTS_FILE).read_text()
    assert '"Что нового в городе":' in content and '"Что со светом":' in content
    assert client.services == ["yandex_station_intents.reload"]
    asyncio.run(p.sync_intents())
    assert client.services == ["yandex_station_intents.reload"]  # без изменений — без перезагрузки


def test_intents_not_included_warns(db, tmp_path):
    (tmp_path / "configuration.yaml").write_text("default_config:\n")
    client = FakeClient()
    p = Publisher(db, client, FakeCollector(), config_dir=tmp_path)
    asyncio.run(p.sync_intents())
    assert "не подключён" in p.intents_status and client.services == []


def test_build_intents_dedup_and_validation():
    groups = [{"slug": "utilities", "phrases": ["Что с ЖКХ", "Есть ли отключения"]}]
    cats = [
        {"slug": "power", "phrases": ["Что со светом", "Есть ли отключения", "What about power"]},
        {"slug": "gas", "phrases": ["Что с газом"], "active": False},
    ]
    intents = build_intents(groups, cats)
    phrases = [i.phrase for i in intents]
    assert phrases == ["Что нового в городе", "Что было за ночь", "Что с ЖКХ", "Что со светом"]
    assert intents[3].extra == []  # дубль и латиница убраны
    assert "sensor.city_digest_classes', 'power'" in intents[3].say_template


@pytest.mark.parametrize("p,ok", [("Что с ЖКХ", "Что с ЖКХ"), ("  Что   с водой ", "Что с водой"),
                                  ("Что с водой?", None), ("Есть ли ДТП 2", "Есть ли ДТП 2"), ("", None)])
def test_clean_phrase(p, ok):
    assert clean_phrase(p) == ok


def test_render_yaml_is_valid_yaml():
    yaml = pytest.importorskip("yaml")
    intents = build_intents([], [{"slug": "power", "phrases": ["Что со светом", "Есть ли свет"]}])
    data = yaml.safe_load(render_yaml(intents))
    entry = data["intents"]["Что со светом"]
    assert entry["extra_phrases"] == ["Есть ли свет"]
    assert entry["say_phrase"] == say_template("sensor.city_digest_classes", "power")
    assert "{{ t }}" in entry["say_phrase"]
