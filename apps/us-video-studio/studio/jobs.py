"""Durable conversation executions in the existing run event journal.

Browser connections only observe jobs. Restart never repeats a model request.
One writer lease and one active job cover this data root's shared cost ledger.
"""
from __future__ import annotations

import hashlib
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from .agents.client import JobCancelled, ModelError
from .agents.codex_client import CodexUnavailable
from .connections import ConnectionUnavailable
from .models import BudgetExceeded, PipelineError, STAGES
from .store import Store

TERMINAL = frozenset({'succeeded', 'failed', 'blocked', 'cancelled', 'interrupted'})


class WriterLease:
    def __init__(self, root: Path):
        self.root = root
        self.handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.close()

    def acquire(self):
        if self.handle is not None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        handle = open(self.root / '.studio-writer.lock', 'a+b')
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise PipelineError('writer_busy', '这个数据目录已有制片室写入进程；请连接原服务。') from exc
        self.handle = handle

    def close(self):
        if self.handle is None:
            return
        handle, self.handle = self.handle, None
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class JobManager:
    def __init__(self, store: Store, pipeline, ledger, prepare: Callable):
        self.store, self.pipeline, self.ledger, self.prepare = store, pipeline, ledger, prepare
        self.lease = WriterLease(store.root)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.active: tuple[str, str] | None = None
        self.cancelled = threading.Event()
        self.initialized = False
        self.closing = False

    def initialize(self):
        with self.lock:
            if self.closing:
                raise PipelineError('closing', '此服务正在停止，请连接当前制片室服务。')
            if self.initialized:
                return
            self.lease.acquire()
            try:
                for run in self.store.list_runs():
                    for job in self.list(run.run_id):
                        if job['status'] not in TERMINAL:
                            self._event(run.run_id, job['job_id'], 'job_status', status='interrupted',
                                        reason='服务曾中断；未自动重发。先检查调用台账，再发送新的继续指令。')
                self.initialized = True
            except Exception:
                self.lease.close()
                raise

    def list(self, run_id):
        jobs = {}
        with self.store.run_lock(run_id):
            rows = self.store.read_events(run_id)
        for row in rows:
            if row.get('kind') == 'job_created':
                jobs[row['job_id']] = {'job_id': row['job_id'], 'status': 'queued'}
            elif row.get('kind') == 'job_status' and row.get('job_id') in jobs:
                jobs[row['job_id']]['status'] = row['status']
        return list(jobs.values())

    def get(self, run_id, job_id, after_seq=0):
        with self.store.run_lock(run_id):
            rows = [e for e in self.store.read_events(run_id) if e.get('job_id') == job_id]
        created = next((r for r in rows if r['kind'] == 'job_created'), None)
        if created is None:
            raise PipelineError('job_missing', '没有找到这次执行。')
        out = {k: v for k, v in created.items() if k not in ('kind', 'seq')}
        out.update(run_id=run_id, status='queued', events=[], cursor=created['seq'], reason='')
        for row in rows:
            out['cursor'] = row['seq']
            if row['kind'] == 'job_status':
                out['status'], out['reason'] = row['status'], row.get('reason', '')
            if row['kind'] == 'job_progress' and row['seq'] > after_seq:
                out['events'].append({**row['event'], 'seq': row['seq'], 'at': row['at']})
        out['unresolved_calls'] = [
            {'run_id': r.run_id, 'call_id': r.call_id, 'role': r.role,
             'call_status': r.call_status, 'usage_status': r.usage_status}
            for r in self.ledger.entries() if r.call_status == 'pending' or r.usage_status != 'known'
        ]
        out['can_continue'] = out['status'] in TERMINAL and not out['unresolved_calls']
        with self.store.run_lock(run_id):
            out['open_attempts'] = [
                {'stage': stage, 'attempt': state.open_attempt.key}
                for stage in STAGES
                if (state := self.pipeline.stage_state(run_id, stage)).open_attempt is not None
            ]
        out['can_continue'] = out['can_continue'] and not out['open_attempts']
        if out['status'] == 'interrupted':
            out['recovery'] = ('不会自动重发旧请求。未知调用先按提供方证据核账；'
                               '在恢复面板关闭遗留阶段尝试，再由你明确发起下一轮。')
        return out

    def latest(self, run_id, after_seq=0):
        jobs = self.list(run_id)
        return self.get(run_id, jobs[-1]['job_id'], after_seq) if jobs else None

    def _event(self, run_id, job_id, kind, **fields):
        with self.store.run_lock(run_id):
            row = self.store.append_event(run_id, kind, job_id=job_id, **fields)
        with self.changed:
            self.changed.notify_all()
        return row

    def submit(self, run_id, text, request_id, *, kind='conversation', payload=None, prepare=None):
        with self.lock:
            self.initialize()
            if self.closing:
                raise PipelineError('closing', '服务正在停止，请稍后重新连接。')
            for row in self.store.read_events(run_id):
                if row.get('kind') == 'job_created' and row.get('request_id') == request_id:
                    if (row['text'] != text or row.get('job_kind', 'conversation') != kind
                            or row.get('job_payload') != payload):
                        raise PipelineError('request_conflict', '相同 request_id 对应另一条指令，请使用新的请求标识。')
                    return self.get(run_id, row['job_id'])
            if self.active is not None:
                raise PipelineError('job_busy', f'制片室正在运行 {self.active[0]} 的任务；本次未排队、未调用模型。')
            runner, config = (prepare or self.prepare)(run_id)
            job_id = str(uuid.uuid4())
            with self.store.run_lock(run_id):
                artifacts = self.store.list_artifacts(run_id)
                refs = {'chat_entries': len(self.store.read_chat(run_id)),
                        'event_seq': len(self.store.read_events(run_id)),
                        'run_brief_sha256': hashlib.sha256(self.store.read_run(run_id).brief.encode('utf-8')).hexdigest(),
                        'artifacts': [{'stage': a.stage, 'version': a.version, 'sha256': a.sha256} for a in artifacts]}
                self._event(run_id, job_id, 'job_created', request_id=request_id, text=text,
                            model_config=config, context_refs=refs, job_kind=kind, job_payload=payload)
            self.active = (run_id, job_id)
            self.cancelled = threading.Event()
            self._event(run_id, job_id, 'job_status', status='running')
            threading.Thread(target=self._work, args=(run_id, job_id, text, runner), daemon=True).start()
            return self.get(run_id, job_id)

    def check_cancel(self):
        if self.cancelled.is_set():
            raise JobCancelled('用户已请求停止；未启动下一次模型调用或工具。')

    def _work(self, run_id, job_id, text, runner):
        def emit(event):
            self._event(run_id, job_id, 'job_progress', event=event)
        status, reason = 'succeeded', ''
        try:
            result = runner(text, emit, self.check_cancel)
            self.check_cancel()
            if result.stopped_because != 'done':
                status, reason = 'blocked', f'本轮结束于 {result.stopped_because}；已保存进度，请查看结果后发送新的指令。'
            emit({'type': 'done', 'text': result.text, 'rounds': result.rounds,
                  'stopped_because': result.stopped_because, 'tools': result.used_tools})
        except JobCancelled as exc:
            status, reason = 'cancelled', str(exc)
            emit({'type': 'cancelled', 'message': reason})
        except BudgetExceeded as exc:
            status, reason = 'blocked', str(exc)
            emit({'type': 'blocked', 'message': reason})
        except (CodexUnavailable, ConnectionUnavailable) as exc:
            status, reason = 'blocked', f'连接准备未通过：{exc}。修正对应连接后再发送新的指令。'
            emit({'type': 'blocked', 'message': reason})
        except Exception as exc:
            detail = str(exc) if isinstance(exc, (ModelError, PipelineError)) else '执行失败；调用状态已保留，请检查连接和台账。'
            status, reason = 'failed', f'{type(exc).__name__}: {detail}'
            emit({'type': 'error', 'message': reason})
        finally:
            with self.lock:
                self._event(run_id, job_id, 'job_status', status=status, reason=reason)
                self.active = None
                if self.closing:
                    self.lease.close()

    def cancel(self, run_id, job_id):
        with self.lock:
            current = self.get(run_id, job_id)
            if current['status'] in TERMINAL:
                return current
            if self.active != (run_id, job_id):
                raise PipelineError('job_not_active', '任务不在此进程运行；请重新加载中断记录。')
            self.cancelled.set()
            if current['status'] != 'cancelling':
                self._event(run_id, job_id, 'job_status', status='cancelling',
                            reason=('停止本地制作等待；提供方可能仍在生成并计费。请从制作记录查询原任务并核实用量。'
                                    if current.get('job_kind') in {'production_generation', 'production_refresh'} else
                                    '等待当前调用结算后停止，不强杀在途请求；未知用量保持待核实。'))
            return self.get(run_id, job_id)

    def busy(self, run_id):
        with self.lock:
            return self.active is not None and self.active[0] == run_id

    def stream(self, run_id, job_id):
        cursor = 0
        while True:
            job = self.get(run_id, job_id, cursor)
            for event in job['events']:
                yield event
            cursor = job['cursor']
            if job['status'] in TERMINAL:
                return
            with self.changed:
                self.changed.wait(timeout=0.2)

    def close(self):
        with self.lock:
            self.closing = True
            self.cancelled.set()
            if self.active is None:
                self.lease.close()
