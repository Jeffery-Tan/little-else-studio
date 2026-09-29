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
from pathlib import Path
from typing import Any, Callable

from .agents.client import JobCancelled, ModelError, NonRetryableStageError
from .agents.codex_client import CodexUnavailable
from .connections import ConnectionUnavailable
from .models import BudgetExceeded, PipelineError, STAGES
from .steer_intent import classify, continuation_allowed, needs_stop
from .store import Store

TERMINAL = frozenset({'succeeded', 'failed', 'blocked', 'cancelled', 'interrupted'})


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

    def initialize(self):
        with self.lock:
            if self.closing:
                raise PipelineError('closing', '此服务正在停止，请连接当前制片室服务。')
            if self.initialized:
                return
            self.lease.acquire()
            try:
                for run in self.store.list_runs():
                    all_rows = self.store.read_events(run.run_id)
                    for job in self.list(run.run_id):
                        if job['status'] not in TERMINAL:
                            self._event(run.run_id, job['job_id'], 'job_status', status='interrupted',
                                        reason='服务曾中断；未自动重发。先检查调用台账，再发送新的继续指令。')
                        rows = [r for r in all_rows if r.get('job_id') == job['job_id']]
                        steers = [r for r in rows if r['kind'] == 'job_steer_received']
                        settled = any(r['kind'] in {'job_steer_discarded', 'job_steer_blocked',
                                                    'job_steer_answered'} for r in rows)
                        if (steers and not settled
                                and not any(r.get('kind') == 'job_created'
                                            and r.get('parent_job_id') == job['job_id'] for r in all_rows)):
                            if any(continuation_allowed(r) for r in steers):
                                self._event(run.run_id, job['job_id'], 'job_steer_blocked',
                                            reason='服务中断，调整已保存但未自动接续；检查进度后请明确接续。')
                            else:
                                # A question or a stop is not a correction waiting
                                # to be resumed. Asking the user to click "continue"
                                # for it would start work they did not ask for.
                                self._event(run.run_id, job['job_id'], 'job_steer_answered',
                                            classes=[r.get('steer_class') or 'work' for r in steers],
                                            reason='服务中断；这条中途消息不是修改要求，没有待接续的工作。')
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
        corrections = []
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
            if row['kind'] == 'job_steer_blocked':
                out['steer_status'] = 'blocked'
                out['steer_reason'] = row['reason']
            if row['kind'] == 'job_steer_discarded':
                out['steer_status'] = 'discarded'
            if row['kind'] == 'job_steer_answered':
                out['steer_status'] = 'answered'
                out['steer_answered_classes'] = row.get('classes')
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
        out['can_continue'] = out['can_continue'] and not out['open_attempts']
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

    def submit(self, run_id, text, request_id, *, kind='conversation', payload=None,
               prepare=None, parent_job_id=None):
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
            if self.active is not None:
                raise PipelineError('job_busy', f'制片室正在运行 {self.active[0]} 的任务；本次未排队、未调用模型。')
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
                            job_payload=payload, parent_job_id=parent_job_id)
            self.active = (run_id, job_id)
            self.cancelled = threading.Event()
            self._event(run_id, job_id, 'job_status', status='running')
            threading.Thread(target=self._work, args=(run_id, job_id, text, runner), daemon=True).start()
            return self.get(run_id, job_id)

    def steer(self, run_id, job_id, text, request_id, expected_goal_revision,
              feedback_target=None, validate_target=None):
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
                        or prior.get('feedback_target') != feedback_target):
                    raise PipelineError('request_conflict', '相同 request_id 对应另一条调整指令。')
                return current
            if self.active != (run_id, job_id) or current['status'] not in {'running', 'cancelling'}:
                raise PipelineError('job_not_active', '原任务已经结束，请刷新后发送新的指令。')
            if current['status'] == 'cancelling' and current['steer_status'] != 'received':
                raise PipelineError('steer_scope', '当前任务已经要求停止；请等待结束后发送新指令。')
            if current['steer_status'] == 'discarded':
                raise PipelineError('steer_scope', '当前任务已要求停止，请等待结束后重新发送。')
            if expected_goal_revision != current['goal_revision']:
                raise PipelineError('goal_conflict', '任务目标已更新，请刷新后按当前目标调整。')
            if len(rows) >= 8 or sum(len(r['text']) for r in rows) + len(text) > 12000:
                raise PipelineError('steer_limit', '这轮补充内容已经较长，请等当前任务结束后发起新对话。')
            if feedback_target and validate_target:
                validate_target()
            acknowledged = {
                'status': '已收到你的询问。正在执行的任务不会被中断，主制片会在本轮结束前回答当前真实进展。',
                'stop': '已收到停止要求，正在安全收尾；提供方可能仍在生成并计费，可从制作记录查询原任务。',
            }.get(classification['class'],
                  '已收到调整，等待当前调用结束后按新要求继续。')
            with self.store.run_lock(run_id):
                self._event(run_id, job_id, 'job_steer_received', text=text, request_id=request_id,
                            expected_goal_revision=expected_goal_revision,
                            goal_revision=expected_goal_revision + 1,
                            feedback_target=feedback_target,
                            steer_class=classification['class'],
                            steer_reason=classification['reason'])
                self._event(run_id, job_id, 'job_progress', event={
                    'type': 'agent_control', 'message': acknowledged})
                if needs_stop(classification):
                    self.cancelled.set()
            if current['status'] != 'cancelling' and needs_stop(classification):
                self._event(run_id, job_id, 'job_status', status='cancelling',
                            reason=('已记录中途意见，等待当前调用结束；随后主制片按当前真实进展处理。'
                                    if job_kind != 'conversation' else
                                    '正在停止旧任务，随后按调整后的目标继续。'))
            return self.get(run_id, job_id)

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
            if not any(continuation_allowed(r) for r in corrections):
                raise PipelineError('steer_state', '这条中途消息不是修改要求，没有需要接续的工作；'
                                                   '请直接发送新的指令。')
            return self.submit(run_id, _continuation_text(current['text'], corrections,
                               current.get('job_kind', 'conversation')),
                               f'steer:{job_id}', parent_job_id=job_id)

    def _work(self, run_id, job_id, text, runner):
        def emit(event):
            self._event(run_id, job_id, 'job_progress', event=event)
        status, reason = 'succeeded', ''
        try:
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
                if deliverable['status'] == 'incomplete' and result.stopped_because != 'repeated_failure':
                    status, reason = 'blocked', '交付未完成：' + '、'.join(deliverable['missing'])
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
            with self.lock:
                self._event(run_id, job_id, 'job_status', status=status, reason=reason)
                self.active = None
                corrections = [r for r in self.store.read_events(run_id)
                               if r.get('job_id') == job_id and r.get('kind') == 'job_steer_received']
                discarded = any(r.get('job_id') == job_id and r.get('kind') == 'job_steer_discarded'
                                for r in self.store.read_events(run_id))
                resumed = [r for r in corrections if continuation_allowed(r)]
                if corrections and not resumed and not discarded:
                    # A status question is answered by the turn that was already
                    # running, and an explicit stop is a stop. Neither may be
                    # recycled into a new request the user did not ask for, and
                    # this must be recorded before the closing check below so a
                    # shutdown cannot leave the message looking unanswered.
                    self._event(run_id, job_id, 'job_steer_answered',
                                classes=[r.get('steer_class') or 'work' for r in corrections])
                if resumed and not discarded and not self.closing:
                    # The old user's instruction is included even if cancellation
                    # happened before Orchestrator.send could write it to chat.
                    completed = self.get(run_id, job_id)
                    revised = _continuation_text(completed['text'], resumed,
                                                 completed.get('job_kind', 'conversation'))
                    try:
                        if self.get(run_id, job_id)['open_attempts']:
                            raise PipelineError('steer_recovery', '原任务留下未完成阶段，请先在恢复面板处理，再发送调整要求。')
                        self.submit(run_id, revised, f'steer:{job_id}', parent_job_id=job_id)
                    except (PipelineError, ValueError, CodexUnavailable, ConnectionUnavailable) as exc:
                        self._event(run_id, job_id, 'job_steer_blocked', reason=str(exc))
                        self._event(run_id, job_id, 'job_progress', event={
                            'type': 'agent_control', 'message': '调整已保存，但未能自动接续；请查看恢复提示。'})
                if self.closing:
                    self.lease.close()

    def cancel(self, run_id, job_id):
        with self.lock:
            current = self.get(run_id, job_id)
            if current['status'] in TERMINAL:
                successor = current.get('successor_job_id')
                if successor and self.active == (run_id, successor):
                    return self.cancel(run_id, successor)
                return current
            if self.active != (run_id, job_id):
                raise PipelineError('job_not_active', '任务不在此进程运行；请重新加载中断记录。')
            self.cancelled.set()
            if current['steer_status'] == 'received':
                self._event(run_id, job_id, 'job_steer_discarded')
            if current['status'] != 'cancelling':
                self._event(run_id, job_id, 'job_status', status='cancelling',
                            reason=('停止本地制作等待；提供方可能仍在生成并计费。请从制作记录查询原任务并核实用量。'
                                    if current.get('job_kind') in {'production_generation', 'production_batch_generation', 'production_refresh'} else
                                    '等待当前调用结算后停止，不强杀在途请求；未知用量保持待核实。'))
            return self.get(run_id, job_id)

    def busy(self, run_id):
        with self.lock:
            return self.active is not None and self.active[0] == run_id

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
                self.lease.close()
