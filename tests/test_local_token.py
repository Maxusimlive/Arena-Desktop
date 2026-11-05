from arena.web import app as web


def test_api_needs_instance_api_token_and_origin_is_still_enforced(monkeypatch):
    client = web.app.test_client()
    client.environ_base.pop('HTTP_X_ARENA_TOKEN')
    assert client.get('/api/permission-mode').status_code == 403
    assert client.post('/api/settings', json={}, headers={'X-Arena-Token': 'wrong'}).status_code == 403
    assert client.get('/api/permission-mode', headers={'X-Arena-Token': web._API_TOKEN}).status_code != 403
    assert client.post('/api/settings', json={}, headers={'X-Arena-Token': web._API_TOKEN,
                       'Origin': 'https://evil.example'}).status_code == 403
    monkeypatch.setattr(web, 'INSTANCE_TOKEN', 'instance-secret')
    monkeypatch.setattr(web, 'DEEP_LINK_HOOK', lambda x: True)
    headers = {'X-Arena-Instance-Token': 'instance-secret'}
    assert client.get('/api/permission-mode', headers=headers).status_code == 403
    assert client.post('/api/deep-link', json={'url': 'arenadesktop://open'}, headers=headers).status_code == 200
    assert client.post('/api/deep-link', json={'url': 'arenadesktop://open'},
                       headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403


def test_ui_page_contains_instance_token_but_static_file_does_not():
    client = web.app.test_client()
    assert web._API_TOKEN.encode() in client.get('/').data
    assert web._API_TOKEN.encode() not in client.get('/static/index.html').data
