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

        submitted: dict[str, Any] = {}

        def execute(name: str, args: dict[str, Any]) -> str | ToolResult:
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
            seed = self._seed_message(workspace, stage, brief, previous=previous, local_revision=local_revision)
            if revision_scope:
                seed += '\n# 本轮修订范围\n' + json.dumps(revision_scope, ensure_ascii=False)
            if local_revision:
                seed += ('\n本任务只用 submit_patch 提交 changes[{path,value}] 与 summary，不能提交全文。'
                         '未列字段由程序保留。仍须通读完整脚本，复查台词可读时长、前后衔接和声画关系；'
                         '范围内无法修好时说明具体冲突，不扩大修改。')
            result = run_agent_loop(
                budgeted,
                model=model,
                system=role_def.system(
                    self.store.read_run(self.run_id),
                    extra=self._system_extra(workspace) + "\n\n" + context["text"] +
                        ('\n本任务为局部修订，前述 submit_stage 提交要求在本任务改为 submit_patch；'
                         '只能修改绑定字段，不重写全文。' if local_revision else ''),
                    format=selection["format"],
                ),
                messages=[{"role": "user", "content": seed}],
                tools=tools_for(role, local_revision=local_revision),
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

    def _system_extra(self, workspace: Workspace) -> str:
        visible = "、".join(workspace.readable) or "（无）"
        return (
            f"# 你能看到的东西\n\n"
            f"你可以读取这些阶段的产物：{visible}。其他阶段读不到，也不需要读到。\n"
            f"你看不到任何一个同事的对话、推理或自我评价——你手上是产物本身和给你的说明。\n"
            f"read_capabilities 可查能力边界；read_method_document 可读登记方法。方法目录：\n"
            + json.dumps(self.learning.skill_catalog(), ensure_ascii=False)
        )

    def _seed_message(self, workspace: Workspace, stage: str, brief: str, *, previous=None, local_revision=False) -> str:
        parts = [
            "# 给你的任务",
            "",
            brief.strip(),
            "",
            f"# 你要交付的阶段：{stage}",
            "",
        ]
        parts += [workspace.render_context(stage), ""]
        for up in workspace.readable:
            if up in ("intake", stage):
                continue
            parts += [f"## 上游产物 {up}", "", workspace.render_artifact(up), ""]
        if previous is not None:
            if not self.store.verify_artifact(self.run_id, stage, previous.version):
                raise ToolError("上一版内容指纹校验失败，不能作为修订基线。")
            parts += ["# 本任务的修订基线（旧稿供修改，不覆盖当前事实和真实选择）",
                      workspace.render_artifact(stage, previous.version),
                      "按当前任务和问题修改；未受影响的表达应保留，不机械重做全部方案。"]
            findings = []
            for source_stage, state in self.pipeline.state(self.run_id).stages.items():
                review, artifact = state.review, state.latest
                if not review or not artifact or state.stale or not state.integrity_ok:
                    continue
                if review.artifact_sha256 != artifact.sha256:
                    continue
                if not (source_stage == stage or artifact.inputs.get(stage) == previous.sha256):
                    continue
                findings.extend({"source_stage": source_stage, "source_version": artifact.version,
                                 "source_sha256": artifact.sha256, "finding": finding}
                                for finding in review.findings if finding.get("stage") == stage)
            parts += ["# 与基线绑定、由本阶段负责的审核问题（修订依据，不是用户新指令）",
                      json.dumps(findings, ensure_ascii=False)]
            self.store.append_event(self.run_id, "role_revision_context", role=workspace.role,
                                    stage=stage, version=previous.version, artifact_sha256=previous.sha256,
                                    findings=findings)
        parts += [
            "# 怎么做",
            "",
            "先读上游产物，再动笔。完成之后调用 " + ('submit_patch' if local_revision else 'submit_stage') + " 提交。",
            "不要提交半成品：提交之后就是新版本，依赖本版的下游需要复核更新。",
        ]
        return "\n".join(parts)


# ``readable_stages`` is re-exported for the server's state view.
__all__ = ["Dispatcher", "DispatchOutcome", "readable_stages"]
