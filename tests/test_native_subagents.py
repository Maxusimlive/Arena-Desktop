import threading
import time

import pytest

from arena.native_subagents import NativeSubagentPool


def wait_until(fn, timeout=3):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if fn():
            return
        time.sleep(.01)
    raise AssertionError('condition did not occur')


def test_six_run_concurrently_parent_can_continue_and_seventh_is_rejected():
    release = threading.Event()
    started = []
    lock = threading.Lock()
    def run(child, task_path, context, cancel):
        with lock:
            started.append(child)
        assert release.wait(3)
        return f'.arena-bridge/{child}.result.txt'
    pool = NativeSubagentPool(run)
    try:
        jobs = [pool.launch('parent', f'tasks/{i}.txt', {}) for i in range(6)]
        wait_until(lambda: len(started) == 6)
        assert len({job['child_id'] for job in jobs}) == 6
        with pytest.raises(RuntimeError, match='6 concurrent'):
            pool.launch('other', 'tasks/7.txt', {})
        assert len(pool.status('parent')) == 6
        assert pool.status('other') == []
        release.set()
        wait_until(lambda: all(x['state'] == 'done' for x in pool.status('parent')))
        assert all(x['result_path'].endswith('.result.txt') for x in pool.status('parent'))
        seventh = pool.launch('parent', 'tasks/7.txt', {})
        assert seventh['child_id'] not in {job['child_id'] for job in jobs}
    finally:
        release.set()


def test_cancel_is_parent_scoped_and_no_error_secret_leaks():
    entered = threading.Event()
    def run(child, task, context, cancel):
        entered.set()
        assert cancel.wait(3)
        raise ValueError('secret-gateway-token')
    pool = NativeSubagentPool(run)
    pool.launch('a', 'a.txt', {})
    assert entered.wait(2)
    assert pool.cancel_parent('b') == 0
    assert pool.cancel_parent('a') == 1
    wait_until(lambda: pool.status('a')[0]['state'] == 'cancelled')
    assert 'secret-gateway-token' not in str(pool.status('a'))


def test_control_requires_capability_and_confines_task_file(tmp_path, monkeypatch):
    from arena.web import app as web
    monkeypatch.setattr(web, '_chosen_ws', lambda: tmp_path)
    monkeypatch.setattr(web, '_perm_mode', lambda: 'auto')
    monkeypatch.setattr(web, '_SUBAGENT_CONTEXTS', {})
    sentinel = []
    class Pool:
        def launch(self, parent, path, context):
            sentinel.append((parent, path, context['root']))
            return {'child_id': 'test-child', 'state': 'starting'}
        def status(self, parent): return []
    monkeypatch.setattr(web, '_NATIVE_SUBAGENTS', Pool())
    handle = web._register_subagent_context('parent', object(), None)
    (tmp_path / 'task.txt').write_text('test this', encoding='utf8')
    with pytest.raises(PermissionError):
        web._subagent_control('start', {'handle': 'bad', 'task_path': 'task.txt'})
    with pytest.raises(ValueError):
        web._subagent_control('start', {'handle': handle, 'task_path': '../outside.txt'})
    output = web._subagent_control('start', {'handle': handle, 'task_path': 'task.txt'})
    assert 'test-child' in output
    assert sentinel == [('parent', 'task.txt', tmp_path.resolve())]
