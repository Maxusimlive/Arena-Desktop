"""Opt-in Chromium regressions for v5.8. No real Arena account is used."""
import json
import os
import threading

import pytest

pytestmark = pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS') != '1', reason='optional browser tests')


@pytest.fixture(scope='module')
def site():
    from werkzeug.serving import make_server
    from arena.web.app import app
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    thread.join(timeout=3)


def test_instance_token_and_scriptless_workspace_preview(site):
    sync_api = pytest.importorskip('playwright.sync_api')
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1400, 'height': 950})
        errors = []
        page.on('pageerror', lambda err: errors.append(str(err)))
        page.goto(site + '/?demo=1', wait_until='networkidle')
        result = page.evaluate('''async () => {
          const token = document.querySelector('meta[name="api-token"]').content;
          const denied = await fetch('/api/permission-mode');
          const allowed = await fetch('/api/permission-mode', {headers: {'X-Arena-Token': token}});
          return {denied: denied.status, allowed: allowed.status};
        }''')
        assert result == {'denied': 403, 'allowed': 200}
        page.locator('#workspaceTree').get_by_text('page.html').click()
        frame = page.locator('iframe.file-preview-frame')
        frame.wait_for(timeout=10000)
        assert frame.get_attribute('sandbox') == ''
        assert "default-src 'none'" in frame.get_attribute('srcdoc')
        assert not errors
        browser.close()


def test_native_children_show_and_can_stop(site):
    sync_api = pytest.importorskip('playwright.sync_api')
    parent = '12345678-1234-4234-8234-123456789abc'
    child = '12345678-1234-4234-8234-123456789abd'
    chat = {'id': parent, 'title': 'Parent agent', 'mode': 'agent', 'modality': 'text', 'agent_backend': 'arena', 'messages': []}
    stopped = []
    answers = []
    calls = []
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1400, 'height': 950})
        errors = []
        page.on('pageerror', lambda err: errors.append(str(err)))
        def api(route):
            path = route.request.url.split('?', 1)[0].split('/api/', 1)[1]
            if path == f'chats/{parent}/native-subagents':
                calls.append(route.request.headers.get('x-arena-token'))
                if route.request.method == 'DELETE':
                    stopped.append(True)
                    return route.fulfill(json={'cancelled': 1})
                return route.fulfill(json={'children': [] if stopped else [{'child_id': child, 'state': 'running', 'task_path': 'tasks/check.txt'}]})
            if path == 'questions/question-1' and route.request.method == 'POST':
                answers.append(route.request.post_data_json)
                return route.fulfill(json={'ok': True})
            pending = [] if answers else [{'id': 'question-1', 'chat_id': parent, 'question': 'Which approach?', 'options': ['Safe', 'Fast'], 'custom': True}]
            values = {'status': {'authenticated': True, 'mode': 'agent', 'modality': 'text', 'desktop': True, 'permission_mode': 'default'},
                      'chats': [chat], f'chats/{parent}': chat, 'history': [], 'models': [], 'permissions': [],
                      'permission-mode': {'mode': 'default'}, 'questions': pending, 'media-choices': [], 'workspace': {}}
            return route.fulfill(status=200, content_type='application/json', body=json.dumps(values.get(path, {'ok': True})))
        page.route('**/api/**', api)
        page.goto(site, wait_until='networkidle')
        page.get_by_role('button', name='Open chat: Parent agent').click()
        page.get_by_text('tasks/check.txt · running').wait_for(timeout=10000)
        question = page.locator('[data-question-card="question-1"]')
        question.get_by_text('Which approach?').wait_for(timeout=10000)
        question.get_by_role('button', name='Safe').click()
        assert answers == [{'answer': 'Safe'}]
        page.get_by_role('button', name='Stop sub-agents').click()
        page.wait_for_function('() => !document.body.innerText.includes("tasks/check.txt · running")', timeout=10000)
        assert stopped and calls and all(calls)
        assert not errors
        browser.close()


def test_all_installed_no_startup_prompt_and_check_again_refreshes(site):
    sync_api = pytest.importorskip('playwright.sync_api')
    seen = []
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1400, 'height': 950})
        def api(route):
            url = route.request.url
            path = url.split('?', 1)[0].split('/api/', 1)[1]
            if path == 'components':
                seen.append(url)
                comps = [{'id': 'cloudflared', 'name': 'cloudflared', 'installed': True, 'version': '1.0'},
                         {'id': 'node', 'name': 'Node.js', 'installed': True, 'version': 'v20'}]
                return route.fulfill(json={'components': comps, 'jobs': {}})
            values = {'status': {'authenticated': True, 'desktop': True, 'mode': 'direct', 'modality': 'text'},
                      'settings': {'gateway_enabled': False}, 'chats': [], 'models': [], 'permissions': [],
                      'permission-mode': {'mode': 'default'}, 'questions': [], 'media-choices': [], 'workspace': {}}
            return route.fulfill(status=200, content_type='application/json', body=json.dumps(values.get(path, {'ok': True})))
        page.route('**/api/**', api)
        page.goto(site, wait_until='networkidle')
        page.wait_for_timeout(1200)
        assert page.locator('#installCard').count() == 0  # nothing missing -> no prompt
        page.locator('[data-action="settings"]').first.click()
        page.locator('#settings-tab-components').click()
        panel = page.locator('.settings-panels')
        panel.locator('.component-row', has_text='cloudflared').get_by_text('Installed').wait_for(timeout=10000)
        seen.clear()
        panel.get_by_role('button', name='Check again').click()
        page.wait_for_function("() => document.querySelector('.settings-panels').innerText.includes('Installed · 1.0')", timeout=10000)
        assert any('refresh=1' in u for u in seen), seen
        browser.close()


def test_gateway_panel_shows_cloudflared_output_after_failure(site):
    sync_api = pytest.importorskip('playwright.sync_api')
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1400, 'height': 950})
        def api(route):
            path = route.request.url.split('?', 1)[0].split('/api/', 1)[1]
            if path == 'gateway':
                return route.fulfill(json={
                    'enabled': True, 'auto': True, 'chosen': [], 'available': [],
                    'workspace': {'exposed': True, 'available': True, 'folder': 'C:/work'},
                    'session': {'state': 'off', 'url': None, 'servers': [],
                                'error': 'cloudflared exited (code 1): ERR quic blocked',
                                'tunnel_log': ['ERR quic blocked', '[gateway] retrying with --protocol http2',
                                               'ERR http2 also blocked']}})
            values = {'status': {'authenticated': True, 'desktop': True, 'mode': 'direct', 'modality': 'text'},
                      'components': {'components': [{'id': 'cloudflared', 'name': 'cloudflared', 'installed': True}], 'jobs': {}},
                      'settings': {}, 'chats': [], 'models': [], 'permissions': [],
                      'permission-mode': {'mode': 'default'}, 'questions': [], 'media-choices': [], 'workspace': {}}
            return route.fulfill(status=200, content_type='application/json', body=json.dumps(values.get(path, {'ok': True})))
        page.route('**/api/**', api)
        page.goto(site, wait_until='networkidle')
        page.locator('[data-action="settings"]').first.click()
        page.locator('#settings-tab-gateway').click()
        panel = page.locator('.settings-panels')
        panel.get_by_text('cloudflared exited (code 1): ERR quic blocked', exact=False).wait_for(timeout=10000)
        panel.locator('.tunnel-log summary').click()
        assert 'http2 also blocked' in panel.locator('.tunnel-log pre').inner_text()
        browser.close()


def test_mcp_signature_on_bash_card_and_subagent_chat_delete(site):
    sync_api = pytest.importorskip('playwright.sync_api')
    deleted = []
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1400, 'height': 950})
        cmd = 'python3 gw.py job("agent-desktop",[{"tool":"desktop_launch","arguments":{}},{"tool":"desktop_screenshot","arguments":{}}])'
        def api(route):
            path = route.request.url.split('?', 1)[0].split('/api/', 1)[1]
            if path == 'chats/c1/delete':
                deleted.append(path)
                return route.fulfill(json={'ok': True, 'arena': 'none'})
            if path == 'chats':
                kids = [] if deleted else [{'id': 'c1', 'title': 'Sub child', 'mode': 'agent'}]
                return route.fulfill(json={'chats': [{'id': 'm1', 'title': 'MCP chat', 'mode': 'agent',
                                                      'children': kids}]})
            if path == 'chats/m1':
                return route.fulfill(json={'id': 'm1', 'title': 'MCP chat', 'mode': 'agent',
                                           'children': [{'id': 'c1', 'title': 'Sub child', 'mode': 'agent'}]})
            if path == 'history':
                return route.fulfill(json={'messages': [
                    {'role': 'user', 'text': 'go'},
                    {'role': 'assistant', 'text': 'working', 'text_b': '', 'sandbox': [
                        {'name': 'bash', 'command': cmd, 'ok': True}]}]})
            values = {'status': {'authenticated': True, 'desktop': True, 'mode': 'agent', 'modality': 'text'},
                      'components': {'components': [{'id': 'cloudflared', 'name': 'cloudflared', 'installed': True}], 'jobs': {}},
                      'settings': {}, 'models': [], 'permissions': [],
                      'permission-mode': {'mode': 'default'}, 'questions': [], 'media-choices': [], 'workspace': {},
                      'chats/m1/native-subagents': {'children': []}}
            return route.fulfill(status=200, content_type='application/json', body=json.dumps(values.get(path, {'ok': True})))
        page.route('**/api/**', api)
        page.goto(site, wait_until='networkidle')
        page.locator('[data-chat-open="m1"]').first.click()
        mcp = page.locator('.activity-mcp')
        mcp.wait_for(timeout=10000)
        assert 'agent-desktop · desktop_launch' in mcp.inner_text()
        assert 'desktop_screenshot' in mcp.inner_text()
        # Deleting the nested sub-agent chat opens the dialog and calls the API.
        page.locator('[data-chat-delete="c1"]').dispatch_event('click')
        page.get_by_text('Delete chat?').wait_for(timeout=10000)
        page.get_by_role('button', name='Delete', exact=True).click()
        page.wait_for_function("() => !document.body.innerText.includes('Sub child')", timeout=10000)
        assert deleted == ['chats/c1/delete']
        browser.close()


def test_first_run_lists_components_and_only_installs_when_clicked(site):
    sync_api = pytest.importorskip('playwright.sync_api')
    installs = []
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1400, 'height': 950})
        def api(route):
            path = route.request.url.split('?', 1)[0].split('/api/', 1)[1]
            if path == 'components/install':
                installs.append(route.request.post_data_json)
                return route.fulfill(json={'ok': True})
            values = {'status': {'authenticated': True, 'desktop': True, 'mode': 'direct', 'modality': 'text'},
                      'components': {'components': [{'id': 'cloudflared', 'name': 'cloudflared', 'installed': False},
                                                    {'id': 'node', 'name': 'Node.js', 'installed': True}]},
                      'settings': {'gateway_enabled': False}, 'chats': [], 'models': [], 'permissions': [],
                      'permission-mode': {'mode': 'default'}, 'questions': [], 'media-choices': [], 'workspace': {}}
            return route.fulfill(status=200, content_type='application/json', body=json.dumps(values.get(path, {'ok': True})))
        page.route('**/api/**', api)
        page.goto(site, wait_until='networkidle')
        card = page.locator('#installCard')
        card.get_by_text('Components for Arena Desktop').wait_for(timeout=10000)
        assert 'cloudflared' in card.inner_text() and 'Node.js' in card.inner_text()
        assert not installs
        card.locator('.component-row', has_text='cloudflared').get_by_role('button', name='Install').click()
        page.wait_for_function("() => document.querySelector('#installCard')?.innerText.includes('Installing cloudflared')", timeout=10000)
        assert installs == [{'id': 'cloudflared'}]
        browser.close()
