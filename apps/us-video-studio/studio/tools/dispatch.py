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
import hashlib
import json
from typing import Any, Callable

from ..agents.budget import BudgetedClient
from ..agents.client import JobCancelled, ModelClient, SafePreflightError, run_agent_loop
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


@dataclass
class DispatchOutcome:
    stage: str
    role: str
    submitted: str | None = None
    summary: str = ""
    limitations: list[str] = field(default_factory=list)
    text: str = ""
    stopped_because: str = ""
    rounds: int = 0
    error: str = ""
    impact: dict = field(default_factory=dict)

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

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is not None:
            self.on_event(event)

    # ------------------------------------------------------------- public -- #

    def dispatch(self, stage: str, brief: str, *, revision_scope=None) -> DispatchOutcome:
        if stage not in STAGE_SPEC:
            raise ToolError(f"unknown stage: {stage!r}")
        role = OWNER[stage]
        if role == "orchestrator":
            raise ToolError(
                f"{stage} is owned by the orchestrator. Write it directly with write_stage "
                f"— it is the intake conversation, not delegated work."
            )
        return self.run_role(stage, role, brief, revision_scope=revision_scope)

    # ------------------------------------------------------------ internal -- #

    def run_role(self, stage: str, role: str, brief: str, *, revision_scope=None) -> DispatchOutcome:
        if self.check_cancel:
            self.check_cancel()
        role_def = ROLES[role]
        outcome = DispatchOutcome(stage=stage, role=role)
        local_revision = bool(revision_scope and revision_scope['mode'] == 'local')
        if revision_scope:
            if revision_scope['stage'] != stage:
                raise ToolError('派工阶段与修订计划不一致。')
            check_base(self.store, self.pipeline, self.run_id, revision_scope)
        selection = creative_selection(self.store, self.run_id)
        if not production_allowed(selection):
            raise ToolError("当前明确路由只允许读取/反馈；先明确选择 new/continue/edit 和交付格式，才能派工。")
        context = self.learning.prepare_context(
            self.run_id, role, **selection,
            purpose="message" if role == "director" else "storyboard",
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

        workspace = Workspace(self.store, self.pipeline, self.run_id, role, target_stage=stage)
        previous = self.store.latest_artifact(self.run_id, stage)
        context_refs = [context["evidence_ref"]]
        model = self.settings.model_for(role)
        budgeted = BudgetedClient(
            self.client, self.ledger, run_id=self.run_id, role=role, stage=stage
        )
        task_tools = tools_for(role, local_revision=local_revision)
        allowed_tools = {tool['name'] for tool in task_tools}

        submitted: dict[str, Any] = {}

        def execute(name: str, args: dict[str, Any]) -> str | ToolResult:
            if name not in allowed_tools:
                raise ToolError(f'{role} 的本次任务不能调用 {name}。')
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
            if name in ("submit_stage", "submit_patch"):
                if local_revision != (name == 'submit_patch'):
                    raise ToolError('本次仅允许绑定范围的字段补丁。' if local_revision else '本次未绑定局部修改范围。')
                changes = []
                with self.store.run_lock(self.run_id):
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
                    )
                    if revision_scope:
                        outcome.impact = record_applied(self.store, self.pipeline, self.run_id, revision_scope, artifact, changes)
                submitted["key"] = f"{stage}-v{artifact.version}"
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
                method_context=context)
            system = role_def.system(self.store.read_run(self.run_id),
                                     extra=task_context.system_extra, format=selection['format'])
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
                expected_result={'artifact_stage': stage, 'review_required': STAGE_SPEC[stage].gated},
                context={'sources': task_context.sources,
                         'method_ref': context['evidence_ref'],
                         'method_sha256': context['snapshot_sha256'],
                         'seed_sha256': task_context.seed_sha256,
                         'system_sha256': hashlib.sha256(system.encode('utf-8')).hexdigest()})
            result = run_agent_loop(
                budgeted,
                model=model,
                system=system,
                messages=[{"role": "user", "content": task_context.seed}],
                tools=task_tools,
                execute_tool=execute,
                max_rounds=self.settings.max_tool_rounds,
                max_continuations=self.settings.max_continuations,
                check_cancel=self.check_cancel,
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
        except (BudgetExceeded, JobCancelled, SafePreflightError) as exc:
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
            raise
        except PipelineError as exc:
            outcome.error = str(exc)
            self._close_attempt(attempt, f"派工中断：{exc}")
            return outcome
        except Exception as exc:
            outcome.error = f"{type(exc).__name__}: {exc}"
            self._close_attempt(attempt, f"派工失败：{type(exc).__name__}")
            return outcome

        outcome.text = result.text
        outcome.stopped_because = result.stopped_because
        outcome.rounds = result.rounds
        if "key" in submitted:
            if not outcome.impact:
                outcome.impact = work_plan(self.pipeline, self.run_id)
            outcome.submitted = submitted["key"]
            outcome.summary = submitted["summary"]
            outcome.limitations = submitted["limitations"]
            self.store.append_event(
                self.run_id,
                "dispatch_done",
                task_id=attempt.key,
                stage=stage,
                role=role,
                artifact=outcome.submitted,
                rounds=result.rounds,
            )
        else:
            self._close_attempt(
                attempt,
                f"{role} 结束于 {result.stopped_because}，没有提交 {stage}",
            )
        return outcome

    # --------------------------------------------------------------- parts -- #

    def _close_attempt(self, attempt, reason: str) -> None:
        try:
            self.pipeline.reject(self.run_id, attempt.stage, attempt.token, reason)
        except PipelineError:
            pass  # already closed; the state is what matters, not this call

# ``readable_stages`` is re-exported for the server's state view.
__all__ = ["Dispatcher", "DispatchOutcome", "readable_stages"]
