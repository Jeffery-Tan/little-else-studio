"""Exercise graceful reload with a real local server and a free synthetic job.

All state and the source fixture live in a temporary directory. No real model,
credentials or production run is used, and workspace source is never changed.
"""
from __future__ import annotations

import argparse
import asyncio
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.error import URLError, HTTPError
from urllib.request import build_opener, ProxyHandler

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))
RUN = 'reload-fixture'


def worker(root, port, source):
    from studio import doctor
    # Only the watched source scope is a fixture. HTTP, jobs, leases and server
    # shutdown are the actual implementation under test.
    doctor.REPOSITORY = source.parent
    doctor._fingerprint_paths = lambda: [source]
    report = doctor.inspect(root, port)
    if not report['ready_to_start']:
        raise RuntimeError('isolated startup checks failed: ' + ', '.join(
            row['name'] for row in report['checks'] if row['status'] == 'error'))
    from studio import runtime_reload
    from studio.config import load_settings
    from studio.server import build_app

    base_watch = runtime_reload.ReloadWatch
    class FixtureWatch(base_watch):
        def tick(self, now):
            if (root / 'stop').exists():
                return self.jobs.begin_reload()
            return super().tick(now)
    runtime_reload.ReloadWatch = FixtureWatch
    app = build_app(load_settings(root))
    jobs = app.state.jobs
    jobs.initialize()
    if not jobs.store.list_runs():
        jobs.store.create_run(RUN, 'free reload fixture', 'no real model')
    @app.post('/fixture-job')
    def create_fixture_job():
        def runner(text, emit, check_cancel):
            while not (root / 'finish-job').exists():
                threading.Event().wait(.05)
            check_cancel()
            (root / 'saved-result.txt').write_text('completed once', encoding='utf-8')
            return SimpleNamespace(text='fixture saved', rounds=1, used_tools=[], stopped_because='done')
        return jobs.submit(RUN, 'free synthetic long job', 'fixture-once', prepare=lambda _: (runner, {}))
    @app.get('/fixture-slow-read')
    async def slow_read():
        (root / 'read-started').touch()
        await asyncio.Event().wait()
    raise SystemExit(runtime_reload.serve(app, port, auto_reload=True))


def check():
    from studio.doctor import mark_new_root
    with tempfile.TemporaryDirectory(prefix='studio-reload-check-') as directory:
        root = Path(directory) / 'data'
        root.mkdir()
        mark_new_root(root)
        source = Path(directory) / 'source' / 'watched.py'
        source.parent.mkdir()
        source.write_text('VALUE = 1\n', encoding='utf-8')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        opener = build_opener(ProxyHandler({}))

        def read(path):
            try:
                with opener.open(f'http://127.0.0.1:{port}{path}', timeout=1) as response:
                    return json.load(response)
            except (URLError, HTTPError, TimeoutError):
                return None

        def until(get, predicate, label):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                value = get()
                if value is not None and predicate(value):
                    return value
                threading.Event().wait(.1)
            raise AssertionError(label)

        command = [sys.executable, str(Path(__file__).resolve()), '--worker',
                   '--root', str(root), '--port', str(port), '--source', str(source)]
        with (Path(directory) / 'server.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(command, cwd=APP_ROOT, stdout=log, stderr=log,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            clients = []
            try:
                before = until(lambda: read('/api/runtime'), lambda r: r['auto_reload']['enabled'], 'first startup')
                # Keep several browser-like connections open across the update.
                for _ in range(3):
                    client = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                    clients.append(client)
                    client.request('GET', '/api/runtime')
                    assert client.getresponse().read()
                # Exercise submission from the real HTTP thread pool, as the
                # workbench does, rather than before the event loop starts.
                with opener.open(f'http://127.0.0.1:{port}/fixture-job', data=b'{}', timeout=2) as reply:
                    created = json.load(reply)
                assert created['status'] in {'queued', 'running'}
                for _ in range(3):
                    client = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                    clients.append(client)
                    client.request('GET', f'/api/runs/{RUN}/jobs/{created["job_id"]}/events')
                    response = client.getresponse()
                    assert response.status == 200
                    assert response.readline().startswith(b'id: ')
                source.write_text('VALUE = 2\n', encoding='utf-8')
                waiting = until(lambda: read('/api/runtime'),
                    lambda r: r['auto_reload']['status'] == 'waiting_for_idle', 'wait for active job')
                assert waiting['started_at'] == before['started_at']
                source.write_text('def broken(:\n', encoding='utf-8')
                invalid = until(lambda: read('/api/runtime'),
                    lambda r: r['auto_reload']['status'] == 'invalid_code', 'keep old service on syntax error')
                assert invalid['code_fingerprint'] == before['code_fingerprint']
                (root / 'finish-job').touch()
                job = until(lambda: read(f'/api/runs/{RUN}/jobs/latest'),
                    lambda r: r['status'] == 'succeeded', 'complete job without cancellation')
                stalled = socket.create_connection(('127.0.0.1', port), timeout=2)
                clients.append(stalled)
                stalled.sendall(b'GET /fixture-slow-read HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
                until(lambda: (root / 'read-started').exists(), bool, 'stalled HTTP read started')
                source.write_text('VALUE = 3\n', encoding='utf-8')
                assert process.wait(timeout=20) == 75, 'clean reload exit code'
                process = subprocess.Popen(command, cwd=APP_ROOT, stdout=log, stderr=log,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                after = until(lambda: read('/api/runtime'),
                    lambda r: r['started_at'] != before['started_at'], 'fresh worker')
                assert after['data_root_id'] == before['data_root_id']
                assert after['code_fingerprint'] != before['code_fingerprint']
                saved = read(f'/api/runs/{RUN}/jobs/{job["job_id"]}')
                assert saved['job_id'] == job['job_id'] and saved['status'] == 'succeeded'
                from studio.store import Store
                assert sum(row['kind'] == 'job_created' for row in Store(root).read_events(RUN)) == 1
                assert read(f'/api/runs/{RUN}/ledger')['rows'] == []
                assert (root / 'saved-result.txt').read_text(encoding='utf-8') == 'completed once'
                print('PASS: browser connections, event streams, busy wait, syntax-error deferral, bounded stalled HTTP drain, lifecycle exit, lease release, fresh worker, preserved result, no replay or model calls')
            except Exception:
                log.flush()
                print((Path(directory) / 'server.log').read_text(encoding='utf-8'), file=sys.stderr)
                raise
            finally:
                for client in clients:
                    client.close()
                (root / 'finish-job').touch()
                (root / 'stop').touch()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    # Only this script's disposable, free fixture process.
                    process.kill()
                    process.wait(timeout=5)
                    raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--port', type=int)
    parser.add_argument('--source', type=Path)
    args = parser.parse_args()
    if args.worker:
        worker(args.root, args.port, args.source)
    else:
        check()
