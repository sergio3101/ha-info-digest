import json

import pytest
from app.util import author_hash, load_options, load_or_create_salt, normalize_chat_ref, parse_proxy


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("chatsudaka", "chatsudaka"),
        ("@chatsudaka", "chatsudaka"),
        ("https://t.me/chatsudaka", "chatsudaka"),
        ("t.me/chatsudaka/", "chatsudaka"),
        ("https://t.me/chatsudaka/12345", "chatsudaka"),
        ("https://telegram.me/chatsudaka?x=1", "chatsudaka"),
        ("https://t.me/+AbCdEf", "https://t.me/+AbCdEf"),
        ("https://t.me/joinchat/AbCdEf", "https://t.me/joinchat/AbCdEf"),
        ("  ", ""),
    ],
)
def test_normalize_chat_ref(ref, expected):
    assert normalize_chat_ref(ref) == expected


def test_parse_proxy():
    assert parse_proxy(None) is None
    assert parse_proxy("") is None
    assert parse_proxy("socks5://192.168.88.1:1080") == {
        "proxy_type": "socks5",
        "addr": "192.168.88.1",
        "port": 1080,
        "rdns": True,
    }
    p = parse_proxy("socks5://u:p@h:1")
    assert (p["username"], p["password"]) == ("u", "p")
    with pytest.raises(ValueError):
        parse_proxy("ftp://h:1")
    with pytest.raises(ValueError):
        parse_proxy("socks5://h")


def test_author_hash_stable_and_salted():
    assert author_hash("s", None) is None
    assert author_hash("s", 42) == author_hash("s", 42)
    assert author_hash("s", 42) != author_hash("t", 42)
    assert len(author_hash("s", 42)) == 16


def test_salt_created_once(tmp_path):
    p = tmp_path / "salt"
    a = load_or_create_salt(p)
    assert load_or_create_salt(p) == a
    assert oct(p.stat().st_mode)[-3:] == "600"


def test_load_options(tmp_path):
    p = tmp_path / "options.json"
    p.write_text(
        json.dumps(
            {
                "telegram_api_id": 123,
                "telegram_api_hash": "h",
                "telegram_phone": "+7900",
                "telegram_chats": ["https://t.me/chatsudaka", "", "@other"],
                "telegram_proxy": "",
                "retention_days": 30,
                "initial_backfill_hours": 24,
                "log_level": "info",
                "unknown_future_option": True,
            }
        )
    )
    o = load_options(p)
    assert o.telegram_configured
    assert o.telegram_chats == ["chatsudaka", "other"]
    assert o.telegram_proxy is None


def test_load_options_string_api_id_and_blank_fields(tmp_path):
    p = tmp_path / "options.json"
    p.write_text(json.dumps({
        "telegram_api_id": " 1234567 ", "telegram_api_hash": "", "telegram_phone": "",
        "telegram_chats": ["chatsudaka"], "yandex_folder_id": " b1gabc ", "yandex_api_key": "",
    }))
    o = load_options(p)
    assert o.telegram_api_id == 1234567
    assert not o.telegram_configured
    assert o.yandex_folder_id == "b1gabc" and not o.yandex_configured
    p.write_text(json.dumps({"telegram_api_id": "abc", "telegram_chats": []}))
    assert load_options(p).telegram_api_id is None


def test_load_options_missing_file(tmp_path):
    o = load_options(tmp_path / "nope.json")
    assert not o.telegram_configured
