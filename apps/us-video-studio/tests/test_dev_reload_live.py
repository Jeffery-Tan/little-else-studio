"""Fresh real Python/uvicorn processes, copied code and disposable local data."""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from studio.doctor import APP_ROOT, mark_new_root
from studio.store import Store


def test_real_reload_retains_data_and_recovers_after_import_failure(tmp_path):
    app = tmp_path / 'repo/apps/us-video-studio'
    shutil.copytree(APP_ROOT / 'studio', app / 'studio',
                    ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path / 'repo/scripts').mkdir()
    shutil.copy2(APP_ROOT.parents[1] / 'scripts/creative_learning.py',
                 tmp_path / 'repo/scripts/creative_learning.py')
    # Seed a valid but stale timestamp-based cache. Equal-size edits in one
    # second must load actual source, rather than falsely reporting a new hash.
    probe = app / 'studio/reload_probe.py'
    probe.write_text('VALUE="first"\n', encoding='utf-8')
    fixed_time = int(time.time()) - 5
    os.utime(probe, (fixed_time, fixed_time))
    subprocess.run([sys.executable, '-m', 'py_compile', str(probe)], check=True)
    route = app / 'studio/http_execution.py'
    route.write_bytes(route.read_bytes().replace(b'return app.state.runtime_info',
        b'from .reload_probe import VALUE\n        return {**app.state.runtime_info, "reload_probe": VALUE}'))
    root = tmp_path / 'data'
    root.mkdir()
    mark_new_root(root)
    store = Store(root)
    store.create_run('reload-proof', 'Synthetic preserved project', 'No provider calls')
    preserved = {path.relative_to(root): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}/api/runtime'

    def runtime():
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.load(response)

    def wait_for(predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = runtime()
                if predicate(value):
                    return value
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(.1)
        raise AssertionError('Reload condition not reached: ' + (tmp_path / 'stderr.log').read_text(errors='replace'))

    source = app / 'studio/models.py'
    original = source.read_bytes()
    command = [sys.executable, '-B', '-c',
               'from studio.dev_reload import supervise; from pathlib import Path; import sys; '
               'sys.exit(supervise(Path(sys.argv[1]), int(sys.argv[2])))', str(root), str(port)]
    child_ids, supervisor_id = set(), None
    with (tmp_path / 'stdout.log').open('w') as out, (tmp_path / 'stderr.log').open('w') as err:
        process = subprocess.Popen(command, cwd=app, stdout=out, stderr=err)
        try:
            initial = wait_for(lambda r: r['hot_reload']['enabled'])
            child_ids.add(initial['process_id'])
            supervisor_id = initial['hot_reload']['supervisor_id']
            assert initial['reload_probe'] == 'first'
            probe.write_text('VALUE="later"\n', encoding='utf-8')
            os.utime(probe, (fixed_time, fixed_time))
            cache_fixed = wait_for(lambda r: r['process_id'] != initial['process_id'])
            child_ids.add(cache_fixed['process_id'])
            assert cache_fixed['reload_probe'] == 'later'
            source.write_bytes(original + b'\n# Synthetic reload change\n')
            updated = wait_for(lambda r: r['process_id'] != cache_fixed['process_id'])
            child_ids.add(updated['process_id'])
            assert updated['code_fingerprint'] != initial['code_fingerprint']
            assert updated['data_root_id'] == initial['data_root_id']
            source.write_bytes(original + b'\nthis is invalid python !!!\n')
            invalid = wait_for(lambda r: r['hot_reload']['state'] == 'invalid_code')
            assert invalid['process_id'] == updated['process_id']
            # A syntactically valid import failure keeps the supervisor alive.
            source.write_bytes(original + b'\nraise RuntimeError("synthetic import failure")\n')
            deadline = time.monotonic() + 12
            while 'Worker failed.' not in (tmp_path / 'stderr.log').read_text(errors='replace'):
                assert time.monotonic() < deadline
                time.sleep(.1)
            assert process.poll() is None
            source.write_bytes(original + b'\n# Fixed import\n')
            fixed = wait_for(lambda r: r['process_id'] != updated['process_id'])
            child_ids.add(fixed['process_id'])
            assert fixed['hot_reload']['supervisor_id'] == supervisor_id
            for relative, content in preserved.items():
                assert (root / relative).read_bytes() == content
            assert Store(root).read_events('reload-proof') == []
            assert Store(root).list_runs()[0].title == 'Synthetic preserved project'
        finally:
            # All these PIDs were observed from this test's isolated root. There
            # are no provider calls or in-flight jobs; no production PID is used.
            process.terminate()
            for pid in child_ids | ({supervisor_id} if supervisor_id else set()):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            process.wait(timeout=5)
