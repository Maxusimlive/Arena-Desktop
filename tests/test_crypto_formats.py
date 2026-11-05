import base64
from arena import crypto


def test_cookie_formats_and_fail_closed(monkeypatch):
    monkeypatch.setattr(crypto, 'DPAPI_AVAILABLE', True)
    monkeypatch.setattr(crypto, '_dpapi_protect', lambda value: value[::-1])
    monkeypatch.setattr(crypto, '_dpapi_unprotect', lambda value: value[::-1])
    value = 'session=abc; text=normal'
    current = crypto.encrypt(value.encode())
    assert current.startswith(b'ARENA-DPAPI-v1:')
    assert crypto.decrypt(current).decode() == value
    upstream = base64.b64encode(b'DPAPI:' + value.encode()[::-1])
    assert crypto.decrypt(upstream).decode() == value
    assert crypto.decrypt(value.encode()).decode() == value
    assert crypto.decrypt(b'dGVzdA==') == b'dGVzdA=='  # plaintext is not silently changed
    monkeypatch.setattr(crypto, '_dpapi_unprotect', lambda data: (_ for _ in ()).throw(OSError('unavailable')))
    assert crypto.decode_cookie(upstream.decode()) == ''
    assert crypto.decode_cookie(current.decode()) == ''
