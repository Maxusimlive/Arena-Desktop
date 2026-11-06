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

    def fake_tunnel(cf, generation, **kwargs):
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
    def fake_tunnel(cf, generation, **kwargs):
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


import os
import stat
import sys

import pytest


def _write_script(path, body):
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX fake cloudflared")
def test_dead_cloudflared_reports_exit_code_and_output(tmp_path):
    fake = _write_script(tmp_path / "cloudflared",
                         "#!/bin/sh\n"
                         "echo 'ERR Cannot determine default origin certificate' >&2\n"
                         "echo 'ERR failed to connect to the edge' >&2\n"
                         "exit 3\n")
    mgr = GatewayManager(tool_index=lambda: {}, call_tool=lambda *_: "ok",
                         approve=lambda *_: True, cloudflared_path=lambda: fake)
    try:
        status = mgr.start(["workspace"], wait_seconds=5)
        assert status["state"] == "off"
        assert "code 3" in status["error"]
        assert "failed to connect to the edge" in status["error"]
        assert any("origin certificate" in line for line in status["tunnel_log"])
        # Both the default and the http2 attempt ran before giving up.
        assert any("http2" in line for line in status["tunnel_log"])
    finally:
        mgr.stop()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX fake cloudflared")
def test_undecodable_cloudflared_output_does_not_kill_tunnel(tmp_path):
    """Regression: UTF-8 output that the locale encoding cannot decode
    (Cyrillic interface names, table borders) killed the reader thread,
    and the healthy tunnel was torn down as 'cloudflared exited'."""
    fake = _write_script(tmp_path / "cloudflared",
                         "#!/bin/sh\n"
                         "printf '\\377\\376 invalid bytes \\342\\224\\202\\n'\n"
                         "echo 'INF |  https://fake-encoding.trycloudflare.com  |'\n"
                         "sleep 30\n")
    mgr = GatewayManager(tool_index=lambda: {}, call_tool=lambda *_: "ok",
                         approve=lambda *_: True, cloudflared_path=lambda: fake)
    try:
        mgr.start(["workspace"], wait_seconds=0)
        deadline = time.time() + 8
        while time.time() < deadline and not mgr._url:
            time.sleep(0.05)
        assert mgr._url == "https://fake-encoding.trycloudflare.com"
        time.sleep(0.5)
        assert mgr.status()["state"] == "starting"  # reader alive, tunnel not torn down
        assert mgr._tunnel is not None and mgr._tunnel.poll() is None
    finally:
        mgr.stop()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX fake cloudflared")
def test_flaky_cloudflared_retries_with_http2(tmp_path):
    counter = tmp_path / "count"
    counter.write_text("0")
    fake = _write_script(tmp_path / "cloudflared",
                         "#!/bin/sh\n"
                         f"n=$(cat {counter}); n=$((n+1)); echo $n > {counter}\n"
                         "if [ \"$n\" -lt 2 ]; then echo 'ERR quic blocked' >&2; exit 1; fi\n"
                         "echo 'INF |  https://fake-retry.trycloudflare.com  |'\n"
                         "sleep 30\n")
    mgr = GatewayManager(tool_index=lambda: {}, call_tool=lambda *_: "ok",
                         approve=lambda *_: True, cloudflared_path=lambda: fake)
    try:
        mgr.start(["workspace"], wait_seconds=0)
        deadline = time.time() + 8
        while time.time() < deadline and mgr._tunnel_protocol != "http2":
            time.sleep(0.05)
        assert mgr._tunnel_protocol == "http2"
        assert counter.read_text().strip() == "2"
        deadline = time.time() + 8
        while time.time() < deadline and not mgr._url:
            time.sleep(0.05)
        assert mgr._url == "https://fake-retry.trycloudflare.com"
        assert mgr.status()["state"] == "starting"  # unverified until health passes
    finally:
        mgr.stop()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX fake cloudflared")
def test_tunnel_dying_after_address_falls_back_to_http2(tmp_path):
    """Field case: address assigned, then QUIC registration dies
    ('no recent network activity') and cloudflared exits before the
    session was ever healthy. That must trigger the http2 fallback too,
    not just a death before any address was assigned."""
    counter = tmp_path / "count"
    counter.write_text("0")
    fake = _write_script(tmp_path / "cloudflared",
                         "#!/bin/sh\n"
                         f"n=$(cat {counter}); n=$((n+1)); echo $n > {counter}\n"
                         "echo 'INF |  https://fake-quic-dead.trycloudflare.com  |'\n"
                         "if [ \"$n\" -lt 2 ]; then echo 'ERR no recent network activity' >&2; exit 1; fi\n"
                         "sleep 30\n")
    mgr = GatewayManager(tool_index=lambda: {}, call_tool=lambda *_: "ok",
                         approve=lambda *_: True, cloudflared_path=lambda: fake)
    try:
        mgr.start(["workspace"], wait_seconds=0)
        deadline = time.time() + 10
        while time.time() < deadline and mgr._tunnel_protocol != "http2":
            time.sleep(0.05)
        assert mgr._tunnel_protocol == "http2"
        assert counter.read_text().strip() == "2"
        deadline = time.time() + 10
        while time.time() < deadline and not mgr._url:
            time.sleep(0.05)
        assert mgr._url == "https://fake-quic-dead.trycloudflare.com"
        assert mgr.status()["state"] == "starting"
    finally:
        mgr.stop()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX fake cloudflared")
def test_preferred_http2_starts_directly_without_quic_attempt(tmp_path):
    args_file = tmp_path / "args"
    fake = _write_script(tmp_path / "cloudflared",
                         "#!/bin/sh\n"
                         f"echo \"$@\" > {args_file}\n"
                         "echo 'INF |  https://fake-preferred.trycloudflare.com  |'\n"
                         "sleep 30\n")
    mgr = GatewayManager(tool_index=lambda: {}, call_tool=lambda *_: "ok",
                         approve=lambda *_: True, cloudflared_path=lambda: fake)
    try:
        mgr.start(["workspace"], wait_seconds=0, preferred_protocol="http2")
        deadline = time.time() + 8
        while time.time() < deadline and not args_file.exists():
            time.sleep(0.05)
        assert "--protocol http2" in args_file.read_text()
        assert mgr._tunnel_protocol == "http2"
        deadline = time.time() + 8
        while time.time() < deadline and not mgr._url:
            time.sleep(0.05)
        assert mgr._url == "https://fake-preferred.trycloudflare.com"
    finally:
        mgr.stop()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX fake cloudflared")
def test_registered_tunnel_turns_on_without_public_probe(tmp_path):
    """Field case: the machine's DNS resolver is flaky, so the public
    trycloudflare URL cannot be probed from this machine at all — yet a
    registered tunnel plus a healthy loopback origin must turn the
    session on (the agent resolves the address from Arena's side)."""
    fake = _write_script(tmp_path / "cloudflared",
                         "#!/bin/sh\n"
                         "echo 'INF |  https://fake-registered.trycloudflare.com  |'\n"
                         "echo 'INF Registered tunnel connection connIndex=0 protocol=http2'\n"
                         "sleep 30\n")
    mgr = GatewayManager(tool_index=lambda: {}, call_tool=lambda *_: "ok",
                         approve=lambda *_: True, cloudflared_path=lambda: fake)
    try:
        status = mgr.start(["workspace"], wait_seconds=10)
        assert status["state"] == "on"
        assert status["tunnel_registered"] is True
        assert mgr.injection_block()  # address is advertised to the agent
    finally:
        mgr.stop()


def test_sync_wait_returns_finished_job_in_one_request():
    """?wait= mode: the POST response is the completed job, so the agent
    spends one tool call instead of a polling loop."""
    mgr = GatewayManager(
        tool_index=lambda: {"s": {"tools": {"ping": {"alias": "ping", "read_only": True}}}},
        call_tool=lambda alias, args: "pong",
        approve=lambda *_: True, cloudflared_path=lambda: None)
    try:
        status = mgr.start(["s"], tunnel=False)
        assert status["state"] == "on"
        url, token = status["url"], status["pairing"]

        def req(method, path, body=None):
            r = urllib.request.Request(
                url + path, method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(r, timeout=15) as resp:
                return resp.status, json.loads(resp.read())

        code, job = req("POST", "/v1/jobs?wait=10",
                        {"server": "s", "steps": [{"tool": "ping", "arguments": {}}]})
        assert code == 200 and job["status"] == "done"
        assert job["results"][0]["output"] == "pong"
        # Async mode still exists, and GET supports long-polling too.
        code, accepted = req("POST", "/v1/jobs",
                             {"server": "s", "steps": [{"tool": "ping", "arguments": {}}]})
        assert code == 202 and accepted["job_id"]
        code, job2 = req("GET", f"/v1/jobs/{accepted['job_id']}?wait=10")
        assert code == 200 and job2["status"] == "done"
    finally:
        mgr.stop()


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
    # No local HTTP server exists in this fixture, so the loopback origin
    # check fails three times in a row and the watchdog stops the session.
    reasons = []
    def stop(reason):
        reasons.append(reason)
        mgr._state = "off"
        mgr._running = False
    monkeypatch.setattr(mgr, "stop", stop)
    mgr._watchdog_loop(1)
    assert mgr.status()["state"] == "off"
    assert "local gateway" in reasons[0]
