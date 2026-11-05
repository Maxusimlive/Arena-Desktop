"""Bounded asynchronous native-agent scheduling.

Coordination is intentionally metadata-only. The parent and children exchange
work and results through workspace files; status never contains model output,
credentials, or a gateway URL. All mutable scheduler state is process-local.
"""
from __future__ import annotations

import threading
import uuid
from typing import Callable


class NativeSubagentPool:
    def __init__(self, run: Callable, limit: int = 6):
        self._run = run
        self.limit = limit
        self._lock = threading.Lock()
        self._jobs = {}

    def launch(self, parent: str, task_path: str, context: dict) -> dict:
        if not parent or not isinstance(task_path, str) or not task_path.strip():
            raise ValueError("A parent and workspace task file are required")
        with self._lock:
            active = sum(job['state'] in ('starting', 'running') for job in self._jobs.values())
            if active >= self.limit:
                raise RuntimeError(f"Sub-agent limit reached ({self.limit} concurrent)")
            child = str(uuid.uuid4())
            job = {'child_id': child, 'parent': parent, 'state': 'starting',
                   'task_path': task_path, 'result_path': None, 'error': None,
                   'cancel': threading.Event()}
            self._jobs[child] = job
            thread = threading.Thread(target=self._drive, args=(job, context), daemon=True)
            try:
                thread.start()
            except Exception:
                del self._jobs[child]
                raise
        return {'child_id': child, 'state': 'starting'}

    def _drive(self, job, context):
        with self._lock:
            if job['cancel'].is_set():
                job['state'] = 'cancelled'
                return
            job['state'] = 'running'
        try:
            result_path = self._run(job['child_id'], job['task_path'], context, job['cancel'])
            with self._lock:
                if job['cancel'].is_set():
                    job['state'] = 'cancelled'
                else:
                    job['result_path'] = result_path
                    job['state'] = 'done'
        except Exception as exc:
            with self._lock:
                job['state'] = 'cancelled' if job['cancel'].is_set() else 'error'
                # Do not expose arbitrary exception text (may contain URLs or tokens).
                job['error'] = type(exc).__name__

    def status(self, parent: str) -> list:
        with self._lock:
            return [{key: job[key] for key in ('child_id', 'state', 'task_path', 'result_path', 'error')}
                    for job in self._jobs.values() if job['parent'] == parent]

    def cancel_all(self) -> int:
        with self._lock:
            jobs = [job for job in self._jobs.values() if job['state'] in ('starting', 'running')]
            for job in jobs:
                job['cancel'].set()
            return len(jobs)

    def cancel_parent(self, parent: str) -> int:
        with self._lock:
            jobs = [job for job in self._jobs.values()
                    if job['parent'] == parent and job['state'] in ('starting', 'running')]
            for job in jobs:
                job['cancel'].set()
            return len(jobs)
