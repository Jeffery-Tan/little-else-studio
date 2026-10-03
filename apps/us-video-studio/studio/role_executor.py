"""Bounded independent role workers; session exclusion is shared per process."""
from __future__ import annotations

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from contextvars import ContextVar, copy_context
import threading

from .models import PipelineError

_DEPTH = ContextVar('studio_role_depth', default=0)


class RoleExecutor:
    def __init__(self, max_workers=4, max_pending=8, max_depth=3):
        self.max_depth = max_depth
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='studio-role')
        self.capacity = threading.BoundedSemaphore(max_pending)
        self.lock = threading.Lock()
        self.active = {}

    def submit(self, key, function):
        depth = _DEPTH.get() + 1
        if depth > self.max_depth:
            raise PipelineError('role_depth', 'Role delegation depth limit reached')
        with self.lock:
            if key in self.active:
                raise PipelineError('session_busy', 'This role session already has a worker')
            if not self.capacity.acquire(blocking=False):
                raise PipelineError('role_capacity', 'Role worker queue is full')
            # The reservation is an execution before pool.submit returns.
            # Observers must see a waitable owner throughout that interval,
            # including when the worker already started in another thread.
            completion = Future()
            completion.set_running_or_notify_cancel()
            self.active[key] = completion
        def run():
            token = _DEPTH.set(depth)
            try:
                return function()
            finally:
                _DEPTH.reset(token)
        try:
            context = copy_context()
            future = self.pool.submit(context.run, run)
        except BaseException as exc:
            with self.lock:
                self.active.pop(key, None)
            self.capacity.release()
            completion.set_exception(exc)
            raise
        def finished(actual):
            error = actual.exception() if not actual.cancelled() else None
            with self.lock:
                self.active.pop(key, None)
            self.capacity.release()
            if actual.cancelled():
                completion.set_exception(CancelledError())
            elif error is not None:
                completion.set_exception(error)
            else:
                completion.set_result(actual.result())
        future.add_done_callback(finished)
        return completion

    def shutdown(self):
        self.pool.shutdown(wait=True, cancel_futures=False)


DEFAULT_EXECUTOR = RoleExecutor()
