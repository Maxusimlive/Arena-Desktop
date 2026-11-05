"""Focused lifecycle regressions; no Cloudflare account is required."""
import threading
import time
import urllib.request
import json

from arena.gateway import GatewayManager


def manager():
    return GatewayManager(tool_index=lambda: {}, call_tool=lambda *_: "ok",
                          approve=lambda *_: True, cloudflared_path=lambda: "fake")


def test_concurrent_start_joins_and_never_advertises_unverified_url(monkeypatch):
    mgr = manager()
    proceed = threading.Event()
    calls = []

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *_): return json.dumps({"ok": True, "service": "arena-desktop-gateway"}).encode()

    def fake_urlopen(*args, **kwargs):
        assert proceed.wait(4)
        return Response()

    def fake_tunnel(cf, generation):
        calls.append(generation)
        mgr._url = "https://fake-test.trycloudflare.com"
        threading.Thread(target=mgr._probe_tunnel, args=(mgr._url, generation), daemon=True).start()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(mgr, "_start_tunnel", fake_tunnel)
    try:
        first = mgr.start(["workspace"], wait_seconds=0)
        assert first["state"] == "starting" and "pairing" not in first
        assert mgr.injection_block() == ""
        second = mgr.start(["workspace"], wait_seconds=0)
        assert second["state"] == "starting" and len(calls) == 1
        proceed.set()
        assert mgr._ready.wait(4)
        assert mgr.status(include_pairing=True)["state"] == "on"
        assert mgr.status(include_pairing=True)["pairing"]
    finally:
        proceed.set()
        mgr.stop()


def test_stale_probe_cannot_reactivate_stopped_session(monkeypatch):
    mgr = manager()
    entered, proceed = threading.Event(), threading.Event()
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *_): return b'{"ok":true,"service":"arena-desktop-gateway"}'
    def fake_urlopen(*args, **kwargs):
        entered.set()
        assert proceed.wait(4)
        return Response()
    def fake_tunnel(cf, generation):
        mgr._url = "https://fake-test.trycloudflare.com"
        threading.Thread(target=mgr._probe_tunnel, args=(mgr._url, generation), daemon=True).start()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(mgr, "_start_tunnel", fake_tunnel)
    mgr.start(["workspace"], wait_seconds=0)
    assert entered.wait(4)
    mgr.stop()
    proceed.set()
    time.sleep(.1)
    assert mgr.status()["state"] == "off"
    assert mgr.injection_block() == ""


def test_stop_during_approval_prevents_call_and_next_steps():
    entered, allow = threading.Event(), threading.Event()
    calls = []
    mgr = GatewayManager(tool_index=lambda: {"s": {"tools": {"x": {"alias": "x"}}}},
                         call_tool=lambda alias, args: calls.append(alias),
                         approve=lambda *_: (entered.set(), allow.wait(5))[1],
                         cloudflared_path=lambda: None)
    try:
        mgr.start(["s"], tunnel=False)
        job_id, err = mgr._submit_job({"server": "s", "steps": [{"tool": "x"}]})
        assert not err and entered.wait(4)
        mgr.stop()
        allow.set()
        time.sleep(.1)
        assert calls == []
        assert mgr._jobs[job_id]["status"] == "error"
    finally:
        allow.set()
        mgr.stop()


def test_shell_uses_explicit_platform_shell(monkeypatch, tmp_path):
    from arena import agent
    calls = []
    class Proc:
        stdout = stderr = None
        returncode = 0
        pid = 1
        def poll(self): return 0
        def wait(self, timeout=None): return 0
    monkeypatch.setattr(agent.subprocess, 'Popen', lambda argv, **kw: calls.append((argv, kw)) or Proc())
    monkeypatch.setattr(agent, 'os_name_is_windows', lambda: True)
    agent.run_shell_command('echo hello && dir', tmp_path)
    assert calls[-1][0] == ['cmd.exe', '/d', '/s', '/c', 'echo hello && dir']
    assert calls[-1][1]['shell'] is False
    monkeypatch.setattr(agent, 'os_name_is_windows', lambda: False)
    agent.run_shell_command('echo hello && ls', tmp_path)
    assert calls[-1][0] == ['/bin/sh', '-lc', 'echo hello && ls']
    assert calls[-1][1]['shell'] is False


def test_watchdog_clears_on_after_three_consecutive_health_failures(monkeypatch):
    from types import SimpleNamespace
    from arena import gateway

    mgr = manager()
    mgr._generation = 1
    mgr._running = True
    mgr._state = "on"
    mgr._url = "https://fake-test.trycloudflare.com"
    mgr._tunnel = object()
    monkeypatch.setattr(mgr, "_expired", lambda: False)
    clock = iter([20, 20, 40, 40, 60, 60])
    monkeypatch.setattr(gateway, "time", SimpleNamespace(sleep=lambda _: None,
                        time=lambda: mgr._last_activity, monotonic=lambda: next(clock)))
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise OSError("503")
    monkeypatch.setattr(urllib.request, "urlopen", fail)
    reasons = []
    def stop(reason):
        reasons.append(reason)
        mgr._state = "off"
        mgr._running = False
    monkeypatch.setattr(mgr, "stop", stop)
    mgr._watchdog_loop(1)
    assert len(calls) == 3
    assert mgr.status()["state"] == "off"
    assert "health checks" in reasons[0]
