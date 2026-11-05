from arena.web import app as web


def test_automation_is_plan_even_for_legacy_bypass(monkeypatch):
    seen = []
    monkeypatch.setattr(web, '_memory_enabled', lambda: False)
    monkeypatch.setattr(web, '_save_chat', lambda *a, **k: None)
    monkeypatch.setattr(web, 'get_client', lambda: object())
    monkeypatch.setattr(web, '_model_name', lambda _: None)
    monkeypatch.setattr(web, '_notify', lambda *a: None)
    def fake_run(client, prompt, conv):
        seen.append(web._perm_mode())
        if False:
            yield None
        return {'a': 'done', 'b': ''}
    monkeypatch.setattr(web, '_agent_run', fake_run)
    try:
        web._run_automation({'name': 'legacy', 'prompt': 'review', 'permission': 'bypassPermissions'})
        assert seen == ['plan']
    finally:
        web._tl.perm_override = None
        web._tl.stop_event = None
        web._tl.chat_id = None


def test_automation_save_cannot_enable_bypass(monkeypatch):
    cfg = {}
    monkeypatch.setattr(web, 'load_config', lambda: dict(cfg))
    monkeypatch.setattr(web, 'save_config', lambda value: cfg.update(value))
    response = web.app.test_client().post('/api/automations', json={
        'name': 'a', 'prompt': 'b', 'schedule': 'hourly', 'permission': 'bypassPermissions'})
    assert response.get_json()['item']['permission'] == 'plan'
    assert cfg['automations'][0]['permission'] == 'plan'
