"""Opt-in background account-session scheduler for sub-agent child chats."""

import pytest

from arena.api import ArenaClient
from arena.web import app as web


class FakeClient:
    def __init__(self, cookie="alice-cookie"):
        self.cookie_str = cookie
        self.active_mode = "direct-battle"
        self.active_modality = "chat"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(web, "CHATS_DIR", tmp_path / "chats")
    cfg = {}
    monkeypatch.setattr(web, "load_config", lambda: dict(cfg))
    monkeypatch.setattr(web, "save_config", lambda values: cfg.update(values))
    web._ACCOUNT_SESSION_ACTIVE.clear()
    web._ACCOUNT_SESSION_TOTAL.clear()
    monkeypatch.setattr(web, "_ACCOUNT_SESSION_CURSOR", 0)

    alice = web._register_account({"email": "alice@local.invalid"}, "alice-cookie")
    bob = web._register_account({"email": "bob@local.invalid"}, "bob-cookie")
    store = web._accounts_load()
    store["current"] = alice
    web._accounts_save(store)

    monkeypatch.setattr(web, "_fresh_client", lambda parent: FakeClient(parent.cookie_str))

    def account_client(record, _parent):
        client = FakeClient(record["cookie"])
        return web._tag_account_client(client, record)

    monkeypatch.setattr(web, "_account_client_for", account_client)
    return cfg, alice, bob


def test_scheduler_is_current_account_until_explicitly_enabled(env):
    _cfg, alice, _bob = env
    lease = web._acquire_account_client(FakeClient())
    try:
        assert lease.account_id == alice
        assert lease.client.cookie_str == "alice-cookie"
        status = web._account_sessions_status()
        assert status["enabled"] is False
        assert status["available"] is True
        assert status["scope"] == "new_subagent_and_fanout_child_chats"
        assert all("cookie" not in account for account in status["accounts"])
    finally:
        lease.release()
    assert web._ACCOUNT_SESSION_ACTIVE == {}


def test_enabled_scheduler_leases_least_busy_accounts(env):
    cfg, alice, bob = env
    cfg["multi_session_subagents"] = True
    parent = FakeClient()
    first = web._acquire_account_client(parent)
    second = web._acquire_account_client(parent)
    try:
        # The first background child prefers a free non-current session;
        # a concurrent second child then falls back to the current one.
        assert first.account_id == bob
        assert first.client.cookie_str == "bob-cookie"
        assert second.account_id == alice
        status = web._account_sessions_status()
        active = {account["id"]: account["active_children"] for account in status["accounts"]}
        assert active == {alice: 1, bob: 1}
        assert "bob-cookie" not in str(status)
    finally:
        first.release()
        second.release()
    assert web._ACCOUNT_SESSION_ACTIVE == {}


def test_noncurrent_client_never_uses_current_browser_engine(env, monkeypatch):
    _cfg, alice, bob = env
    monkeypatch.setattr(web, "_engine_on", lambda: True)
    bob_client = FakeClient("bob-cookie")
    bob_client._arena_account_id = bob
    alice_client = FakeClient()
    alice_client._arena_account_id = alice
    assert web._client_uses_engine(bob_client) is False
    assert web._client_uses_engine(alice_client) is True
    assert web._client_uses_engine(FakeClient()) is True


def test_scheduled_subagent_releases_its_lease(env, monkeypatch):
    cfg, _alice, bob = env
    cfg["multi_session_subagents"] = True
    seen = {}

    def fake_subagent(client, _conv, _call, _lane, _child_id):
        seen["account_id"] = web._client_account_id(client)
        yield web._sse({"type": "ping"})
        return True, "ok", "done", {}

    monkeypatch.setattr(web, "_run_subagent", fake_subagent)
    result = yield_value = None
    gen = web._run_subagent_scheduled(
        FakeClient(), {}, {"params": {"task": "look"}}, "a", "child")
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            result = yield_value = stop.value
            break
    assert yield_value[2] == "done"
    assert result[0] is True
    assert seen["account_id"] == bob
    assert web._ACCOUNT_SESSION_ACTIVE == {}


def test_pacer_scope_separates_noncurrent_accounts(env):
    _cfg, alice, bob = env
    assert web._pacer_scope_key({"modality": "text", "account_id": alice}) == "text"
    assert web._pacer_scope_key({"modality": "text", "account_id": bob}) == f"account:{bob}:text"
    assert web._pacer_scope_key({"modality": "video", "account_id": bob}) == f"account:{bob}:media"


def test_account_bound_client_rotates_only_to_its_sink():
    seen = []
    client = ArenaClient(cookie_str="session=one", allow_cdp=False, cookie_sink=seen.append)
    client.session.cookies.set("session", "two", domain="arena.ai", path="/")
    client.sync_cookies()
    assert seen == ["session=two"]
