import json

from arena import contracts
from arena.web import app as web


CHAT = '12345678-1234-1234-1234-123456789abc'
A = '12345678-1234-1234-1234-123456789abd'
B = '12345678-1234-1234-1234-123456789abe'


def test_nested_webdev_event():
    value = {'payload': [{'type': 'webdev', 'event': {'event': 'preview-ready', 'previewUrl': 'https://example.arena.site/'}}]}
    item = contracts.webdev_items(value)[0]
    assert item['event'] == 'preview-ready'
    assert contracts.find_preview_url(item) == 'https://example.arena.site/'


def test_metadata_is_account_bound_and_validates_ids(monkeypatch):
    client = object()
    calls = []
    monkeypatch.setattr(web, '_arena_get_json_with_client', lambda c, path, timeout: (calls.append((c, path)) or {'status': 'ready', 'previewUrl': 'https://a.arena.site/'}, None, 200))
    assert web._webdev_metadata(client, '../bad') is None
    assert web._webdev_metadata(client, A) == 'https://a.arena.site/'
    assert calls == [(client, '/api/evaluation/webdev/' + A)]


def test_collect_pending_then_ready_and_store_by_lane(monkeypatch):
    counts = {}
    def metadata(client, mid):
        counts[mid] = counts.get(mid, 0) + 1
        return None if counts[mid] == 1 else f'https://{mid}.arena.site/'
    monkeypatch.setattr(web, '_webdev_metadata', metadata)
    monkeypatch.setattr(web.time, 'sleep', lambda _: None)
    monkeypatch.setattr(web, '_stopped', lambda: False)
    conv = {'last_arena_ids': {'a': A, 'b': B}}
    events = list(web._collect_webdev_previews(object(), conv))
    assert len([x for x in events if '"type": "preview"' in x]) == 2
    assert {x['lane'] for x in conv['preview_artifacts']} == {'a', 'b'}
    assert counts == {A: 2, B: 2}


def test_recheck_saves_latest_answer_and_blocks_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'CHATS_DIR', tmp_path)
    monkeypatch.setattr(web, '_accounts_load', lambda: {'accounts': [], 'current': None})
    monkeypatch.setattr(web, 'get_client', lambda: object())
    monkeypatch.setattr(web, '_webdev_metadata', lambda client, mid: 'https://ready.arena.site/')
    web._save_chat(CHAT, 'Web', [{'role': 'user', 'text': 'build'}, {'role': 'assistant', 'text': 'done', 'text_b': '', 'arena_message_ids': {'a': A}}], mode='direct', modality='code')
    client = web.app.test_client()
    response = client.post(f'/api/chats/{CHAT}/webdev-recheck', json={})
    assert response.status_code == 200
    assert response.get_json()['previews'][0]['lane'] == 'a'
    assert web._load_chat(CHAT)['messages'][-1]['artifacts'][0]['url'] == 'https://ready.arena.site/'
    assert client.post(f'/api/chats/{CHAT}/webdev-recheck', json={}).get_json()['previews'] == []
    monkeypatch.setattr(web, '_chat_generating', lambda _: True)
    assert client.post(f'/api/chats/{CHAT}/webdev-recheck', json={}).status_code == 409


def test_recheck_merges_into_newer_snapshot_and_keeps_new_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'CHATS_DIR', tmp_path)
    monkeypatch.setattr(web, '_accounts_load', lambda: {'accounts': [], 'current': None})
    monkeypatch.setattr(web, 'get_client', lambda: object())
    old = [{'role': 'user', 'text': 'build'}, {'role': 'assistant', 'text': 'done', 'arena_message_ids': {'a': A}}]
    web._save_chat(CHAT, 'Web', old, mode='direct', modality='code')
    def metadata(client, mid):
        newer = old + [{'role': 'user', 'text': 'continue'}]
        web._save_chat(CHAT, 'New title', newer, mode='direct', modality='code')
        return 'https://ready.arena.site/'
    monkeypatch.setattr(web, '_webdev_metadata', metadata)
    response = web.app.test_client().post(f'/api/chats/{CHAT}/webdev-recheck', json={})
    latest = web._load_chat(CHAT)
    assert response.get_json()['previews']
    assert latest['title'] == 'New title'
    assert latest['messages'][-1]['text'] == 'continue'
    assert latest['messages'][1]['artifacts'][0]['url'] == 'https://ready.arena.site/'
