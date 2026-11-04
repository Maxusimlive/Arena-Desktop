"""Current-user DPAPI protection for Arena session cookies at rest.

The stored form is deliberately explicit::

    ARENA-DPAPI-v1:<base64 DPAPI ciphertext>

That prefix matters for migration.  Values written by older Arena Desktop
versions are ordinary cookie strings; they are recognised by the absence of
the prefix and returned unchanged.  They are never fed through a permissive
base64 decoder, which could silently turn a real cookie into different
bytes.  On non-Windows systems there is no DPAPI, so values remain plaintext
there and a DPAPI value fails closed instead of being sent to Arena.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import ctypes.wintypes
import sys

PREFIX = "ARENA-DPAPI-v1:"
DPAPI_AVAILABLE = sys.platform == "win32"


class _Blob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, max(1, len(data)))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi_protect(data: bytes) -> bytes:
    if not DPAPI_AVAILABLE:
        raise OSError("Windows DPAPI is not available on this platform")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in, _buffer = _blob(data)
    blob_out = _Blob()
    if not crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out)):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    if not DPAPI_AVAILABLE:
        raise OSError("Windows DPAPI is not available on this platform")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in, _buffer = _blob(data)
    blob_out = _Blob()
    if not crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out)):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def is_protected(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def protect_text(value: str) -> str:
    """Return the versioned DPAPI form, or plaintext off Windows."""
    text = str(value or "")
    if not text or is_protected(text) or not DPAPI_AVAILABLE:
        return text
    encrypted = _dpapi_protect(text.encode("utf-8"))
    return PREFIX + base64.b64encode(encrypted).decode("ascii")


def unprotect_text(value: str) -> str:
    """Decode a versioned DPAPI value; legacy plaintext is unchanged."""
    text = str(value or "")
    if not is_protected(text):
        return text
    try:
        encrypted = base64.b64decode(text[len(PREFIX):], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid DPAPI-protected value") from exc
    return _dpapi_unprotect(encrypted).decode("utf-8")


def decode_cookie(value: object) -> str:
    """Best-effort cookie decoding that fails closed for protected data."""
    text = str(value or "")
    if not is_protected(text):
        return text
    try:
        return unprotect_text(text)
    except Exception:
        return ""
