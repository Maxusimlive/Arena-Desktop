import json

import pytest

from arena import config, crypto
from arena.web import app as web


def _fake_dpapi(monkeypatch):
    monkeypatch.setattr(crypto, "DPAPI_AVAILABLE", True)
    monkeypatch.setattr(crypto, "_dpapi_protect", lambda data: data[::-1])
    monkeypatch.setattr(crypto, "_dpapi_unprotect", lambda data: data[::-1])


def test_legacy_cookie_is_never_base64_mangled():
    legacy = "arena-auth-prod-v1.0=abc%3D%3D; refresh=x+y/z==; theme=dark"
    assert crypto.decode_cookie(legacy) == legacy
    assert crypto.unprotect_text(legacy) == legacy
    assert crypto.is_protected(legacy) is False


def test_dpapi_roundtrip_uses_versioned_prefix(monkeypatch):
    _fake_dpapi(monkeypatch)
    secret = "arena-auth-prod-v1.0=secret-token-value"
    stored = crypto.protect_text(secret)
    assert stored.startswith(crypto.PREFIX)
    assert "secret-token-value" not in stored
    assert crypto.unprotect_text(stored) == secret
    assert crypto.protect_text(stored) == stored


def test_bad_or_unavailable_dpapi_value_fails_closed(monkeypatch):
    assert crypto.decode_cookie(crypto.PREFIX + "not valid base64!") == ""
    _fake_dpapi(monkeypatch)
    stored = crypto.protect_text("cookie=value")
    monkeypatch.setattr(crypto, "DPAPI_AVAILABLE", False)
    monkeypatch.setattr(
        crypto, "_dpapi_unprotect",
        lambda data: (_ for _ in ()).throw(OSError("unavailable")))
    assert crypto.decode_cookie(stored) == ""


def test_config_cookie_file_migrates_legacy_plaintext(tmp_path, monkeypatch):
    _fake_dpapi(monkeypatch)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    cookie_file = tmp_path / "cookies.txt"
    monkeypatch.setattr(config, "COOKIE_FILE", cookie_file)
    cookie_file.write_text("arena-auth-prod-v1.0=legacy-value", encoding="utf-8")

    assert config.get_cookie() == "arena-auth-prod-v1.0=legacy-value"
    stored = cookie_file.read_text(encoding="utf-8")
    assert crypto.is_protected(stored)
    assert "legacy-value" not in stored

    config.save_cookie("arena-auth-prod-v1.0=new-value")
    assert crypto.is_protected(cookie_file.read_text(encoding="utf-8"))
    assert config.get_cookie() == "arena-auth-prod-v1.0=new-value"


def test_account_store_encrypts_at_rest_and_migrates(tmp_path, monkeypatch):
    _fake_dpapi(monkeypatch)
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(web, "ACCOUNTS_FILE", accounts_file)

    account_id = web._register_account(
        {"email": "alice@local.invalid"}, "alice-secret-cookie")
    raw = json.loads(accounts_file.read_text(encoding="utf-8"))
    assert raw["accounts"][0]["id"] == account_id
    assert crypto.is_protected(raw["accounts"][0]["cookie"])
    assert "alice-secret-cookie" not in accounts_file.read_text(encoding="utf-8")
    assert web._accounts_load()["accounts"][0]["cookie"] == "alice-secret-cookie"

    raw["accounts"][0]["cookie"] = "alice-legacy-cookie"
    accounts_file.write_text(json.dumps(raw), encoding="utf-8")
    assert web._accounts_load()["accounts"][0]["cookie"] == "alice-legacy-cookie"
    migrated = json.loads(accounts_file.read_text(encoding="utf-8"))
    assert crypto.is_protected(migrated["accounts"][0]["cookie"])
    assert "alice-legacy-cookie" not in accounts_file.read_text(encoding="utf-8")


def test_account_dpapi_failure_does_not_write_plaintext_or_destroy_old_store(tmp_path, monkeypatch):
    accounts = tmp_path / 'accounts.json'
    accounts.write_text('{"current": null, "accounts": []}', encoding='utf-8')
    monkeypatch.setattr(web, 'ACCOUNTS_FILE', accounts)
    _fake_dpapi(monkeypatch)
    monkeypatch.setattr(crypto, '_dpapi_protect', lambda data: (_ for _ in ()).throw(OSError('DPAPI failed')))
    assert web._register_account({'email': 'test@example.test'}, 'secret-cookie') is None
    assert 'secret-cookie' not in accounts.read_text(encoding='utf-8')
    assert json.loads(accounts.read_text(encoding='utf-8'))['accounts'] == []


def test_unreadable_protected_cookie_not_lost_when_other_record_saved(tmp_path, monkeypatch):
    import base64
    accounts = tmp_path / 'accounts.json'
    stored = crypto.PREFIX + base64.b64encode(b'ciphertext').decode('ascii')
    accounts.write_text(json.dumps({'current': 'a', 'accounts': [{'id': 'a', 'cookie': stored}]}), encoding='utf-8')
    monkeypatch.setattr(web, 'ACCOUNTS_FILE', accounts)
    monkeypatch.setattr(crypto, '_dpapi_unprotect', lambda data: (_ for _ in ()).throw(OSError('DPAPI unavailable')))
    record = web._accounts_load()['accounts'][0]
    assert record['cookie'] == ''
    assert web._accounts_save({'current': 'a', 'accounts': [record]})
    saved = json.loads(accounts.read_text(encoding='utf-8'))['accounts'][0]
    assert saved['cookie'] == stored
    assert '_protected_cookie' not in saved
