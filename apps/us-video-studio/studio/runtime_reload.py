"""Idle-only local code reloads through the normal server lifecycle.

The supervisor launches only checked doctor workers. A worker asks for another
launch only after closing job admission and gracefully releasing its lease.
Crashes, manual stops and startup errors never trigger an automatic restart.
Each new worker rebuilds the app from disk; live modules are never patched.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import sys
import time
from pathlib import Path

from .doctor import _fingerprint_paths, code_fingerprint

RELOAD_EXIT_CODE = 75
RELOAD_HTTP_DRAIN_SECONDS = 15
logger = logging.getLogger('uvicorn.error')


def validate_code() -> bool:
    """Parse source only; do not import changed modules or run user code."""
    try:
        for path in _fingerprint_paths():
            if path.suffix == '.py':
                compile(path.read_bytes(), str(path), 'exec')
    except (SyntaxError, ValueError, OSError):
        return False
    return True


class ReloadWatch:
    def __init__(self, jobs, runtime_info, *, fingerprint=code_fingerprint,
                 validate=validate_code, settle_seconds=2.0):
        self.jobs = jobs
        self.loaded = runtime_info['code_fingerprint']
        self.fingerprint, self.validate = fingerprint, validate
        self.settle_seconds = settle_seconds
        self.pending = None
        self.since = 0.0
        self.validated = None
        self.reloading = False
        self.state = runtime_info['auto_reload'] = {'enabled': True, 'status': 'watching'}

    def tick(self, now):
        try:
            current = self.fingerprint()
        except OSError:
            self.state['status'] = 'check_failed'
            self.validated = None
            self.since = now
            return False
        if current == self.loaded:
            self.pending = self.validated = None
            self.state.clear()
            self.state.update(enabled=True, status='watching')
            return False
        if current != self.pending:
            self.pending, self.since, self.validated = current, now, None
            self.state.update(status='settling', pending_code_fingerprint=current)
            return False
        if now - self.since < self.settle_seconds:
            return False
        if self.validated != current:
            if not self.validate():
                if self.state['status'] != 'invalid_code':
                    logger.warning('Code reload deferred: changed source did not pass syntax checks.')
                self.state['status'] = 'invalid_code'
                return False
            self.validated = current
        if not self.jobs.begin_reload():
            self.state['status'] = 'waiting_for_idle'
            return False
        self.reloading = True
        self.state['status'] = 'reloading'
        logger.info('Code changed; idle Studio is reloading through normal shutdown.')
        return True


def serve(app, port, *, auto_reload):
    import uvicorn

    app.state.runtime_info['auto_reload'] = {'enabled': False, 'status': 'disabled'}
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='info'))
    watch = ReloadWatch(app.state.jobs, app.state.runtime_info) if auto_reload else None

    async def monitor():
        while not server.should_exit:
            if server.started and await asyncio.to_thread(watch.tick, time.monotonic()):
                # begin_reload has already atomically closed admission after
                # all paid jobs finish. Only residual HTTP requests are bounded;
                # manual shutdown keeps uvicorn's original drain policy.
                server.config.timeout_graceful_shutdown = RELOAD_HTTP_DRAIN_SECONDS
                server.should_exit = True
                return
            await asyncio.sleep(0.5)

    async def run():
        task = asyncio.create_task(monitor()) if watch else None
        try:
            await server.serve()
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    asyncio.run(run())
    return RELOAD_EXIT_CODE if watch and watch.reloading else 0


def supervise(data_root: Path, port: int):
    """Keep a stable launcher; never terminate a worker or retry a failed one."""
    command = [sys.executable, '-m', 'studio.doctor', '--data-root', str(data_root),
               '--port', str(port), '--start', '--reload-worker']
    while True:
        result = subprocess.call(command, stdout=sys.stdout, stderr=sys.stderr,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result != RELOAD_EXIT_CODE:
            return result
