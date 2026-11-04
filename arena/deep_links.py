"""Safe ``arenadesktop://`` links and running-instance handoff.

Supported links intentionally do very little:

- ``arenadesktop://open``  - show Arena Desktop
- ``arenadesktop://new``   - open a new, empty chat
- ``arenadesktop://ask?q=...`` - put text in a new chat's composer
- ``arenadesktop://chat?id=<uuid>`` - open an existing local chat

An ``ask`` link never sends the message.  Any application can ask Windows
to open a custom protocol, so following a link must not spend Arena usage
or start tool work without the user pressing Send in Arena Desktop.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from arena.config import CONFIG_DIR

SCHEME = "arenadesktop"
ACTIONS = {"open", "new", "ask", "chat"}
MAX_ASK_CHARS = 10000
_INSTANCE_FILE = CONFIG_DIR / "instance.json"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def parse_deep_link(raw: Any) -> Dict[str, Any]:
    """Validate and normalize one deep link, or raise ``ValueError``."""
    if isinstance(raw, dict):
        # The local handoff endpoint accepts the URL form, but accepting an
        # already-decoded mapping makes the desktop bridge and tests simple.
        action = str(raw.get("action") or "").strip().lower()
        params = {str(k): [str(v)] for k, v in raw.items() if k != "action"}
    else:
        text = str(raw or "").strip()
        if not text or len(text) > MAX_ASK_CHARS + 4096:
            raise ValueError("invalid Arena Desktop link")
        parsed = urllib.parse.urlparse(text)
        if parsed.scheme.lower() != SCHEME:
            raise ValueError("invalid Arena Desktop link scheme")
        action = (parsed.netloc or parsed.path.lstrip("/").split("/", 1)[0]).lower()
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        # A chat id in the path is a convenience form:
        # arenadesktop://chat/<uuid>
        if action == "chat" and parsed.netloc.lower() == "chat" and parsed.path:
            candidate = parsed.path.strip("/")
            if candidate and "id" not in params:
                params["id"] = [candidate]
    if action not in ACTIONS:
        raise ValueError("unsupported Arena Desktop link")

    def one(*names: str) -> str:
        for name in names:
            values = params.get(name)
            if values:
                return str(values[0])
        return ""

    if action in ("open", "new"):
        return {"action": action}
    if action == "ask":
        text = one("q", "text", "prompt").strip()
        if not text:
            raise ValueError("ask link needs q=<text>")
        if len(text) > MAX_ASK_CHARS:
            raise ValueError("ask link text is too long")
        return {"action": "ask", "text": text}

    chat_id = one("id", "chat_id", "chatId").strip()
    if not _UUID_RE.match(chat_id):
        raise ValueError("chat link needs a valid chat id")
    result: Dict[str, Any] = {"action": "chat", "chat_id": chat_id}
    msg = one("msg", "msg_index", "message").strip()
    if msg:
        try:
            index = int(msg)
        except ValueError as exc:
            raise ValueError("chat link message index is invalid") from exc
        if index < 0 or index > 1000000:
            raise ValueError("chat link message index is invalid")
        result["msg_index"] = index
    return result


def find_deep_link_arg(argv: Iterable[str]) -> Optional[str]:
    for value in argv:
        if isinstance(value, str) and value.lower().startswith(f"{SCHEME}://"):
            return value
    return None


def instance_path() -> Path:
    return _INSTANCE_FILE


def new_instance_token() -> str:
    return secrets.token_urlsafe(32)


def write_instance(port: int, token: str) -> None:
    """Publish this process as the deep-link target (atomic replace)."""
    _INSTANCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "port": int(port),
        "pid": os.getpid(),
        "token": str(token),
        "executable": sys.executable,
    }
    tmp = _INSTANCE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, _INSTANCE_FILE)


def read_instance() -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(_INSTANCE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        data["port"] = int(data.get("port") or 0)
        data["pid"] = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if not (0 < data["port"] < 65536 and data["pid"] > 0 and data.get("token")):
        return None
    return data


def pid_alive(pid: int) -> bool:
    if not pid or pid == os.getpid():
        return pid == os.getpid()
    if sys.platform == "win32":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information, False, int(pid))
            if not handle:
                # Access denied still means a process exists.
                return ctypes.GetLastError() == 5
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def forward_deep_link(raw: str) -> bool:
    """Send a validated link to the already-running app, if there is one."""
    try:
        payload = parse_deep_link(raw)
    except ValueError:
        return False
    instance = read_instance()
    if not instance or not pid_alive(int(instance["pid"])):
        return False
    try:
        import requests

        response = requests.post(
            f"http://127.0.0.1:{instance['port']}/api/deep-link",
            json={"link": payload},
            headers={"X-Arena-Instance-Token": str(instance["token"])},
            timeout=3,
        )
        return bool(response.ok and (response.json() or {}).get("ok"))
    except Exception:
        return False


def register_protocol(executable: Optional[str] = None) -> bool:
    """Register ``arenadesktop://`` for the current Windows user.

    HKCU needs no administrator rights and points at this exact EXE, so a
    portable build can register itself on first launch.  Source runs do
    not register anything.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    exe = str(Path(executable or sys.executable).resolve())
    try:
        import winreg

        root = winreg.HKEY_CURRENT_USER
        base = rf"Software\Classes\{SCHEME}"
        with winreg.CreateKeyEx(root, base, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, "URL:Arena Desktop Protocol")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKeyEx(root, base + r"\DefaultIcon", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, f'"{exe}",0')
        with winreg.CreateKeyEx(root, base + r"\shell\open\command", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, f'"{exe}" "%1"')
        return True
    except Exception:
        return False
