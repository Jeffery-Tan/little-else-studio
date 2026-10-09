"""Dispatch: hand a stage to the role that owns it, in its own context.

The sub-agent receives the upstream *artifacts* and the orchestrator's brief.
It does not receive the orchestrator's conversation, and it cannot see the
reviewer. Its only output channel is ``submit_stage``, bound to the single
attempt the dispatcher opened for it — it never handles the attempt token, and
it cannot submit a stage it does not own.

If the sub-agent finishes without submitting, the attempt is closed with a
stated reason rather than left dangling. An open attempt blocks the stage, so
leaving one behind would be a silent trap for the next dispatch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from concurrent.futures import TimeoutError as FutureTimeout
import copy
import hashlib
import json
import threading
import time
from typing import Any, Callable

from ..agents.budget import BudgetedClient
from ..agents.client import (JobCancelled, ModelClient, ModelError,
                             NonRetryableStageError, SafePreflightError, WriterRootClosureRejected, run_agent_loop)
from ..agents.roles import OWNER, ROLES, readable_stages, creative_selection, production_allowed
from ..config import Settings
from ..ledger import Ledger
from ..models import BudgetExceeded, PipelineError, STAGE_SPEC
from ..pipeline import Pipeline
from ..store import Store
from .registry import tools_for
from .execution import ToolResult
from .workspace import ToolError, Workspace
from ..revision_scope import check_base, patch_payload, record_applied, work_plan
from ..task_context import assemble_role_task
from ..agent_runtime import AgentRuntime, TERMINAL, current_task_id, bind_task, reset_task
from ..agent_sessions import AgentSessions, SessionJournal
from ..role_executor import DEFAULT_EXECUTOR

_HANDLES = {}
_HANDLES_LOCK = threading.Lock()


def in_flight_role_futures(store, run_id=None, parent_job_id=None):
    """Reserved/running workers; durable terminal status is not execution idle."""
    root = str(store.root.resolve())
    with DEFAULT_EXECUTOR.lock:
        active = tuple(DEFAULT_EXECUTOR.active.items())
    futures = []
    for (data_root, role_run_id, session_id), future in active:
        if data_root != root or (run_id is not None and role_run_id != run_id):
            continue
        if future.done():
            continue
        if parent_job_id is not None:
            row = AgentSessions(store, role_run_id).snapshot(session_id)
            if row.get('parent_job_id') != parent_job_id:
                continue
        futures.append(future)
    return tuple(futures)


@dataclass
class DispatchOutcome:
    stage: str
    role: str
    task_id: str = ""
    session_id: str = ""
    submitted: str | None = None
    artifact_version: int | None = None
    artifact_sha256: str = ""
    summary: str = ""
    limitations: list[str] = field(default_factory=list)
    text: str = ""
    stopped_because: str = ""
    rounds: int = 0
    error: str = ""
    failure_code: str = ""
    impact: dict = field(default_factory=dict)

    def receipt(self) -> str:
        """A compact, machine-readable handoff for the parent Agent."""
        status = ('cancelled' if self.stopped_because == 'cancelled' else
                  'failed' if self.error else 'submitted' if self.submitted else 'incomplete')
        return json.dumps({
            "ok": status == "submitted",
            "status": status,
            "stage": self.stage,
            "role": self.role,
            "task_id": self.task_id or None,
            "session_id": self.session_id or None,
            "artifact": ({"key": self.submitted, "version": self.artifact_version,
                          "sha256": self.artifact_sha256}
                         if self.submitted else None),
            "summary": self.summary[:800],
            "summary_truncated": len(self.summary) > 800,
            "limitations": self.limitations[:5],
            "limitations_truncated": len(self.limitations) > 5,
            "stop_reason": self.stopped_because or ("error" if self.error else ""),
            "error": self.error or None,
            **({'failure_code': self.failure_code, 'retryable': False} if self.failure_code else {}),
            "next": self.impact,
            "read_more": (f"read_artifact(stage={self.stage}, version={self.artifact_version})"
                          if self.submitted else None),
        }, ensure_ascii=False)

    def render(self) -> str:
        if self.error:
            return f"派工失败（{self.stage} → {self.role}）：{self.error}"
        if not self.submitted:
            return (
                f"{self.role} 结束了本轮，但没有提交 {self.stage}，阶段工作未完成"
                f"（stopped_because={self.stopped_because}）。它的最后输出：\n\n{self.text}"
            )
        out = [f"已提交 {self.submitted}（{self.stage} → {self.role}）。", f"摘要：{self.summary}"]
        if self.limitations:
            out.append("它自述的局限：")
            out += [f"- {x}" for x in self.limitations]
        if self.text:
            out.append(f"\n它的说明：\n{self.text}")
        if self.impact:
            out.append("当前依赖与下一步：" + json.dumps(self.impact, ensure_ascii=False))
        return "\n".join(out)


class Dispatcher:
    """Runs role agents. One instance per run."""

    def __init__(
        self,
        store: Store,
        pipeline: Pipeline,
        settings: Settings,
        client: ModelClient,
        ledger: Ledger,
        run_id: str,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        check_cancel: Callable[[], None] | None = None,
        learning_service=None,
    ):
        self.store = store
        self.pipeline = pipeline
        self.settings = settings
        self.client = client
        self.ledger = ledger
        self.run_id = run_id
        self.on_event = on_event
        self.check_cancel = check_cancel
        if learning_service is None:
            from ..learning import LearningService
            learning_service = LearningService(store)
        self.learning = learning_service
        self._sessions = AgentSessions(store, run_id)
        self._session_id = None
        self._resume_messages = []

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is not None:
            self.on_event(event)

    # ------------------------------------------------------------- public -- #

    def dispatch(self, stage: str, brief: str, *, revision_scope=None,
                 user_input_id=None, turn_scope=None, session_id=None) -> DispatchOutcome:
        if stage not in STAGE_SPEC:
            raise ToolError(f"unknown stage: {stage!r}")
        role = OWNER[stage]
        if role == "orchestrator":
            raise ToolError(
                f"{stage} is owned by the orchestrator. Write it directly with write_stage "
                f"— it is the intake conversation, not delegated work."
            )
        return self.run_role(stage, role, brief, revision_scope=revision_scope,
                             user_input_id=user_input_id, turn_scope=turn_scope, session_id=session_id)

    def sessions(self):
        return self._sessions.sessions()

    def snapshot(self):
        return self._sessions.snapshot()

    def session_contract(self, session_id):
        return self._sessions.contract(session_id)

    def message_session(self, session_id, message_id, text, *, source_message_id=None):
        return self._sessions.post_message(session_id, message_id, text, source_message_id=source_message_id)

    def cancel_session(self, session_id, *, reason='User stopped this role session'):
        return self._sessions.cancel(session_id, reason=reason)

    def _key(self, session_id):
        return (str(self.store.root.resolve()), self.run_id, session_id)

    def _saved_outcome(self, session_id):
        row = self._sessions.snapshot(session_id)
        outcome = DispatchOutcome(stage=row['stage'], role=row['role'], session_id=session_id,
                                  task_id=row.get('task_id') or '', stopped_because=row['status'])
        artifact = next((a for a in self.store.list_artifacts(self.run_id, row['stage'])
                         if a.attempt == row.get('task_id')), None)
        if artifact:
            outcome.submitted, outcome.artifact_version = artifact.key, artifact.version
            outcome.artifact_sha256, outcome.summary = artifact.sha256, artifact.summary
            outcome.limitations = artifact.limitations
        if row['status'] == 'failed':
            outcome.error = row.get('reason') or 'Role execution failed'
            outcome.failure_code = row.get('failure_code') or ''
        return outcome

    def wait_session(self, session_id, timeout_seconds=0, *, propagate_failure=False):
        deadline = time.monotonic() + min(30, max(0, float(timeout_seconds)))
        with _HANDLES_LOCK:
            future = _HANDLES.get(self._key(session_id))
        while future is not None and not future.done() and time.monotonic() < deadline:
            if self.check_cancel:
                self.check_cancel()
            try:
                future.result(timeout=min(.1, max(.001, deadline - time.monotonic())))
            except FutureTimeout:
                pass
            except Exception:
                break  # Failed child is exposed below as its durable receipt.
        row = self._sessions.snapshot(session_id)
        if propagate_failure and row.get('failure_code') == 'writer_root_closure_rejected':
            raise WriterRootClosureRejected('格式处理后的脚本提交已被业务校验拒绝，本轮已停止；'
                                             '请核对失败记录，不能在本轮自动重派。')
        done = row['status'] not in {'idle', 'queued', 'running'}
        outcome = self._saved_outcome(session_id) if done else None
        # Observing a failed child reports it; it does not throw its exception
        # into another independently running task.
        if future is not None and future.done() and not future.exception():
            outcome = future.result()
        return {'session': row, 'done': done, 'outcome': json.loads(outcome.receipt()) if outcome else None}

    def _wait_outcome(self, session_id):
        with _HANDLES_LOCK:
            future = _HANDLES[self._key(session_id)]
        while not future.done():
            if self.check_cancel:
                try:
                    self.check_cancel()
                except JobCancelled:
                    self.cancel_session(session_id, reason='Owning task stopped')
                    # Independent transports observe their own session stop.
                    while not future.done():
                        try:
                            future.result(timeout=.1)
                        except FutureTimeout:
                            pass
                    raise
            try:
                future.result(timeout=.05)
            except FutureTimeout:
                pass
        if self.check_cancel:
            self.check_cancel()
        return future.result()

    def start_role(self, stage, brief, *, revision_scope=None, user_input_id=None,
                   turn_scope=None, session_id=None, _resume=False, parent_job_id=None,
                   selection_override=None):
        if stage not in OWNER or OWNER[stage] == 'orchestrator':
            raise ToolError('Only specialist stages can have independent role sessions')
        if self.check_cancel:
            self.check_cancel()
        parent_id = current_task_id()
        parent = AgentRuntime(self.store, self.run_id).task(parent_id) if parent_id else None
        parent_job_id = parent_job_id or (parent['parent_job_id'] if parent else None)
        user_input_id = user_input_id or (parent['source_message_id'] if parent else None)
        if parent_job_id and any(row.get('parent_job_id') == parent_job_id and row['stage'] == stage
                and row.get('failure_code') == 'writer_root_closure_rejected'
                for row in self._sessions.sessions()):
            raise WriterRootClosureRejected('本轮脚本格式处理后的提交已被业务校验拒绝，不能自动重派；'
                                             '请保留失败事实并等待新的用户任务。')
        if parent_job_id and any(row.get('parent_job_id') == parent_job_id and row['stage'] == stage
                and (row['stop_requested'] or row['status'] == 'cancelled') for row in self._sessions.sessions()):
            raise ToolError('本轮该项角色任务已经停止，不能重新派工或自动重试；等待新的用户任务。')
        contract = {'stage': stage, 'brief': brief, 'revision_scope': revision_scope,
                    'user_input_id': user_input_id, 'turn_scope': turn_scope,
                    'selection': selection_override or creative_selection(self.store, self.run_id)}
        row = self._sessions.ensure(role=OWNER[stage], stage=stage, session_id=session_id,
            source_message_id=user_input_id, parent_job_id=parent_job_id, contract=contract)
        session_id = row['session_id']
        self._sessions.claim(session_id, resume=_resume, source_message_id=user_input_id,
                             parent_job_id=parent_job_id)
        worker = copy.copy(self)
        worker._session_id = session_id
        worker._resume_messages = self._sessions.recovery_messages(session_id) if _resume else []
        parent_check_cancel = self.check_cancel

        def check_worker_cancel():
            # A parent stop still owns every child. The session check adds
            # independent role cancellation without losing that boundary.
            if parent_check_cancel:
                parent_check_cancel()
            self._sessions.check_cancel(session_id)

        worker.check_cancel = check_worker_cancel
        if hasattr(self.client, '_snapshot'):
            worker.client = self.client._snapshot.client(check_cancel=worker.check_cancel,
                max_tokens=self.client.max_tokens, cancel_event=threading.Event())
        elif self.client.__class__.__module__.startswith('studio.'):
            worker.client = copy.copy(self.client)
            if hasattr(worker.client, 'check_cancel'):
                worker.client.check_cancel = worker.check_cancel
            if hasattr(worker.client, 'cancel_event'):
                worker.client.cancel_event = threading.Event()

        def execute():
            try:
                result = worker._run_role_inline(stage, OWNER[stage], brief, revision_scope=revision_scope,
                    user_input_id=user_input_id, turn_scope=copy.deepcopy(turn_scope),
                    selection_override=contract['selection'])
                result.session_id = session_id
                status = 'completed' if result.submitted else 'failed' if result.error else 'waiting'
                self._sessions.finish(session_id, status, reason=result.error or result.stopped_because,
                    artifact_receipt={'stage': stage, 'version': result.artifact_version,
                                      'sha256': result.artifact_sha256} if result.submitted else None)
                return result
            except JobCancelled:
                self._sessions.finish(session_id, 'cancelled', reason='Role execution stopped')
                return self._saved_outcome(session_id)
            except WriterRootClosureRejected as exc:
                self._sessions.finish(session_id, 'failed', reason=str(exc),
                                      failure_code='writer_root_closure_rejected')
                raise
            except BaseException as exc:
                self._sessions.finish(session_id, 'failed', reason=f'{type(exc).__name__}: {exc}')
                raise
        try:
            future = DEFAULT_EXECUTOR.submit(self._key(session_id), execute)
        except Exception as exc:
            self._sessions.finish(session_id, 'failed', reason=str(exc))
            raise
        with _HANDLES_LOCK:
            _HANDLES[self._key(session_id)] = future
        return self._sessions.snapshot(session_id)

    def run_role(self, stage, role, brief, *, revision_scope=None, user_input_id=None,
                 turn_scope=None, session_id=None):
        if OWNER.get(stage) != role:
            raise ToolError('Role must own its delegated stage')
        row = self.start_role(stage, brief, revision_scope=revision_scope, user_input_id=user_input_id,
                              turn_scope=turn_scope, session_id=session_id)
        return self._wait_outcome(row['session_id'])

    def resume_session(self, session_id, *, brief='', user_input_id=None, parent_job_id=None, revision_scope=None):
        with self.store.run_lock(self.run_id):
            row = self._sessions.snapshot(session_id)
            if user_input_id:
                from ..conversation_input import message
                self._sessions.require_resume_input(session_id, message(self.store, self.run_id, user_input_id),
                                                    revision_scope=revision_scope)
            if row['status'] in {'queued', 'running', 'cancelled'} or row['stop_requested']:
                raise ToolError('Only interrupted or waiting sessions can be explicitly continued')
            saved = self._saved_outcome(session_id)
            if saved.submitted:
                if user_input_id or self._sessions.pending(session_id):
                    raise ToolError(self._sessions.closed_input_reason(session_id))
                self._sessions.commit_artifact(session_id, {
                    'stage': row['stage'], 'version': saved.artifact_version, 'sha256': saved.artifact_sha256})
                self._sessions.finish(session_id, 'completed', artifact_receipt={
                    'stage': row['stage'], 'version': saved.artifact_version, 'sha256': saved.artifact_sha256})
                return saved
            old = self.pipeline.state(self.run_id).stages[row['stage']].open_attempt
            if old and old.key == row['task_id']:
                self._close_attempt(old, 'Explicit session continuation supersedes interrupted attempt')
            if row['task_id']:
                runtime = AgentRuntime(self.store, self.run_id)
                if runtime.task(row['task_id'])['status'] not in TERMINAL:
                    runtime.transition(row['task_id'], 'interrupted', reason='Explicit session continuation')
            contract = self._sessions.contract(session_id)
            from ..conversation_input import constraints_for_inputs, message, accept, start_execution
            if user_input_id:
                receipt = message(self.store, self.run_id, user_input_id)
                accept(self.store, self.run_id, receipt)
                start_execution(self.store, self.run_id, receipt)
            identities = [*self._sessions.source_messages(session_id)]
            if user_input_id:
                identities.append(user_input_id)
            scope = {**(contract.get('turn_scope') or {}),
                     'project_constraints': constraints_for_inputs(self.store, self.run_id, identities)}
            selection = contract.get('selection') or next((dict(event['selection']) for event in
                reversed(self.store.read_events(self.run_id)) if event['kind'] == 'creative_context_selected'
                and production_allowed(event['selection'])), creative_selection(self.store, self.run_id))
        row = self.start_role(row['stage'], brief or contract['brief'],
            revision_scope=contract.get('revision_scope'),
            user_input_id=user_input_id or row['source_message_id'], turn_scope=scope,
            session_id=session_id, _resume=True, parent_job_id=parent_job_id, selection_override=selection)
        return self._wait_outcome(row['session_id'])

    # ------------------------------------------------------------ internal -- #

    def _run_role_inline(self, stage: str, role: str, brief: str, *, revision_scope=None,
                 user_input_id=None, turn_scope=None, selection_override=None) -> DispatchOutcome:
        if self.check_cancel:
            self.check_cancel()
        role_def = ROLES[role]
        outcome = DispatchOutcome(stage=stage, role=role)
        local_revision = bool(revision_scope and revision_scope['mode'] == 'local')
        if revision_scope:
            if revision_scope['stage'] != stage:
                raise ToolError('派工阶段与修订计划不一致。')
            check_base(self.store, self.pipeline, self.run_id, revision_scope)
        selection = selection_override or creative_selection(self.store, self.run_id)
        decision_context = (turn_scope or {}).get('decision_context')
        if decision_context:
            from ..decision_continuation import validate
            decision_scope = validate(self.store, self.pipeline, self.run_id, decision_context)
            if stage not in decision_scope['allowed_stages']:
                raise ToolError('该阶段超出原 UI 决定的可逆准备范围。')
            selection = {'intent': decision_scope['intent'], 'format': decision_scope['format'],
                         'tags': [], 'case_ids': None}
        if not production_allowed(selection):
            raise ToolError("当前明确路由只允许读取/反馈；先明确选择 new/continue/edit 和交付格式，才能派工。")
        from ..conversation_input import project_constraints, constraints_for_scope
        constraints = ((turn_scope or {})['project_constraints']
                       if 'project_constraints' in (turn_scope or {}) else project_constraints(self.store, self.run_id))
        paths = [field['path'] for field in revision_scope['fields']] if local_revision else None
        if any(row['status'] == 'needs_clarification' for row in
               constraints_for_scope(constraints, stage=stage, paths=paths)):
            raise ToolError('本阶段的用户项目条件尚有冲突，须先澄清，不能沿旧条件继续派工。')
        reference = (turn_scope or {}).get('reference_selection')
        if reference:
            from ..creation_mode import context as reference_context, current
            reference_context(self.store, self.run_id, selection=reference)
            if current(self.store, self.run_id) != reference:
                raise ToolError('本任务绑定的参考与当前创作依据不一致，请回主制片核对，不能换用其他参考派工。')
        context = self.learning.prepare_context(
            self.run_id, role, **selection,
            purpose="message" if role == "director" else "storyboard",
            **({'reference_selection': reference} if reference else {}),
        )
        self._emit({"type": "learning_context", "role": role, "stage": stage,
                    "snapshot_sha256": context["snapshot_sha256"], "evidence_ref": context["evidence_ref"]})

        try:
            attempt = self.pipeline.start_attempt(self.run_id, stage, actor="orchestrator")
        except PipelineError as exc:
            # Preconditions are enforced by the pipeline, not by the dispatcher's
            # judgement, so the message is the real reason and is passed through.
            outcome.error = str(exc)
            return outcome
        outcome.task_id = attempt.key

        workspace = Workspace(self.store, self.pipeline, self.run_id, role, target_stage=stage)
        previous = self.store.latest_artifact(self.run_id, stage)
        context_refs = [context["evidence_ref"]]
        model = self.settings.model_for(role)
        budgeted = BudgetedClient(
            self.client, self.ledger, run_id=self.run_id, role=role, stage=stage
        )
        task_tools = tools_for(role, local_revision=local_revision, format=selection['format'])
        allowed_tools = {tool['name'] for tool in task_tools}
        runtime = AgentRuntime(self.store, self.run_id)
        parent_id = current_task_id()
        runtime_created = False
        try:
            runtime.create_task(attempt.key, role=role, stage=stage,
                session_id=self._session_id,
                parent_task_id=parent_id,
                parent_job_id=runtime.task(parent_id)['parent_job_id'] if parent_id else None,
                source_message_id=user_input_id,
                context_refs={'method_ref': context['evidence_ref'],
                              'method_sha256': context['snapshot_sha256']},
                allowed_tools=allowed_tools,
                expected_result={'artifact_stage': stage, 'user_decision_required': STAGE_SPEC[stage].gated})
            runtime_created = True
            if self._session_id:
                self._sessions.bind_attempt(self._session_id, attempt.key)
            runtime.transition(attempt.key, 'running')
        except Exception as exc:
            self._close_attempt(attempt, f'派工准备失败：{type(exc).__name__}')
            if runtime_created:
                task = runtime.task(attempt.key)
                if task['status'] not in TERMINAL:
                    runtime.transition(attempt.key, 'cancelled' if task['stop_requested'] else 'failed',
                                       reason=type(exc).__name__)
                if task['stop_requested']:
                    raise JobCancelled('Role task stopped before execution') from None
            raise
        # Parent runtime provenance also covers service-owned stage commands;
        # never substitute a global latest receipt while an older task runs.
        user_input_id = runtime.task(attempt.key)['source_message_id']
        if user_input_id:
            from ..conversation_input import message, accept, start_execution
            receipt = message(self.store, self.run_id, user_input_id)
            accept(self.store, self.run_id, receipt)
            start_execution(self.store, self.run_id, receipt)
        task_binding = bind_task(attempt.key)

        submitted: dict[str, Any] = {}
        source = {'message_id': user_input_id, 'needs_interpretation': False}

        def inbox(item):
            if source['needs_interpretation']:
                return False
            if not item.get('source_message_id'):
                return  # Non-user notes are context, never an authority source.
            from ..conversation_input import message, accept, start_execution
            receipt = message(self.store, self.run_id, item['source_message_id'])
            accept(self.store, self.run_id, receipt)
            start_execution(self.store, self.run_id, receipt)
            source.update(message_id=receipt['message_id'], needs_interpretation=True)
            self._sessions.activate_source(self._session_id, receipt['message_id'])

        journal = SessionJournal(self._sessions, self._session_id, inbox_handler=inbox) if self._session_id else None

        def check_user_limits():
            if decision_context:
                from ..decision_continuation import validate
                validate(self.store, self.pipeline, self.run_id, decision_context)
            from ..conversation_input import (message, interpretation_for, interpreted_task_constraints,
                                               constraints_for_inputs, task_constraints, validated_edit_scopes)
            if self.check_cancel:
                self.check_cancel()
            if self._session_id and self._sessions.pending(self._session_id):
                raise ToolError('新要求已收到但尚未进入本轮模型上下文；先处理下一步消息，不能提交旧方案。')
            if source['needs_interpretation']:
                raise ToolError('先解释本步骤收到的用户新要求，再在原任务允许范围内提交。')
            identities = (self._sessions.source_messages(self._session_id, current_turn=True) if self._session_id
                          else list(dict.fromkeys([user_input_id, source['message_id']])))
            for identity in identities:
                if not identity:
                    continue
                receipt = message(self.store, self.run_id, identity)
                interpreted = interpretation_for(self.store, self.run_id, identity)
                limits = interpreted_task_constraints(receipt, interpreted)
                frozen_scopes = validated_edit_scopes(receipt, interpreted)
                stage_scopes = [scope for scope in frozen_scopes if scope['stage'] == stage]
                if frozen_scopes and not stage_scopes:
                    raise ToolError('新编辑范围没有授权本角色阶段，不能沿旧任务提交。')
                if limits['readonly'] or ('allowed_stages' in limits and stage not in limits['allowed_stages']):
                    raise ToolError('本轮真实用户要求不允许修改这一阶段。')
                stage_local_only = task_constraints(receipt)['local_only'] or any(
                    scope['mode'] == 'local' for scope in stage_scopes)
                if stage_local_only and not local_revision:
                    raise ToolError('新要求仅限局部字段；整稿提交已停止，需由主制片绑定准确版本和字段后建立新修订任务。')
                if local_revision:
                    # Ordinary content paths are not editing authority. Only
                    # the exact target or source-bound typed edit scope owns it.
                    requested = []
                    target = receipt.get('feedback_target') or {}
                    if target.get('path'):
                        if target.get('stage', stage) != stage:
                            raise ToolError('新反馈指向另一阶段，不能沿本角色旧任务提交。')
                        if (target.get('version', revision_scope['version']) != revision_scope['version']
                                or target.get('artifact_sha256', revision_scope['sha256']) != revision_scope['sha256']):
                            raise ToolError('新反馈版本与本角色固定修订版本不一致。')
                        requested.append(target['path'])
                    for frozen in stage_scopes:
                        if (frozen['mode'] not in {'local', 'rework'}
                                or frozen['version'] != revision_scope['version']
                                or frozen['sha256'] != revision_scope['sha256']
                                or frozen['inputs'] != revision_scope['inputs']):
                            raise ToolError('新编辑范围与本角色固定阶段、版本或依据不一致，不能沿旧任务提交。')
                        # Rework permits a narrower plan on this exact base;
                        # the fixed local plan still limits every patch field.
                        if frozen['mode'] == 'local':
                            requested.extend(frozen['paths'])
                    allowed = [field['path'] for field in revision_scope['fields']]
                    if any(path not in allowed for path in requested):
                        raise ToolError('新要求超出本角色固定字段范围；需主制片建立新的准确修订计划。')
                    if identity != user_input_id and stage_local_only and not requested:
                        raise ToolError('新局部要求尚未绑定具体字段，先澄清或解释准确路径，不能猜测修改对象。')
            paths = [entry['path'] for entry in revision_scope['fields']] if local_revision else None
            constraints = constraints_for_inputs(self.store, self.run_id,
                self._sessions.source_messages(self._session_id) if self._session_id else identities,
                stage=stage, paths=paths)
            if any(row['status'] == 'needs_clarification' for row in constraints):
                raise ToolError('当前阶段存在尚未澄清的用户条件，不能提交。')

        def execute(name: str, args: dict[str, Any]) -> str | ToolResult:
            if name not in allowed_tools:
                raise ToolError(f'{role} 的本次任务不能调用 {name}。')
            if name == 'propose_task_interpretation':
                from ..conversation_input import message, record_task_interpretation
                if not source['message_id']:
                    raise ToolError('当前角色没有真实用户原话可供解释。')
                result = record_task_interpretation(self.store, self.run_id,
                    message(self.store, self.run_id, source['message_id']), args,
                    prior_user_input_ids=self._sessions.source_messages(self._session_id)
                        if self._session_id else ())
                source['needs_interpretation'] = False
                return json.dumps(result, ensure_ascii=False)
            if name == "read_capabilities":
                from .context import capabilities
                return json.dumps(capabilities(self.settings, role, local_revision=local_revision), ensure_ascii=False)
            if name == "read_method_document":
                document = self.learning.read_method_document(args["name"], role=role, run_id=self.run_id)
                if document.get("evidence_ref"):
                    context_refs.append(document["evidence_ref"])
                return json.dumps(document, ensure_ascii=False)
            if name == "read_artifact":
                if args["stage"] == stage:
                    if previous is None or args.get("version") not in (None, previous.version):
                        raise ToolError("修订只读取本任务开始时绑定的上一版；此版本不存在或不匹配。")
                    return workspace.render_artifact(stage, previous.version)
                return workspace.render_artifact(args["stage"], args.get("version"))
            if name == "list_materials":
                return workspace.render_materials()
            if name == 'read_role_history':
                if not self._session_id:
                    raise ToolError('当前角色没有绑定的历史会话。')
                return json.dumps(self._sessions.read_role_history(self._session_id, **args), ensure_ascii=False)
            if name in ("submit_stage", "submit_patch"):
                if local_revision != (name == 'submit_patch'):
                    raise ToolError('本次仅允许绑定范围的字段补丁。' if local_revision else '本次未绑定局部修改范围。')
                changes = []
                with self.store.run_lock(self.run_id):
                    check_user_limits()
                    if revision_scope:
                        check_base(self.store, self.pipeline, self.run_id, revision_scope)
                    if local_revision:
                        if args.get('plan_id', revision_scope['plan_id']) != revision_scope['plan_id']:
                            raise ToolError('补丁与本次计划不一致。')
                        args, changes = patch_payload(self.store, self.pipeline, self.run_id, revision_scope,
                                                      {k: v for k, v in args.items() if k != 'plan_id'})
                    artifact = self.pipeline.submit(
                        self.run_id, stage, attempt.token,
                        content=args.get("content", ""), summary=args.get("summary", ""),
                        evidence=list(dict.fromkeys([*(args.get("evidence") or []), *context_refs])),
                        limitations=args.get("limitations") or [],
                        structured=args.get("structured"), actor=f"{role}:{attempt.key}",
                        **({'writing_source_message_ids': list(dict.fromkeys(identity for identity in
                                (self._sessions.source_messages(self._session_id) if self._session_id
                                 else [user_input_id, source['message_id']]) if identity)),
                            'writing_format': selection['format'],
                            'local_writing_revision': local_revision} if stage == 'writing' else {}),
                    )
                    if self._session_id:
                        self._sessions.commit_artifact(self._session_id, {
                            'stage': stage, 'version': artifact.version, 'sha256': artifact.sha256})
                    if revision_scope:
                        outcome.impact = record_applied(self.store, self.pipeline, self.run_id, revision_scope, artifact, changes)
                submitted["key"] = f"{stage}-v{artifact.version}"
                submitted["version"] = artifact.version
                submitted["sha256"] = artifact.sha256
                submitted["summary"] = artifact.summary
                submitted["limitations"] = artifact.limitations
                return ToolResult(
                    f"已提交 {submitted['key']}。它现在不可修改；要改是新的版本，"
                    f"并且会让下游失效。你的工作完成了。", concludes_turn=True,
                )
            raise ToolError(f"role {role!r} has no tool named {name!r}")

        try:
            if revision_scope:
                check_base(self.store, self.pipeline, self.run_id, revision_scope)
            task_context = assemble_role_task(
                self.store, self.pipeline, self.learning, self.run_id, workspace,
                stage, brief, previous=previous, revision_scope=revision_scope,
                method_context=context, user_input_id=user_input_id, turn_scope=turn_scope,
                journal_constraints=journal.current_constraints() if journal else None)
            system = role_def.system(self.store.read_run(self.run_id),
                                     extra=task_context.system_extra, format=selection['format'],
                                     local_revision=local_revision)
            if revision_scope:
                check_base(self.store, self.pipeline, self.run_id, revision_scope)
            self.store.append_event(self.run_id, 'agent_task_prepared',
                schema_version=1, task_id=attempt.key,
                task_kind='stage_revision' if revision_scope else 'stage_creation',
                stage=stage, role=role,
                plan_id=revision_scope['plan_id'] if revision_scope else None,
                mode=revision_scope['mode'] if revision_scope else None,
                base={'version': revision_scope['version'], 'sha256': revision_scope['sha256']}
                     if revision_scope else None,
                inputs=attempt.inputs,
                allowed_tools=sorted(allowed_tools),
                expected_result={'artifact_stage': stage, 'user_decision_required': STAGE_SPEC[stage].gated},
                context={'sources': task_context.sources,
                         'method_ref': context['evidence_ref'],
                         'method_sha256': context['snapshot_sha256'],
                         'seed_sha256': task_context.seed_sha256,
                         'system_sha256': hashlib.sha256(system.encode('utf-8')).hexdigest(),
                         'seed_chars': len(task_context.seed),
                         'system_chars': len(system),
                         'tool_schema_chars': len(json.dumps(task_tools, ensure_ascii=False))})
            result = run_agent_loop(
                budgeted,
                model=model,
                system=system,
                messages=([{"role": "user", "content": task_context.seed}, *self._resume_messages,
                           {"role": "user", "content": task_context.seed}] if self._resume_messages
                          else [{"role": "user", "content": task_context.seed}]),
                tools=task_tools,
                execute_tool=execute,
                max_rounds=self.settings.max_tool_rounds,
                max_continuations=self.settings.max_continuations,
                check_cancel=self.check_cancel,
                session_journal=journal,
                parallel_read_tools=frozenset({'read_capabilities', 'read_artifact', 'list_materials', 'read_role_history'}),
                on_activity=lambda event: self._emit({"role": role, "stage": stage, **event}),
                on_turn=lambda t: self._emit(
                    {"type": "agent_turn", "role": role, "stage": stage,
                     "stop_reason": t.stop_reason,
                     "tools": [c.name for c in t.tool_calls]}
                ),
                on_tool=lambda r: self._emit(
                    {"type": "agent_tool", "role": role, "stage": stage, **r}
                ),
            )
        except (BudgetExceeded, JobCancelled, SafePreflightError, WriterRootClosureRejected) as exc:
            # Close the attempt *before* letting this through. An open attempt
            # blocks its stage, and an exhausted budget is precisely the case
            # where he raises the cap and expects the run to carry on — so
            # leaving one behind would strand it.
            #
            # Then re-raise rather than returning `outcome`. The handler below
            # would turn this into an ordinary dispatch failure, which the
            # orchestrator reads as retryable; `run_agent_loop` already decided
            # it is not. The reason carries the raw enum because
            # `vocabulary.rejection_sentence` keys off it to say 预算 rather
            # than print "[budget]".
            self._close_attempt(attempt, f"派工中断：{exc}")
            runtime.transition(attempt.key, 'cancelled' if isinstance(exc, JobCancelled) else 'failed',
                               reason=type(exc).__name__)
            raise
        except ModelError as exc:
            self._close_attempt(attempt, f"派工失败：{type(exc).__name__}")
            runtime.transition(attempt.key, 'failed', reason=type(exc).__name__)
            if exc.response_diagnostic or not exc.retryable:
                raise NonRetryableStageError(exc.public_message or '模型响应校验失败，本轮已停止自动重试。') from None
            if exc.status == 0 and 'usage unconfirmed' in exc.message:
                raise NonRetryableStageError(
                    '负责这一步的团队模型连接超时或中断，本阶段新版未保存；本次调用用量无法确认，'
                    '本轮已停止自动重试以避免重复计费。请检查连接后从当前阶段继续。') from None
            if exc.status == 200 and exc.message.startswith('output truncated at max_tokens'):
                raise NonRetryableStageError(
                    '负责这一步的团队模型输出达到单次上限，结果不完整，未保存为新版；'
                    '本轮已停止自动重试以避免重复计费，请从当前阶段继续。') from None
            if exc.status == 200 and exc.message.startswith('invalid'):
                # The provider was invoked, and usage may already be owed.
                # The orchestrator cannot repair malformed wire structure by
                # asking the same role again in the same turn.
                raise NonRetryableStageError(
                    '团队模型返回的结果读不出来，本轮已停止自动重试；'
                    '已保存调用记录，请检查连接后从当前阶段继续。') from None
            outcome.error = str(exc)
            return outcome
        except PipelineError as exc:
            outcome.error = str(exc)
            self._close_attempt(attempt, f"派工中断：{exc}")
            runtime.transition(attempt.key, 'failed', reason=type(exc).__name__)
            return outcome
        except Exception as exc:
            outcome.error = f"{type(exc).__name__}: {exc}"
            self._close_attempt(attempt, f"派工失败：{type(exc).__name__}")
            runtime.transition(attempt.key, 'failed', reason=type(exc).__name__)
            return outcome
        finally:
            reset_task(task_binding)

        outcome.text = result.text
        outcome.stopped_because = result.stopped_because
        outcome.rounds = result.rounds
        if "key" in submitted:
            if not outcome.impact:
                outcome.impact = work_plan(self.pipeline, self.run_id)
            outcome.submitted = submitted["key"]
            outcome.artifact_version = submitted["version"]
            outcome.artifact_sha256 = submitted["sha256"]
            outcome.summary = submitted["summary"]
            outcome.limitations = submitted["limitations"]
            # Artifact persistence and cancellation are separate facts. A stop
            # arriving after submit preserves the artifact, closes the role as
            # cancelled, and never leaves an open runtime task behind.
            with self.store.run_lock(self.run_id):
                task = runtime.task(attempt.key)
                try:
                    if task['stop_requested']:
                        raise JobCancelled('Role stopped after artifact was saved')
                    if self.check_cancel:
                        self.check_cancel()
                except JobCancelled:
                    runtime.transition(attempt.key, 'cancelled', reason='Stopped after artifact was saved')
                    raise
                runtime.transition(attempt.key, 'completed', artifact_receipt={
                    'stage': stage, 'version': outcome.artifact_version, 'sha256': outcome.artifact_sha256})
            self.store.append_event(
                self.run_id,
                "dispatch_done",
                task_id=attempt.key,
                stage=stage,
                role=role,
                artifact=outcome.submitted,
                artifact_sha256=outcome.artifact_sha256,
                rounds=result.rounds,
            )
        else:
            self._close_attempt(
                attempt,
                f"{role} 结束于 {result.stopped_because}，没有提交 {stage}",
            )
            runtime.transition(attempt.key, 'failed', reason=f'No stage submission: {result.stopped_because}')
        return outcome

    # --------------------------------------------------------------- parts -- #

    def _close_attempt(self, attempt, reason: str) -> None:
        try:
            self.pipeline.reject(self.run_id, attempt.stage, attempt.token, reason)
        except PipelineError:
            pass  # already closed; the state is what matters, not this call

# ``readable_stages`` is re-exported for the server's state view.
__all__ = ["Dispatcher", "DispatchOutcome", "readable_stages"]
