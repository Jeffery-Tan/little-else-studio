"""Durable conversation executions in the existing run event journal.

Browser connections only observe jobs. Restart never repeats a model request.
One writer lease and one active job cover this data root's shared cost ledger.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable

from .agents.client import JobCancelled, ModelError, NonRetryableStageError
from .agents.codex_client import CodexUnavailable
from .connections import ConnectionUnavailable
from .models import BudgetExceeded, PipelineError, STAGES
from .steer_intent import classify, continuation_allowed, needs_stop
from .store import Store
from .agent_runtime import AgentRuntime, bind_task, reset_task
from .tools.registry import tool_names_for

TERMINAL = frozenset({'succeeded', 'failed', 'blocked', 'cancelled', 'interrupted'})


def _pending_steer_work(corrections):
    # A stop revokes all still-pending changes for this job, including changes
    # received before it. Filtering each message independently revives old work.
    corrections = [row for row in corrections if not row.get('target_session_id')]
    if any(row.get('steer_class') == 'stop' for row in corrections):
        return []
    return [row for row in corrections if continuation_allowed(row)]


def _continuation_text(original, corrections, job_kind='conversation'):
    def correction_line(i, row):
        target = row.get('feedback_target')
        location = ('\n   用户选定的准确对象：' + json.dumps(target, ensure_ascii=False, sort_keys=True) +
                    '。先核对这个版本仍存在；若已变化，说明冲突并请用户重新选，不能猜测对应新版。') if target else ''
        kind = row.get('steer_class')
        label = {'status': '（执行中提问，只回答，不改动在途工作）',
                 'stop': '（执行中要求停止）'}.get(kind, '')
        return f'{i}. {row["text"]}{label}{location}'
    additions = '\n'.join(correction_line(i, row) for i, row in enumerate(corrections, 1))
    if job_kind != 'conversation':
        return ('用户在上一项任务执行中插入了新要求。上一项任务是「' + original + '」，'
                '可能只完成了一部分，也可能留下需要核实的媒体请求。\n'
                '补充要求（按收到顺序，以后者为准）：\n' +
                additions +
                '\n先读取当前项目、准确产物和未完成调用，再由主制片判断哪些内容需要立即修正、'
                '哪些适合下一步处理。不要继承上一项任务的图审或费用授权，不自动重复媒体提交。')
    return ('调整上一条正在执行的任务。原要求：\n' + original +
            '\n执行中收到的补充要求（按顺序，以后者为准）：\n' +
            additions +
            '\n先核对已保存成果和当前权限，再按最新要求继续。'
            '注意：用户在执行中说的话可能是询问、讨论或修改要求，先按原话判断是哪一种；'
            '只有确实改变要求的才改动已保存的成果，纯询问只回答事实。')


class WriterLease:
    def __init__(self, root: Path, *, name: str = '.studio-writer.lock'):
        self.root = root
        self.name = name
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
        handle = open(self.root / self.name, 'a+b')
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
    """One manager per data root; ``self.lock`` guards its bookkeeping.

    **Lock order — always ``jobs.lock`` first, then ``store.run_lock``.** The
    worker's finishing path holds ``jobs.lock`` and then appends its status event
    through ``_event``, which takes the run lock; ``get`` does the same in that
    order. Any caller that already holds a run lock must therefore *not* ask this
    manager anything — take the value before acquiring the run lock instead.
    On 2026-09-30 a single ``jobs.busy()`` call from inside the timeline's run
    lock deadlocked the two (ABBA) and froze every endpoint that reads a run: the
    worker waited for the run lock while the reader waited for ``jobs.lock``.
    """

    def __init__(self, store: Store, pipeline, ledger, prepare: Callable):
        self.store, self.pipeline, self.ledger, self.prepare = store, pipeline, ledger, prepare
        self.lease = WriterLease(store.root)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.active: tuple[str, str] | None = None
        self.cancelled = threading.Event()
        self.initialized = False
        self.closing = False
        self.before_submit: Callable[[], None] | None = None
        self.prepare_input: Callable | None = None
        self.prepare_followup: Callable | None = None
        self._closing_drain_started = False

    def _role_futures(self, run_id=None, parent_job_id=None):
        from .tools.dispatch import in_flight_role_futures
        return in_flight_role_futures(self.store, run_id=run_id,
                                     parent_job_id=parent_job_id)

    def _settle_roles(self, run_id=None, parent_job_id=None, *, stop=False):
        """Wait for real workers, never holding jobs.lock or a run lock.

        A session's terminal receipt can precede its thread's final callback or
        model lease release. The bounded executor is the execution authority.
        Parent cancellation must not interrupt this settlement wait.
        """
        if stop:
            from .agent_sessions import AgentSessions
            runs = [run_id] if run_id else [run.run_id for run in self.store.list_runs()]
            for identity in runs:
                sessions = AgentSessions(self.store, identity)
                for session in sessions.sessions():
                    owned = parent_job_id is None or session.get('parent_job_id') == parent_job_id
                    if owned and session['status'] in {'queued', 'running'}:
                        sessions.cancel(session['session_id'], reason='Owning execution is stopping')
        while futures := self._role_futures(run_id, parent_job_id):
            for future in futures:
                try:
                    future.result(timeout=.1)
                except FutureTimeout:
                    pass
                except Exception:
                    # Failure is already durable in the role journal. It is
                    # still a settled worker, not grounds to replay its call.
                    pass

    def _start_close_drain_locked(self):
        if self._closing_drain_started:
            return
        self._closing_drain_started = True
        def drain():
            self._settle_roles(stop=True)
            with self.lock:
                if self.active is None:
                    self.lease.close()
        threading.Thread(target=drain, name='studio-closing-roles', daemon=True).start()

    def initialize(self):
        with self.lock:
            if self.closing:
                raise PipelineError('closing', '此服务正在停止，请连接当前制片室服务。')
            if self.initialized:
                return
            self.lease.acquire()
            try:
                for run in self.store.list_runs():
                    AgentRuntime(self.store, run.run_id).interrupt_open_tasks()
                    from .agent_sessions import AgentSessions
                    AgentSessions(self.store, run.run_id).interrupt_open_sessions()
                    all_rows = self.store.read_events(run.run_id)
                    for job in self.list(run.run_id):
                        created = next(row for row in all_rows
                                       if row['kind'] == 'job_created' and row['job_id'] == job['job_id'])
                        job_payload = created.get('job_payload') or {}
                        if job['status'] not in TERMINAL:
                            self._event(run.run_id, job['job_id'], 'job_status', status='interrupted',
                                        reason='服务曾中断；未自动重发。先检查调用台账，再发送新的继续指令。')
                        rows = [r for r in all_rows if r.get('job_id') == job['job_id']]
                        steers = [r for r in rows if r['kind'] == 'job_steer_received']
                        answered_ids = {r.get('request_id') for r in rows
                                        if r['kind'] == 'job_steer_answered' and r.get('text')}
                        settled = any(r['kind'] in {'job_steer_delivery_failed', 'job_steer_discarded', 'job_steer_blocked',
                                                    'job_steer_unanswered'} for r in rows) or (
                            bool(steers) and all(r.get('request_id') in answered_ids for r in steers))
                        has_successor = any(r.get('kind') == 'job_created'
                                            and r.get('parent_job_id') == job['job_id'] for r in all_rows)
                        if not has_successor:
                            discarded = any(r['kind'] == 'job_steer_discarded' for r in rows)
                            # A restart can occur after saving the stop but before
                            # saving its discard receipt. Never offer that old work.
                            settled = self._settle_stopped_steer(
                                run.run_id, job['job_id'], steers, discarded) or settled
                        if steers and not settled and not has_successor:
                            if _pending_steer_work(steers):
                                self._event(run.run_id, job['job_id'], 'job_steer_blocked',
                                            reason='服务中断，调整已保存但未自动接续；检查进度后请明确接续。')
                            else:
                                # A question or a stop is not a correction waiting
                                # to be resumed. Asking the user to click "continue"
                                # for it would start work they did not ask for.
                                self._event(run.run_id, job['job_id'], 'job_steer_unanswered',
                                            reason='服务中断前未保存该问题的回答；原任务未重发，请重新查询状态。')
                        if (job_payload.get('pending_user_inputs') and not has_successor
                                and not any(r['kind'] in {'job_steer_discarded', 'job_steer_blocked'} for r in rows)):
                            self._event(run.run_id, job['job_id'], 'job_steer_blocked',
                                        reason='服务中断，已收到的消息尚未处理；请核对状态后明确接续。')
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
        # A terminal old job and its queued successor must be observed as one
        # transition; otherwise a polling browser can stop watching between them.
        with self.lock:
            return self._get_locked(run_id, job_id, after_seq)

    def _get_locked(self, run_id, job_id, after_seq=0):
        with self.store.run_lock(run_id):
            all_rows = self.store.read_events(run_id)
            rows = [e for e in all_rows if e.get('job_id') == job_id]
        created = next((r for r in rows if r['kind'] == 'job_created'), None)
        if created is None:
            raise PipelineError('job_missing', '没有找到这次执行。')
        out = {k: v for k, v in created.items() if k not in ('kind', 'seq')}
        out.update(run_id=run_id, status='queued', events=[], cursor=created['seq'], reason='',
                   deliverable=None, revision_result=None, goal_revision=0,
                   steer_status=None, steer_class=None, successor_job_id=None)
        from .agent_sessions import AgentSessions
        out['role_sessions'] = AgentSessions(self.store, run_id).sessions()
        corrections = []
        answered = set()
        for row in rows:
            out['cursor'] = row['seq']
            if row['kind'] == 'job_status':
                out['status'], out['reason'] = row['status'], row.get('reason', '')
            if row['kind'] == 'job_deliverable':
                out['deliverable'] = row['deliverable']
            if row['kind'] == 'job_progress':
                event = row['event']
                if event.get('type') == 'writing_revision_result' and isinstance(event.get('result'), dict):
                    out['revision_result'] = event['result']
                if row['seq'] > after_seq:
                    out['events'].append({**event, 'seq': row['seq'], 'at': row['at']})
            if row['kind'] == 'job_steer_received':
                corrections.append(row)
                out['goal_revision'] = row['goal_revision']
                out['steer_status'] = 'received'
                out['steer_class'] = row.get('steer_class') or 'work'
            if row['kind'] == 'job_steer_delivered':
                out['steer_status'] = 'delivered'
                out['steer_reason'] = row['text']
                out['steer_target_session_id'] = row['session_id']
            if row['kind'] == 'job_steer_delivery_failed':
                out['steer_status'] = 'rejected'
                out['steer_reason'] = row['reason']
                out['steer_target_session_id'] = row['session_id']
            if row['kind'] == 'job_steer_blocked':
                out['steer_status'] = 'blocked'
                out['steer_reason'] = row['reason']
            if row['kind'] == 'job_steer_discarded':
                out['steer_status'] = 'discarded'
            if row['kind'] == 'job_steer_answered':
                out['steer_status'] = 'answered'
                out['steer_answered_classes'] = row.get('classes')
                answered.add(row.get('request_id'))
            if row['kind'] == 'job_steer_unanswered':
                out['steer_status'] = 'unanswered'
                out['steer_reason'] = row['reason']
        if out['steer_status'] == 'answered' and any(
                row.get('request_id') not in answered and continuation_allowed(row) for row in corrections):
            out['steer_status'] = 'received'
        successor = next((r for r in all_rows if r.get('kind') == 'job_created'
                          and r.get('parent_job_id') == job_id), None)
        if successor is not None:
            out['successor_job_id'] = successor['job_id']
            out['steer_status'] = 'continued'
        out['steer_count'] = len(corrections)
        if corrections:
            out['steer_latest_text'] = corrections[-1]['text']
        out['unresolved_calls'] = [
            {'run_id': r.run_id, 'call_id': r.call_id, 'role': r.role,
             'call_status': r.call_status, 'usage_status': r.usage_status}
            for r in self.ledger.entries(run_id) if r.call_status == 'pending' or r.usage_status != 'known'
        ]
        out['can_continue'] = out['status'] in TERMINAL
        with self.store.run_lock(run_id):
            out['open_attempts'] = [
                {'stage': stage, 'attempt': state.open_attempt.key}
                for stage in STAGES
                if (state := self.pipeline.stage_state(run_id, stage)).open_attempt is not None
            ]
        out['can_continue'] = (out['can_continue'] and not out['open_attempts']
                               and not self._role_futures(run_id))
        if out['status'] == 'interrupted':
            out['recovery'] = ('不会自动重发旧请求。用量缺失仅保留记录；'
                               '在恢复面板关闭遗留阶段尝试，再由你明确发起下一轮。')
        return out

    def latest(self, run_id, after_seq=0):
        jobs = self.list(run_id)
        return self.get(run_id, jobs[-1]['job_id'], after_seq) if jobs else None

    def request_receipt(self, run_id, request_id, *, kind='job', job_id=None):
        """Read an exact durable acceptance record without retrying the request.

        Absence is not proof of rejection: an in-flight POST may still arrive.
        The caller must keep its original identity and payload until resolved.
        """
        if kind not in {'job', 'steer'} or (kind == 'steer' and not job_id):
            raise ValueError('request receipt needs a kind and a target job for steer')
        event_kind = 'job_created' if kind == 'job' else 'job_steer_received'
        with self.store.run_lock(run_id):
            rows = self.store.read_events(run_id)
        rejection = next((row for row in rows
                          if kind == 'steer' and row.get('kind') == 'job_steer_delivery_failed'
                          and row.get('request_id') == request_id
                          and row.get('job_id') == job_id), None)
        if rejection is not None:
            return {'received': False, 'rejected': True, 'kind': kind,
                    'request_id': request_id, 'job_id': job_id,
                    'code': rejection['code'], 'reason': rejection['reason']}
        record = next((row for row in rows
                       if row.get('kind') == event_kind
                       and row.get('request_id') == request_id
                       and (kind == 'job' or row.get('job_id') == job_id)), None)
        return {'received': record is not None, 'kind': kind,
                'request_id': request_id,
                'job_id': record['job_id'] if record else job_id if kind == 'steer' else None}

    def _event(self, run_id, job_id, kind, **fields):
        with self.store.run_lock(run_id):
            row = self.store.append_event(run_id, kind, job_id=job_id, **fields)
        with self.changed:
            self.changed.notify_all()
        return row

    def _settle_stopped_steer(self, run_id, job_id, corrections, discarded):
        if discarded:
            return True
        stop = next((row for row in reversed(corrections)
                     if row.get('steer_class') == 'stop'), None)
        if stop is None:
            return False
        self._event(run_id, job_id, 'job_steer_discarded', request_id=stop['request_id'],
                    reason='用户已要求停止；本轮尚未执行的调整已撤销，不自动接续。')
        return True

    def submit(self, run_id, text, request_id, *, kind='conversation', payload=None,
               prepare=None, parent_job_id=None, user_input_id=None):
        with self.lock:
            self.initialize()
            if self.closing:
                raise PipelineError('closing', '服务正在停止，请稍后重新连接。')
            if self.store.deletion_state(run_id):
                raise PipelineError('project_deleted', '项目已移入回收站，请先恢复后再继续。')
            for row in self.store.read_events(run_id):
                if row.get('kind') == 'job_created' and row.get('request_id') == request_id:
                    if (row['text'] != text or row.get('job_kind', 'conversation') != kind
                            or row.get('job_payload') != payload):
                        raise PipelineError('request_conflict', '相同 request_id 对应另一条指令，请使用新的请求标识。')
                    return self.get(run_id, row['job_id'])
            if self.active is not None or self._role_futures():
                active_run = self.active[0] if self.active else '已有项目'
                raise PipelineError('job_busy', f'制片室正在运行 {active_run} 的任务或等待角色收尾；本次未排队、未调用模型。')
            if self.before_submit is not None:
                self.before_submit()
            runner, config = (prepare or self.prepare)(run_id)
            job_id = str(uuid.uuid4())
            with self.store.run_lock(run_id):
                artifacts = self.store.list_artifacts(run_id)
                refs = {'chat_entries': len(self.store.read_chat(run_id)),
                        'event_seq': len(self.store.read_events(run_id)),
                        'run_brief_sha256': hashlib.sha256(self.store.read_run(run_id).brief.encode('utf-8')).hexdigest(),
                        'artifacts': [{'stage': a.stage, 'version': a.version, 'sha256': a.sha256} for a in artifacts]}
                self._event(run_id, job_id, 'job_created', request_id=request_id, text=text,
                            model_config=config, context_refs=refs, job_kind=kind,
                            job_payload=payload, parent_job_id=parent_job_id, user_input_id=user_input_id)
                if user_input_id:
                    from .conversation_input import message, accept
                    accept(self.store, run_id, message(self.store, run_id, user_input_id))
                runtime = AgentRuntime(self.store, run_id)
                runtime.create_task(job_id, role='orchestrator', parent_job_id=job_id,
                    source_message_id=user_input_id or (payload or {}).get('source_user_input_id'),
                    context_refs=refs, allowed_tools=tool_names_for('orchestrator'))
                if user_input_id:
                    receipt = message(self.store, run_id, user_input_id)
                    runtime.post_message(job_id, user_input_id, receipt['text'],
                                         source_message_id=user_input_id)
            self.active = (run_id, job_id)
            self.cancelled = threading.Event()
            self._event(run_id, job_id, 'job_status', status='running')
            threading.Thread(target=self._work, args=(run_id, job_id, text, runner), daemon=True).start()
            return self.get(run_id, job_id)

    def submit_decision_followup(self, run_id, text, request_id, *, parent_job_id=None):
        """Both UI and conversation consume the same fixed identity and receipt."""
        with self.lock:
            prior = next((e for e in self.store.read_events(run_id)
                          if e['kind'] == 'job_created' and e.get('request_id') == request_id), None)
            if prior:
                if prior['text'] != text:
                    raise PipelineError('request_conflict', '这个决定的接续内容已经改变。')
                job = self.get(run_id, prior['job_id'])
            else:
                intent = next((e for e in self.store.read_events(run_id)
                               if e['kind'] == 'decision_followup_requested'
                               and e.get('request_id') == request_id), {})
                source_id = intent.get('source_user_input_id')
                job = self.submit(run_id, text, request_id, parent_job_id=parent_job_id,
                    payload={'source_user_input_id': source_id} if source_id else None,
                    prepare=(lambda rid: self.prepare_followup(rid, source_id))
                        if source_id and self.prepare_followup else None)
            if not any(e['kind'] == 'decision_followup_consumed' and e.get('request_id') == request_id
                       for e in self.store.read_events(run_id)):
                self._event(run_id, job['job_id'], 'decision_followup_consumed',
                            request_id=request_id, parent_job_id=parent_job_id)
            return job

    def steer(self, run_id, job_id, text, request_id, expected_goal_revision,
              feedback_target=None, validate_target=None, *, display_context_id=None,
              question_id=None, target_session_id=None):
        """Durably accept an in-flight message, then decide if work must stop.

        The message is persisted first, always. What happens to the running turn
        depends on what the message is: a question about progress must not kill
        the generation it is asking about, and an explicit stop must not be
        recycled into an automatic successor. Only work that genuinely changes
        the request interrupts the turn. See ``studio/steer_intent.py``.

        Retried requests return their original receipt even after that worker
        exits.
        """
        text = text.strip()
        if not text:
            raise PipelineError('steer_empty', '请填写调整内容。')
        with self.lock:
            self.initialize()
            current = self.get(run_id, job_id)
            job_kind = current.get('job_kind', 'conversation')
            classification = classify(text)
            rows = [r for r in self.store.read_events(run_id)
                    if r.get('job_id') == job_id and r.get('kind') == 'job_steer_received']
            prior = next((r for r in rows if r['request_id'] == request_id), None)
            if prior is not None:
                if (prior['text'] != text or prior['expected_goal_revision'] != expected_goal_revision
                        or prior.get('feedback_target') != feedback_target
                        or prior.get('display_context_id') != display_context_id
                        or prior.get('question_id') != question_id
                        or prior.get('target_session_id') != target_session_id):
                    raise PipelineError('request_conflict', '相同 request_id 对应另一条调整指令。')
                failed = next((row for row in self.store.read_events(run_id)
                               if row.get('kind') == 'job_steer_delivery_failed'
                               and row.get('job_id') == job_id and row.get('request_id') == request_id), None)
                if failed is not None:
                    raise PipelineError(failed['code'], failed['reason'])
                return current
            if self.active != (run_id, job_id) or current['status'] not in {'running', 'cancelling'}:
                raise PipelineError('job_not_active', '原任务已经结束，请刷新后发送新的指令。')
            if current['status'] == 'cancelling' and current['steer_status'] != 'received':
                raise PipelineError('steer_scope', '当前任务已经要求停止；请等待结束后发送新指令。')
            if current['steer_status'] == 'discarded':
                raise PipelineError('steer_scope', '当前任务已要求停止，请等待结束后重新发送。')
            if expected_goal_revision != current['goal_revision']:
                raise PipelineError('goal_conflict', '任务目标已更新，请刷新后按当前目标调整。')
            if target_session_id:
                from .agent_sessions import AgentSessions
                sessions = AgentSessions(self.store, run_id)
                session = next((row for row in sessions.sessions()
                                if row['session_id'] == target_session_id), None)
                if (not session or session.get('parent_job_id') != job_id
                        or session.get('status') not in {'queued', 'running'}):
                    raise PipelineError('session_scope', '这项角色任务已经结束或不属于本轮，请刷新后明确恢复。')
                if feedback_target or question_id:
                    raise PipelineError('session_scope', '定向角色补充不能同时绑定另一个作品或待决问题。')
            if len(rows) >= 8 or sum(len(r['text']) for r in rows) + len(text) > 12000:
                raise PipelineError('steer_limit', '这轮补充内容已经较长，请等当前任务结束后发起新对话。')
            if feedback_target and validate_target:
                validate_target()
            if feedback_target and question_id:
                raise PipelineError('steer_scope', '作品反馈不能同时回答待决问题。')
            if question_id:
                from .interactions import check_answer
                check_answer(self.store, run_id, question_id)
            from .conversation_input import receive, accept
            receipt = receive(self.store, run_id, text, request_id, display_context_id,
                              feedback_target=feedback_target, question_id=question_id)
            targeted = target_session_id and classification['class'] != 'status'
            if targeted:
                classification = {'class': 'role_stop' if classification['class'] == 'stop' else 'role_message',
                                  'reason': 'User selected an exact active role session'}
            acknowledged = {
                'status': '已收到你的询问，正在读取当前状态；原任务继续执行。',
                'stop': '已收到停止要求，正在安全收尾；提供方可能仍在生成并计费，可从制作记录查询原任务。',
                'discussion': '已收到讨论要求；原任务继续执行，结束后单独回答，不自动改稿。',
            }.get(classification['class'],
                  '已收到调整，等待当前调用结束后按新要求继续。')
            if targeted:
                acknowledged = ('已要求停止这项角色任务；其他任务继续，已保存内容保留。'
                                if classification['class'] == 'role_stop' else
                                '已收到给这项角色任务的补充，将在下一步按原话处理；其他任务继续。')
            with self.store.run_lock(run_id):
                received = self._event(run_id, job_id, 'job_steer_received', text=text, request_id=request_id,
                                       expected_goal_revision=expected_goal_revision,
                                       goal_revision=expected_goal_revision + 1,
                                       feedback_target=feedback_target,
                                       user_input_id=receipt['message_id'],
                                       display_context_id=display_context_id, question_id=question_id,
                                       target_session_id=target_session_id,
                                       steer_class=classification['class'],
                                       steer_reason=classification['reason'])
                accept(self.store, run_id, receipt)
                if targeted:
                    try:
                        # Receipt creation can interleave with role completion.
                        # Check again inside the same run lock as inbox delivery.
                        selected = sessions.snapshot(target_session_id)
                        if (selected.get('parent_job_id') != job_id
                                or selected['status'] not in {'queued', 'running'}
                                or selected['stop_requested']):
                            raise PipelineError('session_closed',
                                '这项角色任务已经结束，补充未投递。原话已保存；请刷新后重新选择要处理的对象。')
                        if classification['class'] == 'role_stop':
                            from .conversation_input import start_execution
                            start_execution(self.store, run_id, receipt)
                            sessions.cancel(target_session_id, reason=text)
                        else:
                            sessions.post_message(target_session_id, receipt['message_id'], text,
                                                  source_message_id=receipt['message_id'])
                    except PipelineError as exc:
                        self._event(run_id, job_id, 'job_steer_delivery_failed', request_id=request_id,
                                    user_input_id=receipt['message_id'], session_id=target_session_id,
                                    code=exc.rule,
                                    reason=str(exc).removeprefix(f'[{exc.rule}] '))
                        raise
                    self._event(run_id, job_id, 'job_steer_delivered', request_id=request_id,
                                user_input_id=receipt['message_id'], session_id=target_session_id,
                                text=acknowledged)
                if classification['class'] in {'status', 'stop'}:
                    from .conversation_input import start_execution
                    start_execution(self.store, run_id, receipt)
                self._settle_stopped_steer(run_id, job_id, [received], False)
                self._event(run_id, job_id, 'job_progress', event={
                    'type': 'agent_control', 'message': acknowledged})
                if needs_stop(classification):
                    self.cancelled.set()
                    AgentRuntime(self.store, run_id).request_stop(job_id, reason=acknowledged)
            if classification['class'] == 'status':
                self._answer_status(run_id, job_id, received)
            if needs_stop(classification) and (current['status'] != 'cancelling'
                                               or classification['class'] == 'stop'):
                self._event(run_id, job_id, 'job_status', status='cancelling',
                            reason=('已收到停止要求，等待当前调用安全结束；不自动接续已保存的调整。'
                                    if classification['class'] == 'stop' else
                                    '已记录中途意见，等待当前调用结束；随后主制片按当前真实进展处理。'
                                    if job_kind != 'conversation' else
                                    '正在停止旧任务，随后按调整后的目标继续。'))
            return self.get(run_id, job_id)

    def _answer_status(self, run_id, job_id, received):
        """A read-only reply is saved before claiming this question was answered."""
        from .production import agent_production_state
        from .vocabulary import stage_label
        job = self.get(run_id, job_id)
        with self.store.run_lock(run_id):
            production = agent_production_state(self.store, self.pipeline, run_id)
            current = [f'{stage_label(stage)}第 {state.latest.version} 版'
                       for stage in STAGES
                       if (state := self.pipeline.stage_state(run_id, stage)).latest and not state.stale]
            lines = [('当前任务仍在执行。' if job['status'] == 'running' else
                      '当前任务正在安全停止，尚未结算完成。')]
            lines.append('已保存：' + '、'.join(current) + '。' if current else '本轮尚无有效稿件保存。')
            snapshot = {'job_status': job['status'], 'saved': current, 'coverage': {}}
            for kind, label in (('image', '图片'), ('video', '视频')):
                coverage = production['shots'][kind]
                if not coverage['complete_list']:
                    continue
                shots = coverage['shots']
                completed = sum(shot['stage'] == 'completed' for shot in shots)
                failed = [shot['ordinal'] for shot in shots if shot['stage'] == 'failed']
                unknown = [shot['ordinal'] for shot in shots if shot['stage'] == 'unverified']
                lines.append(f'{label}结果已完成 {completed}/{len(shots)} 镜。')
                if failed:
                    lines.append('失败镜头：' + '、'.join(map(str, failed)) + '。')
                if unknown:
                    lines.append('待核实镜头：' + '、'.join(map(str, unknown)) + '；只查询原任务。')
                snapshot['coverage'][kind] = {'completed': completed, 'total': len(shots),
                                              'failed': failed, 'unknown': unknown}
            reply = '\n'.join(lines)
            self.store.append_chat(run_id, 'assistant', reply)
            self._event(run_id, job_id, 'job_steer_answered',
                        request_id=received['request_id'], user_input_id=received['user_input_id'],
                        classes=['status'], text=reply, snapshot=snapshot)
            self._event(run_id, job_id, 'job_progress', event={
                'type': 'agent_control', 'message': reply})

    def _pending_inputs(self, run_id, current, corrections):
        """Pending inbox lives in the same job journal, with one real input per turn."""
        from .conversation_input import receive
        pending = list((current.get('job_payload') or {}).get('pending_user_inputs') or [])
        for row in _pending_steer_work(corrections):
            message_id = row.get('user_input_id')
            if not message_id:  # Legacy journal entry; no invented display authority.
                message_id = receive(self.store, run_id, row['text'], row['request_id'],
                                     feedback_target=row.get('feedback_target'))['message_id']
            pending.append(message_id)
        return list(dict.fromkeys(pending))

    def _submit_pending_inputs(self, run_id, current, pending):
        from .conversation_input import message
        receipt = message(self.store, run_id, pending[0])
        previous = current.get('job_payload') or {}
        original = previous.get('steer_original', current['text'])
        original_kind = previous.get('steer_job_kind', current.get('job_kind', 'conversation'))
        row = {'text': receipt['text'], 'feedback_target': receipt.get('feedback_target')}
        payload = {'pending_user_inputs': pending[1:], 'steer_original': original,
                   'steer_job_kind': original_kind}
        return self.submit(run_id, _continuation_text(original, [row], original_kind),
                           f'steer:{current["job_id"]}', parent_job_id=current['job_id'],
                           payload=payload, user_input_id=receipt['message_id'],
                           prepare=(lambda rid: self.prepare_input(rid, receipt))
                               if self.prepare_input else None)

    def check_cancel(self):
        if self.cancelled.is_set():
            raise JobCancelled('用户已请求停止；未启动下一次模型调用或工具。')

    def resume_steer(self, run_id, job_id):
        """Explicitly continue a correction left behind by recovery or restart.

        Only a message that actually changed the request is continued. The user
        asking for progress must not be served by starting work they did not ask
        for, however long the service was down; a new request is still the way to
        ask for new work.
        """
        with self.lock:
            self.initialize()
            current = self.get(run_id, job_id)
            if current['successor_job_id']:
                return self.get(run_id, current['successor_job_id'])
            if current['steer_status'] != 'blocked' or current['status'] not in TERMINAL:
                raise PipelineError('steer_state', '当前没有待接续的调整，请刷新任务状态。')
            if current['open_attempts']:
                raise PipelineError('steer_recovery', '请先在恢复面板处理未完成阶段。')
            corrections = [r for r in self.store.read_events(run_id)
                           if r.get('job_id') == job_id and r.get('kind') == 'job_steer_received']
            pending = self._pending_inputs(run_id, current, corrections)
            if not pending:
                raise PipelineError('steer_state', '这条中途消息不是修改要求，没有需要接续的工作；'
                                                   '请直接发送新的指令。')
            return self._submit_pending_inputs(run_id, current, pending)

    def _work(self, run_id, job_id, text, runner):
        def emit(event):
            self._event(run_id, job_id, 'job_progress', event=event)
        status, reason = 'succeeded', ''
        runtime = AgentRuntime(self.store, run_id)
        activation = bind_task(job_id)
        try:
            with self.lock:
                self.check_cancel()
                runtime.transition(job_id, 'running')
                for message in runtime.pending_messages(job_id):
                    runtime.ack_message(job_id, message['message_id'])
            result = runner(text, emit, self.check_cancel)
            self.check_cancel()
            if result.stopped_because == 'repeated_failure':
                # Why it stopped is the actionable fact — more so than the
                # unfinished deliverable, which is a consequence of it. Words
                # the user can act on, not an English enum.
                status, reason = 'blocked', (
                    '同一问题连续失败，本轮已停止重试：请查看团队工作记录里的具体错误，'
                    '改掉原因后再发送新的指令。')
            elif result.stopped_because != 'done':
                status, reason = 'blocked', f'本轮结束于 {result.stopped_because}；已保存进度，请查看结果后发送新的指令。'
            deliverable = getattr(result, 'deliverable', None)
            if deliverable:
                self._event(run_id, job_id, 'job_deliverable', deliverable=deliverable)
                if (deliverable['status'] in {'incomplete', 'input_ready', 'awaiting_authorisation',
                                             'in_progress', 'failed', 'unknown'}
                        and result.stopped_because != 'repeated_failure'):
                    labels = {'input_ready': '分镜输入已齐备，媒体尚未生成',
                              'awaiting_authorisation': '等待你确认准确生成范围和费用',
                              'in_progress': '媒体仍在执行，只能查询原任务',
                              'failed': '媒体生成失败，请查看原任务错误',
                              'unknown': '媒体结果待核实，请查询原任务'}
                    status = 'blocked'
                    reason = labels.get(deliverable['status'], '交付未完成') + '：' + '、'.join(deliverable['missing'])
            emit({'type': 'done', 'text': result.text, 'rounds': result.rounds,
                  'stopped_because': result.stopped_because, 'tools': result.used_tools})
        except JobCancelled as exc:
            status, reason = 'cancelled', str(exc)
            emit({'type': 'cancelled', 'message': reason})
        except BudgetExceeded as exc:
            status, reason = 'blocked', str(exc)
            emit({'type': 'blocked', 'message': reason})
        except NonRetryableStageError as exc:
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
            # An error (including non-Orchestrator runners) may leave an
            # independent child in a provider call. Keep active and the writer
            # lease until the child has saved its actual outcome and returned.
            self._settle_roles(run_id, job_id,
                               stop=status != 'succeeded' or self.cancelled.is_set())
            with self.lock:
                # Cancellation can arrive after the last provider check but
                # before this lock. It must remain a stop, not a completion error.
                if self.cancelled.is_set() and status in {'succeeded', 'blocked'}:
                    status, reason = 'cancelled', '用户已请求停止；已保存的产物保留，不自动重跑。'
                task_status = {'succeeded': 'completed', 'failed': 'failed',
                               'blocked': 'waiting', 'cancelled': 'cancelled'}.get(status, 'interrupted')
                try:
                    runtime.transition(job_id, task_status, reason=reason)
                except PipelineError as exc:
                    # A missing child result is a real defect, not a completed turn.
                    status, reason = 'failed', str(exc)
                    runtime.transition(job_id, 'failed', reason=reason)
                self._event(run_id, job_id, 'job_status', status=status, reason=reason)
                self.active = None
                rows = self.store.read_events(run_id)
                corrections = [r for r in rows
                               if r.get('job_id') == job_id and r.get('kind') == 'job_steer_received']
                discarded = any(r.get('job_id') == job_id and r.get('kind') == 'job_steer_discarded'
                                for r in rows)
                discarded = self._settle_stopped_steer(run_id, job_id, corrections, discarded)
                resumed = _pending_steer_work(corrections)
                completed = self.get(run_id, job_id)
                pending = [] if discarded else self._pending_inputs(run_id, completed, corrections)
                # Explicit cancellation revokes inherited inbox entries too.
                if discarded or (status == 'cancelled' and not resumed and not self.closing):
                    pending = []
                if pending and self.closing:
                    self._event(run_id, job_id, 'job_steer_blocked',
                                reason='服务停止，已收到的消息尚未处理；请核对状态后明确接续。')
                if pending and not discarded and not self.closing:
                    # The old user's instruction is included even if cancellation
                    # happened before Orchestrator.send could write it to chat.
                    try:
                        if self.get(run_id, job_id)['open_attempts']:
                            raise PipelineError('steer_recovery', '原任务留下未完成阶段，请先在恢复面板处理，再发送调整要求。')
                        self._submit_pending_inputs(run_id, completed, pending)
                    except (PipelineError, ValueError, CodexUnavailable, ConnectionUnavailable) as exc:
                        self._event(run_id, job_id, 'job_steer_blocked', reason=str(exc))
                        self._event(run_id, job_id, 'job_progress', event={
                            'type': 'agent_control', 'message': '调整已保存，但未能自动接续；请查看恢复提示。'})
                # The current turn only records a decision. Start its exactly-
                # once successor after releasing all run locks, in the same
                # jobs->run lock order as UI continuation. Never restart on
                # service initialization; failed creation stays explicit pending.
                source = self.get(run_id, job_id)['request_id']
                followups = [e for e in self.store.read_events(run_id)
                             if e['kind'] == 'decision_followup_requested'
                             and e.get('source_request_id') == source]
                for followup in followups:
                    if (self.closing or pending or any(r.get('steer_class') != 'status' for r in corrections)
                            or discarded or status == 'cancelled'):
                        self._event(run_id, job_id, 'decision_followup_suppressed',
                                    request_id=followup['request_id'],
                                    reason='中途消息或停止已改变本轮接续要求，原决定接续未自动启动。')
                        continue
                    try:
                        if self.get(run_id, job_id)['open_attempts']:
                            raise PipelineError('decision_recovery', '有尚未结束的阶段，请先查看恢复记录。')
                        self.submit_decision_followup(run_id, followup['text'], followup['request_id'],
                                                      parent_job_id=job_id)
                    except (PipelineError, ValueError, CodexUnavailable, ConnectionUnavailable) as exc:
                        self._event(run_id, job_id, 'decision_followup_blocked',
                                    request_id=followup['request_id'], reason=str(exc))
                if self.closing:
                    if self._role_futures():
                        self._start_close_drain_locked()
                    else:
                        self.lease.close()
            reset_task(activation)

    def cancel(self, run_id, job_id):
        with self.lock:
            current = self.get(run_id, job_id)
            if current['status'] in TERMINAL:
                successor = current.get('successor_job_id')
                if successor:
                    return self.cancel(run_id, successor)
                return current
            if self.active != (run_id, job_id):
                raise PipelineError('job_not_active', '任务不在此进程运行；请重新加载中断记录。')
            self.cancelled.set()
            AgentRuntime(self.store, run_id).request_stop(job_id)
            if current['steer_status'] == 'received' or (current.get('job_payload') or {}).get('pending_user_inputs'):
                self._event(run_id, job_id, 'job_steer_discarded')
            if current['status'] != 'cancelling':
                self._event(run_id, job_id, 'job_status', status='cancelling',
                            reason=('停止本地制作等待；提供方可能仍在生成并计费。请从制作记录查询原任务并核实用量。'
                                    if current.get('job_kind') in {'production_generation', 'production_batch_generation', 'production_refresh'} else
                                    '等待当前调用结算后停止，不强杀在途请求；未知用量保持待核实。'))
            return self.get(run_id, job_id)

    def busy(self, run_id):
        with self.lock:
            return ((self.active is not None and self.active[0] == run_id)
                    or bool(self._role_futures(run_id)))

    def event_marks(self, run_id, job_id, after_seq=0):
        """Small, redaction-safe subscription cursor; details stay on GET job."""
        with self.lock:
            with self.store.run_lock(run_id):
                rows = [row for row in self.store.read_events(run_id)
                        if row.get('job_id') == job_id]
            if not any(row['kind'] == 'job_created' for row in rows):
                raise PipelineError('job_missing', '没有找到这次执行。')
            return [{'seq': row['seq'], 'kind': row['kind']}
                    for row in rows if row['seq'] > after_seq]

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
                if self._role_futures():
                    self._start_close_drain_locked()
                else:
                    self.lease.close()

    def close_when_idle(self):
        """Atomically stop accepting jobs for reload, without cancelling work."""
        with self.lock:
            if self.active is not None or self._role_futures():
                return False
            self.closing = True
            return True
