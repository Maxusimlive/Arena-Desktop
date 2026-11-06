"""Agent Gateway (v5.5): let the genuine Arena Agent call selected local
MCP servers through a temporary, pairing-protected tunnel session.

Shape (after the user's ArenaAgentGateway design, reimplemented on this
app's MCP layer): a loopback HTTP job API in front of the already-running
local MCP connectors, exposed for one session through a Cloudflare Quick
Tunnel. Bearer pairing code (secrets.token_urlsafe(32), constant-time
compare), hard expiry + idle auto-stop, per-call approvals delegated to
the app's PermissionBroker, serial job execution. Nothing listens on any
non-loopback interface and nothing survives Stop: token, tunnel and the
HTTP server all go away together.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
import subprocess
import threading
import time
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

MAX_BODY = 262144
MAX_STEPS = 40
MAX_WAIT_SECONDS = 90  # stay below Cloudflare's ~100s proxy read timeout
MAX_OUTPUT = 20000
_TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class _Handler(BaseHTTPRequestHandler):
    server_version = "ArenaDesktopGateway/1"
    protocol_version = "HTTP/1.1"

    @property
    def mgr(self) -> "GatewayManager":
        return self.server.manager  # type: ignore[attr-defined]

    def log_message(self, *args):  # keep stdout clean
        pass

    def _json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authed(self) -> bool:
        token = self.mgr._token
        if not token or self.mgr._expired():
            return False
        auth = self.headers.get("Authorization") or ""
        candidate = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if not candidate:
            candidate = self.headers.get("X-Arena-Pairing") or ""
        return bool(candidate) and hmac.compare_digest(candidate, token)

    def _wait_seconds(self, payload=None) -> float:
        raw = None
        if payload is not None and payload.get("wait_seconds") is not None:
            raw = payload.get("wait_seconds")
        elif "?" in self.path:
            from urllib.parse import parse_qs, urlparse
            values = parse_qs(urlparse(self.path).query).get("wait")
            raw = values[0] if values else None
        try:
            return max(0.0, min(float(raw), MAX_WAIT_SECONDS)) if raw is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def do_GET(self):  # noqa: N802 (stdlib naming)
        path = self.path.split("?", 1)[0]
        if path == "/v1/health":
            self._json(200, {"ok": True, "service": "arena-desktop-gateway"})
            return
        if not self._authed():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        self.mgr._touch()
        if path == "/v1/servers":
            self._json(200, {"ok": True, "servers": self.mgr._public_index()})
        elif path.startswith("/v1/jobs/"):
            job_id = path[len("/v1/jobs/"):]
            wait = self._wait_seconds()
            job = self.mgr.wait_job(job_id, wait) if wait else self.mgr._jobs.get(job_id)
            if job is None:
                self._json(404, {"ok": False, "error": "job not found"})
            else:
                self._json(200, {"ok": True, **job})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path != "/v1/jobs":
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not self._authed():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._json(413, {"ok": False, "error": "bad content length"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json(400, {"ok": False, "error": "invalid JSON"})
            return
        accepted, error = self.mgr._submit_job(payload)
        if accepted is None:
            self._json(400, {"ok": False, "error": error})
            return
        wait = self._wait_seconds(payload)
        if wait:
            # Sync mode: hold this one request until the job completes and
            # answer with the finished job — the agent spends a single tool
            # call instead of a polling loop of them.
            job = self.mgr.wait_job(accepted, wait)
            if job is not None:
                self._json(200, {"ok": True, **job})
                return
        self._json(202, {"ok": True, "job_id": accepted})


class GatewayManager:
    def __init__(self, *, tool_index: Callable[[], Dict[str, Any]],
                 call_tool: Callable[[str, Dict[str, Any]], str],
                 approve: Callable[[str, str, bool, Dict[str, Any]], bool],
                 cloudflared_path: Callable[[], Optional[Any]],
                 logger=None):
        self._tool_index = tool_index
        self._call_tool = call_tool
        self._approve = approve
        self._cloudflared_path = cloudflared_path
        self._log = logger
        self._lock = threading.Lock()
        self._start_lock = threading.RLock()
        self._generation = 0
        self._state = "off"  # off | starting | on | needs_cloudflared
        self._last_error: Optional[str] = None
        self._token: Optional[str] = None
        self._url: Optional[str] = None
        self._servers: List[str] = []
        self._index: Dict[str, Any] = {}
        self._created = 0.0
        self._expires = 0.0
        self._last_activity = 0.0
        self._idle_seconds = 45 * 60
        self._http: Optional[ThreadingHTTPServer] = None
        self._tunnel: Optional[subprocess.Popen] = None
        self._ready = threading.Event()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._job_events: Dict[str, threading.Event] = {}
        self._job_queue: deque = deque()
        self._job_cv = threading.Condition()
        self._calls: deque = deque(maxlen=40)
        self._worker: Optional[threading.Thread] = None
        self._watchdog: Optional[threading.Thread] = None
        self._running = False
        # Diagnostics for a tunnel that will not stay up: the last lines
        # cloudflared printed (its own error is the whole story — previously
        # they were read only to spot the tunnel URL and then thrown away,
        # leaving an opaque "cloudflared exited"), plus retry bookkeeping
        # for the http2 fallback below.
        self._tunnel_log: deque = deque(maxlen=60)
        self._tunnel_protocol: Optional[str] = None
        self._tunnel_retried = False
        self._cf_path: Optional[str] = None
        self._fallback_at: Optional[float] = None
        self._used_fallback = False
        # The tunnel is considered live when cloudflared reports a
        # registered connection AND the local origin answers on loopback.
        # Verifying through the public URL from this same machine proved
        # fragile in the field: a flaky local DNS resolver (timeouts seen
        # in real logs) failed the check for a full minute and killed
        # tunnels that were perfectly reachable for the remote agent,
        # which resolves the address from Arena's side anyway.
        self._tunnel_registered = False
        self._public_check: Optional[str] = None

    # ---------- status ----------
    def summary(self) -> Dict[str, Any]:
        return {"active": self._state == "on", "state": self._state,
                "servers": len(self._servers) if self._state == "on" else 0}

    def status(self, include_pairing: bool = False) -> Dict[str, Any]:
        out = {
            "state": self._state, "url": self._url, "servers": list(self._servers),
            "started_at": self._created or None, "expires_at": self._expires or None,
            "error": self._last_error, "calls": list(reversed(self._calls)),
            "tunnel_log": list(self._tunnel_log),
            "tunnel_protocol": self._tunnel_protocol,
            "cloudflared": self._cf_path,
            "used_fallback": self._used_fallback,
            "tunnel_registered": self._tunnel_registered,
            "public_check": self._public_check,
        }
        if include_pairing and self._state == "on":
            out["pairing"] = self._token
        return out

    def _expired(self) -> bool:
        return bool(self._expires) and time.time() > self._expires

    def _touch(self) -> None:
        self._last_activity = time.time()

    def _info(self, message: str) -> None:
        try:
            if self._log:
                self._log.info("gateway: %s", message)
        except Exception:
            pass

    # ---------- lifecycle ----------
    def start(self, servers: List[str], *, idle_minutes: int = 45,
              hard_hours: int = 8, wait_seconds: float = 45,
              tunnel: bool = True,
              preferred_protocol: Optional[str] = None) -> Dict[str, Any]:
        # A second caller joins the existing start instead of creating another
        # HTTP server/tunnel and orphaning the first one.
        with self._start_lock:
            if self._state in ("on", "starting"):
                if sorted(servers) != sorted(self._servers):
                    self.stop("restart with a different server set")
                elif self._state == "on":
                    return self.status(include_pairing=True)
            if self._state != "starting":
                cf = self._cloudflared_path() if tunnel else None
                if tunnel and cf is None:
                    self._state = "needs_cloudflared"
                    self._last_error = "cloudflared is not installed"
                    return self.status(include_pairing=True)
                self._generation += 1
                generation = self._generation
                self._state = "starting"
                self._last_error = None
                self._tunnel_log.clear()
                # A network where QUIC already proved dead starts directly
                # on HTTP/2 (the caller persists this preference); the one
                # allowed fallback is then already spent.
                self._tunnel_retried = preferred_protocol == "http2"
                self._tunnel_protocol = None
                self._fallback_at = None
                self._used_fallback = False
                self._tunnel_registered = False
                self._public_check = None
                self._cf_path = str(cf) if cf else None
                self._servers = list(servers)
                self._token = secrets.token_urlsafe(32)
                self._created = time.time()
                self._expires = self._created + hard_hours * 3600
                self._idle_seconds = max(60, idle_minutes * 60)
                self._touch()
                self._ready = threading.Event()
                try:
                    self._index = self._filtered_index()
                    self._start_http()
                    self._running = True
                    self._worker = threading.Thread(target=self._worker_loop, args=(generation,), daemon=True)
                    self._worker.start()
                    if tunnel:
                        self._start_tunnel(str(cf), generation,
                                           protocol="http2" if preferred_protocol == "http2" else None)
                    else:
                        self._url = f"http://127.0.0.1:{self._http.server_address[1]}"
                        self._state = "on"
                        self._ready.set()
                    self._watchdog = threading.Thread(target=self._watchdog_loop, args=(generation,), daemon=True)
                    self._watchdog.start()
                except Exception as e:
                    self.stop(f"startup failed: {type(e).__name__}: {e}")
                    return self.status(include_pairing=True)
            ready = self._ready
        # The caller's patience is not the tunnel's deadline. Continue probing
        # in the background; never mark ready without a successful health check.
        # If the QUIC attempt falls back to HTTP/2 mid-wait, the fallback gets
        # its own full budget — otherwise the caller gives up ("state=
        # starting") seconds before the healthy http2 tunnel is verified.
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while not ready.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if ready.wait(min(remaining, 2.0)):
                break
            with self._start_lock:
                if self._fallback_at and self._fallback_at + wait_seconds > deadline:
                    deadline = self._fallback_at + wait_seconds
        return self.status(include_pairing=True)

    def stop(self, reason: str = "stopped") -> Dict[str, Any]:
        with self._start_lock:
            if self._state != "off":
                self._info(f"stopping ({reason})")
            self._generation += 1
            self._last_error = None if reason == "stopped" else reason
            self._cleanup()
            self._state = "off"
            self._token = None
            self._url = None
            self._servers = []
            self._ready.set()
            return self.status()

    def _cleanup(self) -> None:
        self._running = False
        with self._job_cv:
            self._job_queue.clear()
            self._job_cv.notify_all()
        if self._tunnel is not None:
            proc, self._tunnel = self._tunnel, None
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self._http is not None:
            server, self._http = self._http, None
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        with self._lock:
            for job in self._jobs.values():
                if job.get("status") in ("queued", "running"):
                    job["status"] = "error"
                    job["error"] = "gateway session ended"
                    event = self._job_events.get(job.get("job_id"))
                    if event is not None:
                        event.set()

    def _start_http(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.manager = self  # type: ignore[attr-defined]
        server.daemon_threads = True
        self._http = server
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def _start_tunnel(self, cf: str, generation: int, protocol: Optional[str] = None) -> None:
        port = self._http.server_address[1]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        args = [cf, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"]
        if protocol:
            args += ["--protocol", protocol]
        self._tunnel_protocol = protocol
        # Binary pipe, decoded by hand below: with text=True the pipe uses
        # the Windows locale encoding (cp1251 on Russian systems) while
        # cloudflared writes UTF-8 (interface names, box-drawing table
        # borders). An undecodable byte killed the reader thread with
        # UnicodeDecodeError, the dead reader was mistaken for a dead
        # process, and the gateway tore down a perfectly healthy tunnel
        # with a baffling, error-free "cloudflared exited".
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, creationflags=flags)
        self._tunnel = proc
        threading.Thread(target=self._tunnel_reader, args=(proc, generation), daemon=True).start()

    def _tunnel_reader(self, proc, generation: int) -> None:
        if proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
                clean = line.rstrip("\r\n")
                if self._token:
                    clean = clean.replace(self._token, "***")
                if clean.strip():
                    self._tunnel_log.append(clean[-500:])
                with self._start_lock:
                    if generation != self._generation or not self._running:
                        return
                    match = _TUNNEL_URL.search(line or "")
                    if match and not self._url:
                        self._url = match.group(0)
                        self._info("tunnel address assigned; checking health")
                        threading.Thread(target=self._probe_tunnel,
                                         args=(self._url, generation), daemon=True).start()
                    if "Registered tunnel connection" in line and not self._tunnel_registered:
                        self._tunnel_registered = True
                        self._info("tunnel connection registered; verifying local gateway")
                        threading.Thread(target=self._verify_registered,
                                         args=(generation,), daemon=True).start()
        except Exception:
            pass
        # EOF on the pipe normally means the process ended, but if it is
        # somehow still alive (reader broke early), wait for the real exit
        # instead of reporting a phantom one with code None.
        try:
            returncode = proc.wait(timeout=15)
        except Exception:
            returncode = proc.poll()
        with self._start_lock:
            if generation != self._generation or not self._running:
                return
            if self._tunnel is not proc:
                return  # superseded by a fallback restart; that reader owns the session now
            # Quick Tunnels speak QUIC (UDP 7844) by default. Plenty of ISPs
            # and routers let the tiny pre-check probes through and then
            # throttle or NAT-timeout the sustained tunnel: cloudflared gets
            # its address, retries registration against a dead QUIC stream
            # ("no recent network activity") and exits. So the HTTP/2
            # fallback applies whenever the tunnel dies before the session
            # was ever verified healthy — address assigned or not.
            if self._state == "starting" and self._fallback_to_http2(generation, proc):
                return
            tail = [line for line in list(self._tunnel_log)[-3:] if line.strip()]
            detail = " | ".join(tail)[-400:]
            reason = f"cloudflared exited (code {returncode})"
            if detail:
                reason += f": {detail}"
            for line in tail:
                self._info(f"cloudflared: {line}")
            self.stop(reason)

    def _fallback_to_http2(self, generation: int, dying=None) -> bool:
        """Restart a never-verified tunnel once over HTTP/2 (TCP 443).

        Caller must hold _start_lock. Returns True when the session was
        handed to a fresh http2 tunnel. The dead/unhealthy QUIC address is
        dropped so nothing can advertise or probe it any more.
        """
        if (generation != self._generation or not self._running
                or self._state != "starting" or self._tunnel_retried
                or not self._cf_path):
            return False
        self._tunnel_retried = True
        self._used_fallback = True
        self._fallback_at = time.monotonic()
        self._tunnel_registered = False
        self._public_check = None
        old, self._tunnel = self._tunnel, None
        self._url = None
        self._created = time.time()  # restart the watchdog's no-address clock
        self._info("QUIC tunnel did not become healthy; retrying with --protocol http2")
        self._tunnel_log.append("[gateway] QUIC tunnel unhealthy; retrying with --protocol http2")
        if old is not None and old is not dying:
            try:
                old.terminate()
            except Exception:
                pass
        try:
            self._start_tunnel(self._cf_path, generation, protocol="http2")
            return True
        except Exception as e:
            self._tunnel_log.append(f"[gateway] http2 retry failed to start: {e}")
            return False

    def _origin_ok(self) -> bool:
        """Health of the local gateway HTTP server over loopback (no DNS,
        no tunnel) — the part of the chain this process actually controls."""
        server = self._http
        if server is None:
            return False
        try:
            port = server.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=3) as resp:
                payload = json.loads(resp.read(4096))
                return resp.status == 200 and payload.get("ok") is True \
                    and payload.get("service") == "arena-desktop-gateway"
        except Exception:
            return False

    def _verify_registered(self, generation: int) -> None:
        """Registered tunnel + answering origin = session on. The public
        probe still runs (once, recorded for diagnostics) but no longer
        gates the session: from a machine with flaky DNS it kept failing
        for a minute and tearing down tunnels the agent could reach fine.
        """
        for _ in range(20):
            with self._start_lock:
                if generation != self._generation or not self._running:
                    return
                if self._state == "on":
                    return
            if self._origin_ok():
                with self._start_lock:
                    if generation != self._generation or not self._running \
                            or self._state != "starting" or not self._url:
                        return
                    self._state = "on"
                    self._last_error = None
                    self._ready.set()
                    self._touch()
                    url = self._url
                self._info("gateway on (registered tunnel, local origin healthy)")
                threading.Thread(target=self._public_probe_once,
                                 args=(url, generation), daemon=True).start()
                return
            time.sleep(0.5)

    def _public_probe_once(self, url: str, generation: int) -> None:
        try:
            with urllib.request.urlopen(f"{url}/v1/health", timeout=8) as resp:
                payload = json.loads(resp.read(4096))
                ok = resp.status == 200 and payload.get("ok") is True \
                    and payload.get("service") == "arena-desktop-gateway"
            result = "ok" if ok else "unexpected response"
        except Exception as e:
            result = f"failed: {type(e).__name__}"
        with self._start_lock:
            if generation == self._generation:
                self._public_check = result

    def _probe_tunnel(self, url: str, generation: int) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            with self._start_lock:
                if generation != self._generation or not self._running:
                    return
                if self._url != url:
                    return  # a fallback restart owns the session now
            try:
                with urllib.request.urlopen(f"{url}/v1/health", timeout=5) as resp:
                    payload = json.loads(resp.read(4096))
                    healthy = resp.status == 200 and payload.get("ok") is True and payload.get("service") == "arena-desktop-gateway"
                if healthy:
                    with self._start_lock:
                        if generation != self._generation or not self._running:
                            return
                        self._state = "on"
                        self._last_error = None
                        self._ready.set()
                        self._touch()
                    return
            except Exception:
                pass
            time.sleep(1.5)
        with self._start_lock:
            if generation == self._generation and self._running and self._state == "starting":
                if self._tunnel_registered:
                    return  # registered tunnels are verified on the local path
                # 60s of failed health checks on a tunnel that never came
                # up is the same QUIC failure class, just slower: fall back
                # before giving up entirely.
                if self._fallback_to_http2(generation):
                    return
                self.stop("tunnel health check failed; check cloudflared and network")

    def _watchdog_loop(self, generation: int) -> None:
        failures, last_probe = 0, 0.0
        while True:
            time.sleep(5)
            with self._start_lock:
                if generation != self._generation or not self._running:
                    return
                if self._expired():
                    self.stop("session expired")
                    return
                if self._state == "starting" and not self._url and time.time() - self._created > 60:
                    if self._fallback_to_http2(generation):
                        continue
                    self.stop("cloudflared did not provide an address")
                    return
                if self._state == "on" and time.time() - self._last_activity > self._idle_seconds:
                    self.stop("idle timeout")
                    return
                active = self._state == "on"
            if not active or time.monotonic() - last_probe < 15:
                continue
            last_probe = time.monotonic()
            # Watch the part this process controls. The public path is at
            # the mercy of the local DNS resolver (see _verify_registered)
            # and cloudflared's own exit — reported by its reader — covers
            # tunnel death; a dead origin is the remaining real failure.
            healthy = self._origin_ok()
            failures = 0 if healthy else failures + 1
            if failures >= 3:
                with self._start_lock:
                    if generation == self._generation and self._running:
                        self.stop("local gateway server stopped responding")
                return

    # ---------- tool index / jobs ----------
    def _filtered_index(self) -> Dict[str, Any]:
        index = self._tool_index() or {}
        if self._servers:
            index = {name: data for name, data in index.items() if name in self._servers}
        return index

    def _public_index(self) -> List[Dict[str, Any]]:
        out = []
        for server, data in sorted(self._index.items()):
            tools = []
            for tool, spec in sorted((data.get("tools") or {}).items()):
                tools.append({"name": tool,
                              "description": spec.get("description") or "",
                              "input_schema": spec.get("input_schema") or {},
                              "read_only": bool(spec.get("read_only"))})
            out.append({"name": server, "tools": tools})
        return out

    def _resolve(self, server: str, tool: str) -> Optional[Dict[str, Any]]:
        spec = ((self._index.get(server) or {}).get("tools") or {}).get(tool)
        if spec is None:  # servers may have started after the session opened
            self._index = self._filtered_index()
            spec = ((self._index.get(server) or {}).get("tools") or {}).get(tool)
        return spec

    def _submit_job(self, payload: Any):
        if not isinstance(payload, dict):
            return None, "job must be a JSON object"
        server = str(payload.get("server") or "")
        steps = payload.get("steps")
        if server not in self._index:
            self._index = self._filtered_index()
        if server not in self._index:
            return None, f"server not exposed in this session: {server}"
        if not isinstance(steps, list) or not steps:
            return None, "steps must be a non-empty list"
        if len(steps) > MAX_STEPS:
            return None, f"too many steps (max {MAX_STEPS})"
        clean_steps = []
        for step in steps:
            if not isinstance(step, dict) or not step.get("tool"):
                return None, "each step needs a tool name"
            clean_steps.append({"tool": str(step["tool"]),
                                "arguments": step.get("arguments") or {},
                                "continue_on_error": bool(step.get("continue_on_error"))})
        job_id = uuid.uuid4().hex[:16]
        job = {"job_id": job_id, "server": server, "status": "queued",
               "results": [], "error": None, "created_at": time.time()}
        with self._lock:
            self._jobs[job_id] = job
            self._job_events[job_id] = threading.Event()
            while len(self._jobs) > 50:
                oldest = next(iter(self._jobs))
                self._jobs.pop(oldest, None)
                self._job_events.pop(oldest, None)
        with self._job_cv:
            self._job_queue.append((job, clean_steps))
            self._job_cv.notify()
        self._touch()
        return job_id, None

    def wait_job(self, job_id: str, seconds: float) -> Optional[Dict[str, Any]]:
        """Block until the job finishes or the timeout passes; returns the
        job either way (None if unknown). This is what makes the sync
        ``?wait=`` mode possible: one agent request, one response with the
        result, instead of a poll loop of separate agent tool calls."""
        with self._lock:
            event = self._job_events.get(job_id)
            job = self._jobs.get(job_id)
        if job is None:
            return None
        if event is not None and seconds > 0:
            event.wait(min(float(seconds), MAX_WAIT_SECONDS))
        return job

    def _signal_job(self, job: Dict[str, Any]) -> None:
        with self._lock:
            event = self._job_events.get(job.get("job_id"))
        if event is not None:
            event.set()

    def _worker_loop(self, generation: int) -> None:
        while self._running and generation == self._generation:
            with self._job_cv:
                while self._running and generation == self._generation and not self._job_queue:
                    self._job_cv.wait(timeout=2)
                if not self._running or generation != self._generation:
                    return
                if not self._job_queue:
                    continue
                job, steps = self._job_queue.popleft()
            self._run_job(job, steps, generation)

    def _run_job(self, job: Dict[str, Any], steps: List[Dict[str, Any]], generation: int) -> None:
        job["status"] = "running"
        server = job["server"]
        for step in steps:
            if not self._running or generation != self._generation:
                job["status"] = "error"
                job["error"] = "gateway session ended"
                self._signal_job(job)
                return
            self._touch()
            started = time.time()
            tool = step["tool"]
            result: Dict[str, Any] = {"tool": tool, "ok": False, "output": "", "error": None}
            spec = self._resolve(server, tool)
            if spec is None:
                result["error"] = f"tool not available on {server}: {tool}"
            elif not self._approve(server, tool, bool(spec.get("read_only")),
                                   step.get("arguments") or {}):
                result["error"] = "denied by the user (or approval timed out)"
            elif generation != self._generation or not self._running:
                job["status"] = "error"
                job["error"] = "gateway session ended"
                self._signal_job(job)
                return
            else:
                try:
                    output = self._call_tool(spec["alias"], step.get("arguments") or {})
                    result["output"] = str(output or "")[:MAX_OUTPUT]
                    result["ok"] = not result["output"].startswith(("MCP Error:", "Error: MCP tool"))
                    if not result["ok"]:
                        result["error"] = result["output"][:500]
                except Exception as e:
                    result["error"] = f"{type(e).__name__}: {e}"
            job["results"].append(result)
            self._calls.append({"ts": started, "server": server, "tool": tool,
                                "ok": bool(result["ok"]),
                                "ms": int((time.time() - started) * 1000)})
            if not result["ok"] and not step.get("continue_on_error"):
                break
        if generation != self._generation or not self._running:
            job["status"] = "error"
            job["error"] = "gateway session ended"
            self._signal_job(job)
            return
        job["status"] = "done" if all(r["ok"] for r in job["results"]) else "error"
        self._signal_job(job)
        self._touch()

    # ---------- agent-facing instructions ----------
    def injection_block(self) -> str:
        if self._state != "on" or not self._url or not self._token:
            return ""
        counts = ", ".join(
            f"{name} ({len(data.get('tools') or {})} tools)"
            for name, data in sorted(self._index.items())) or "none"
        workspace_line = ""
        if "workspace" in self._index:
            workspace_line = (
                "The 'workspace' server is the user's project folder on their own "
                "computer: all paths are relative to that folder and cannot escape "
                "it. ls/read_file/search inspect it; write_file/edit_file/mkdir/delete "
                "change it; run_cmd runs a shell command inside it. Prefer it over "
                "your own cloud workspace whenever the user talks about their files "
                "or project.\n")
        return (
            "[Arena Desktop local tools gateway]\n"
            "For this session you can run tools on the user's own computer through a "
            "temporary secure gateway the user enabled.\n"
            f"Gateway URL: {self._url}\n"
            f"Pairing code (secret — never repeat it in your replies): {self._token}\n"
            "How to use:\n"
            "1. GET {url}/v1/servers with header 'Authorization: Bearer <code>' ONCE to list "
            "servers, tools and their JSON input schemas (it does not change during the session; "
            "do not re-fetch it per job, and you may skip the health check).\n"
            "2. Run work synchronously: POST {url}/v1/jobs?wait=60 with the same header, a client "
            "timeout of at least 90 seconds, and body "
            "{\"server\": \"<name>\", \"steps\": [{\"tool\": \"<tool>\", \"arguments\": {...}}]}. "
            "The response IS the finished job (status \"done\"/\"error\", results[].output) — "
            "one call, no polling. Only if the response status is still \"running\", poll "
            "GET {url}/v1/jobs/<job_id>?wait=60 the same way.\n"
            "3. Batch aggressively: put every related step in ONE job's steps (up to 40), and "
            "for shell work prefer a single run_cmd with a compound command over many small "
            "calls. Every separate request costs you a full round trip.\n"
            f"Servers available right now: {counts}.\n"
            + workspace_line +
            "Rules: use these tools only when the task needs something on the user's "
            "computer. The user may be asked to approve individual calls in the app; "
            "if a call is denied, respect that and adjust your plan. Keep tool calls "
            "focused and batch related steps into one job.\n"
        ).replace("{url}", self._url)
