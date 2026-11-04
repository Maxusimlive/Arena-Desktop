import pytest

from arena import deep_links
from arena.web import app as appmod


CHAT_ID = "123e4567-e89b-42d3-a456-426614174000"


@pytest.mark.parametrize("raw,expected", [
    ("arenadesktop://open", {"action": "open"}),
    ("arenadesktop://new", {"action": "new"}),
    ("arenadesktop://ask?q=Hello%20there", {"action": "ask", "text": "Hello there"}),
    (f"arenadesktop://chat?id={CHAT_ID}", {"action": "chat", "chat_id": CHAT_ID}),
    (f"arenadesktop://chat/{CHAT_ID}?msg=3",
     {"action": "chat", "chat_id": CHAT_ID, "msg_index": 3}),
])
def test_parse_supported_deep_links(raw, expected):
    assert deep_links.parse_deep_link(raw) == expected


@pytest.mark.parametrize("raw", [
    "https://example.test/open",
    "arenadesktop://delete-everything",
    "arenadesktop://ask",
    "arenadesktop://ask?q=" + "x" * 10001,
    "arenadesktop://chat?id=not-a-chat",
    f"arenadesktop://chat?id={CHAT_ID}&msg=-1",
    "",
])
def test_parse_rejects_unsafe_or_invalid_deep_links(raw):
    with pytest.raises(ValueError):
        deep_links.parse_deep_link(raw)


def test_find_arg_and_instance_roundtrip(monkeypatch, tmp_path):
    assert deep_links.find_deep_link_arg(
        ["--mode", "direct", "ArenaDesktop://new"]) == "ArenaDesktop://new"
    assert deep_links.find_deep_link_arg(["--background"]) is None

    path = tmp_path / "instance.json"
    monkeypatch.setattr(deep_links, "_INSTANCE_FILE", path)
    deep_links.write_instance(49152, "test-token")
    data = deep_links.read_instance()
    assert data["port"] == 49152
    assert data["token"] == "test-token"
    assert data["pid"] > 0
    assert deep_links.pid_alive(data["pid"]) is True


def test_registry_is_not_touched_for_source_runs():
    # The test process is never a frozen Windows EXE.
    assert deep_links.register_protocol() is False


def test_deep_link_endpoint_requires_instance_token(monkeypatch):
    seen = []
    monkeypatch.setattr(appmod, "INSTANCE_TOKEN", "secret-instance-token")
    monkeypatch.setattr(appmod, "DEEP_LINK_HOOK", lambda payload: seen.append(payload) or True)
    client = appmod.app.test_client()

    rejected = client.post(
        "/api/deep-link",
        json={"url": "arenadesktop://ask?q=Do%20not%20send%20this"},
        headers={"X-Arena-Instance-Token": "wrong"},
    )
    assert rejected.status_code == 403
    assert seen == []

    accepted = client.post(
        "/api/deep-link",
        json={"url": "arenadesktop://ask?q=Do%20not%20send%20this"},
        headers={"X-Arena-Instance-Token": "secret-instance-token"},
    )
    assert accepted.status_code == 200
    assert accepted.get_json() == {"ok": True, "action": "ask"}
    assert seen == [{"action": "ask", "text": "Do not send this"}]
    assert b"Do not send this" not in accepted.data


def test_deep_link_endpoint_reports_missing_window(monkeypatch):
    monkeypatch.setattr(appmod, "INSTANCE_TOKEN", "secret-instance-token")
    monkeypatch.setattr(appmod, "DEEP_LINK_HOOK", None)
    response = appmod.app.test_client().post(
        "/api/deep-link",
        json={"url": "arenadesktop://open"},
        headers={"X-Arena-Instance-Token": "secret-instance-token"},
    )
    assert response.status_code == 503
