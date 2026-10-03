"""Local code reload with an idle boundary; never replay a model/media request.

The supervisor and worker are separate fresh Python processes. The supervisor
owns no business writer lease; it waits for the old worker to fully exit before
starting the next one. Only backend/method files in the runtime fingerprint are
watched. Runtime data, credentials, frontend files and tests do not trigger it.
"""
from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .doctor import APP_ROOT, _fingerprint_paths, code_fingerprint

RELOAD_EXIT = 75
SUPERVISOR_LOCK = '.studio-supervisor.lock'
IDLE_HTTP_SHUTDOWN_TIMEOUT = 10.0


def validate_python():
    for path in _fingerprint_paths():
        if path.suffix == '.py':
            ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))


class IdleReload:
    def __init__(self, app, server, *, fingerprint=code_fingerprint,
                 validate=validate_python, now=time.monotonic, settle=1.0, supervisor_id=None):
        self.app, self.server = app, server
        self.fingerprint, self.validate, self.now, self.settle = fingerprint, validate, now, settle
        self.loaded = app.state.runtime_info['code_fingerprint']
        self.candidate, self.changed_at, self.checked = None, 0.0, None
        self.invalid = False
        self.reloading = False
        self.info = {'enabled': True, 'state': 'watching', 'supervisor_id': supervisor_id or os.getppid()}
        app.state.runtime_info['hot_reload'] = self.info

    def tick(self):
        current = self.fingerprint()
        if current == self.loaded:
            self.candidate = self.checked = None
            self.invalid = False
            self.info.update(state='watching', message='后端代码变更后自动更新；执行中等待本轮结束。')
            return
        if current != self.candidate:
            self.candidate, self.changed_at, self.checked = current, self.now(), None
            self.invalid = False
            self.info.update(state='settling', message='检测到代码变更，等待文件保存完成。')
            return
        if self.now() - self.changed_at < self.settle:
            return
        if self.checked != current:
            try:
                self.validate()
            except (SyntaxError, UnicodeError):
                self.checked = current
                self.invalid = True
                self.info.update(state='invalid_code', message='新代码有语法或编码错误，原服务继续运行；修复后自动更新。')
                print('[reload] Invalid Python source; keeping the loaded worker.', file=sys.stderr, flush=True)
                return
            self.checked = current
        if self.invalid:
            self.info.update(state='invalid_code', message='新代码有语法或编码错误，原服务继续运行；修复后自动更新。')
            return
        jobs, auth = self.app.state.jobs, self.app.state.codex_auth
        # Same lock as job submission and login. No idle-check / new-job race.
        with jobs.lock:
            if auth.process is not None or not jobs.close_when_idle():
                self.info.update(state='waiting_for_idle', message='新代码已就绪，等待当前制作或登录结束后自动更新。')
                return
            auth.closed = True
            self.info.update(state='reloading', message='正在加载新版本，请稍后重新连接。')
            self.reloading = True
            # Business workers and login are already idle, and their entry
            # points are closed. Bound only the remaining HTTP drain: Windows
            # Proactor disconnect errors can strand Server.wait_closed even
            # after the listening socket and all application work have ended.
            self.server.config.timeout_graceful_shutdown = IDLE_HTTP_SHUTDOWN_TIMEOUT
            self.server.should_exit = True

    def watch(self, stopped):
        while not stopped.wait(.5):
            if not self.server.started:
                continue
            try:
                self.tick()
            except OSError:
                self.info.update(state='waiting_for_files', message='等待代码文件可读取后再更新；原服务继续运行。')
            if self.reloading:
                return


def serve_worker(root, port, supervisor_id=None):
    import uvicorn
    from .config import load_settings
    from .server import build_app
    app = build_app(load_settings(root))
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='info'))
    reload = IdleReload(app, server, supervisor_id=supervisor_id)
    stopped = threading.Event()
    watcher = threading.Thread(target=reload.watch, args=(stopped,), daemon=True)
    watcher.start()
    try:
        server.run()
    finally:
        stopped.set()
        watcher.join(timeout=2)
    return RELOAD_EXIT if reload.reloading else 0 if server.started else 1


def supervise(root: Path, port: int):
    from .jobs import WriterLease
    from .models import PipelineError
    lease = WriterLease(root, name=SUPERVISOR_LOCK)
    try:
        lease.acquire()
    except PipelineError:
        print('[reload] This data root already has a supervisor; connect to it.', file=sys.stderr, flush=True)
        return 2
    try:
        while True:
            try:
                baseline = code_fingerprint()
            except OSError:
                time.sleep(.5)
                continue
            # -B prevents writes, but alone still reads existing .pyc files.
            # A fresh prefix prevents stale imports after equal-size edits in
            # the same second. No cache directory is created with -B.
            cache = Path(tempfile.gettempdir()) / ('studio-bytecode-' + uuid.uuid4().hex)
            command = [sys.executable, '-B', '-X', 'pycache_prefix=' + str(cache),
                       '-m', 'studio.dev_reload', '--worker',
                       '--data-root', str(root), '--port', str(port), '--supervisor-id', str(os.getpid())]
            print('[reload] Starting a fresh worker.', flush=True)
            child = subprocess.Popen(command, cwd=APP_ROOT)
            try:
                result = child.wait()
            except KeyboardInterrupt:
                # Ctrl+C is an explicit stop. Wait for the console signal's
                # normal shutdown rather than force-killing an in-flight job.
                child.wait()
                return 0
            if result == RELOAD_EXIT:
                continue
            if result == 0:
                return 0
            print('[reload] Worker failed. Fix the code to start a fresh worker; see the error above.',
                  file=sys.stderr, flush=True)
            # Do not hammer a broken import, configuration or provider request.
            # The supervisor stays alive, so saving the fix restores the server.
            while True:
                time.sleep(.5)
                try:
                    current = code_fingerprint()
                    if current != baseline:
                        validate_python()
                        break
                except (OSError, SyntaxError, UnicodeError):
                    continue
    except KeyboardInterrupt:
        return 0
    finally:
        lease.close()


def main():
    parser = argparse.ArgumentParser(description='Local Studio idle-safe reload worker')
    parser.add_argument('--worker', action='store_true', required=True)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=4760)
    parser.add_argument('--supervisor-id', type=int)
    args = parser.parse_args()
    raise SystemExit(serve_worker(args.data_root, args.port, args.supervisor_id))


if __name__ == '__main__':
    main()
