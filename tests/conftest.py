"""Test clients carry the same per-instance token as the desktop UI."""
import pytest


@pytest.fixture(autouse=True)
def authenticated_test_client(monkeypatch):
    from arena.web import app as web
    original = web.app.test_client

    def create(*args, **kwargs):
        client = original(*args, **kwargs)
        client.environ_base['HTTP_X_ARENA_TOKEN'] = web._API_TOKEN
        return client

    monkeypatch.setattr(web.app, 'test_client', create)
