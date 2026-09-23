"""The independent reviewer.

Independence here is structural, not a matter of instruction:

* It runs in a **fresh message list**. The producer's conversation never enters
  it, and there is no code path that could add it.
* Its seed contains **artifact files and the user's brief** — evidence and
  standard — and nothing else. Prior reviews are deliberately withheld so a
  verdict is not anchored by the last one.
* Its tool list is ``read_artifact`` and ``finish_review``. It has no dispatch,
  no submit, and no approval. There is nothing for it to overwrite.

What it can do is *fail to produce a verdict*. If it never calls
``finish_review``, no review is recorded and the stage stays ``awaiting_review``.
That is the honest outcome: a review that did not happen must not look like one
that passed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Callable

from ..config import Settings
from ..ledger import Ledger
from ..models import BudgetExceeded, PipelineError, STAGE_SPEC
from ..pipeline import Pipeline
from ..store import Store
from ..tools.registry import tools_for
from ..tools.execution import ToolResult
from ..tools.workspace import ToolError, Workspace
from .budget import BudgetedClient
from .client import JobCancelled, ModelClient, SafePreflightError, run_agent_loop
from .roles import REVIEWER, creative_selection, production_allowed


@dataclass
class ReviewOutcome:
    stage: str
    verdict: str | None = None
    findings: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    rounds: int = 0
    stopped_because: str = ""
    error: str = ""

    def render(self) -> str:
        if self.error:
            return f"审核没有完成（{self.stage}）：{self.error}"
        if self.verdict is None:
            return (
                f"审核者没有给出判定（{self.stage}），因此没有审核记录被写入，"
                f"这一版仍然是「待审核」。它的最后输出：\n\n{self.text}"
            )
        if self.verdict == "pass":
            return (
                f"{self.stage} 审核通过。注意：通过只意味着它在自己声明的范围内成立，"
                f"不代表画面、音效、费用或 Jeffery 的批准通过。\n\n{self.text}"
            )
        lines = [f"{self.stage} 被退回，{len(self.findings)} 条问题："]
        for f in self.findings:
            lines.append(f"- [{f.get('kind', '?')}] {f.get('stage', '?')}：{f.get('issue', '')}")
        if self.text:
            lines += ["", self.text]
        return "\n".join(lines)


def run_review(
    *,
    store: Store,
    pipeline: Pipeline,
    settings: Settings,
    client: ModelClient,
    ledger: Ledger,
    run_id: str,
    stage: str,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
    learning_service=None,
    explicit_request: bool = False,
) -> ReviewOutcome:
    if check_cancel:
        check_cancel()
    outcome = ReviewOutcome(stage=stage)

    def emit(event: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(event)

    if not STAGE_SPEC[stage].gated:
        outcome.error = f"{stage} has no review gate in v1"
        return outcome

    state = pipeline.stage_state(run_id, stage)
    if state.latest is None:
        outcome.error = f"{stage} has nothing to review yet"
        return outcome
    if state.latest.digest_algorithm != "artifact-v2":
        outcome.error = "历史版本只有正文指纹；请先产生包含展示字段与依据的新版本，再审核"
        return outcome
    if state.stale:
        outcome.error = f"{stage}-v{state.latest.version} 已过期：{state.stale_reason}"
        return outcome
    if not state.integrity_ok:
        outcome.error = f"{stage}-v{state.latest.version} 指纹校验失败，文件被改动过"
        return outcome
    if state.review is not None and state.review.artifact_sha256 == state.latest.sha256:
        outcome.verdict = state.review.verdict
        outcome.findings = state.review.findings
        outcome.text = f"{stage}-v{state.latest.version} 使用已保存的审核结论，没有重复调用模型。"
        return outcome

    selection = creative_selection(store, run_id)
    if not explicit_request and not production_allowed(selection):
        raise ToolError("当前路由只允许读取/反馈；不能启动新审核调用。")
    if learning_service is None:
        from ..learning import LearningService
        learning_service = LearningService(store)
    # Fresh purpose-specific retrieval; never reuse the producer's snapshot.
    context = learning_service.prepare_context(run_id, "reviewer", **selection, purpose="review")
    emit({"type": "learning_context", "role": "reviewer", "stage": stage,
          "snapshot_sha256": context["snapshot_sha256"], "evidence_ref": context["evidence_ref"]})

    workspace = Workspace(store, pipeline, run_id, "reviewer", target_stage=stage)
    versions = {name: store.latest_artifact(run_id, name).version
                for name in (stage, *pipeline.upstream_stages(stage))
                if store.latest_artifact(run_id, name) is not None}
    versions[stage] = state.latest.version
    run = store.read_run(run_id)
    budgeted = BudgetedClient(
        client, ledger, run_id=run_id, role="reviewer", stage=stage
    )

    recorded: dict[str, Any] = {}

    def execute(name: str, args: dict[str, Any]) -> str | ToolResult:
        if name == "read_capabilities":
            from ..tools.context import capabilities
            return json.dumps(capabilities(settings, "reviewer"), ensure_ascii=False)
        if name == "read_method_document":
            return json.dumps(learning_service.read_method_document(args["name"], role="reviewer", run_id=run_id), ensure_ascii=False)
        if name == "read_artifact":
            requested_stage = args["stage"]
            if args.get("version") not in (None, versions.get(requested_stage)):
                raise ToolError("review is bound to the initial artifact versions")
            return workspace.render_artifact(requested_stage, versions.get(requested_stage))
        if name == "list_materials":
            return workspace.render_materials()
        if name == "finish_review":
            verdict = args.get("verdict")
            findings = args.get("findings") or []
            review = pipeline.record_review(
                run_id, stage, verdict=verdict, findings=findings,
                expected_artifact_sha256=state.latest.sha256,
                expected_version=state.latest.version,
            )
            recorded["verdict"] = review.verdict
            recorded["findings"] = review.findings
            recorded["sha"] = review.artifact_sha256
            return ToolResult(
                f"已记录：{stage} 判定为 {verdict}。"
                + ("你可以结束了。" if verdict == "pass" else "你可以结束了；退回的是产物，不是你。"),
                concludes_turn=True,
            )
        raise ToolError(f"reviewer has no tool named {name!r}")

    seed = _seed(workspace, stage, run, versions=versions)
    current = pipeline.stage_state(run_id, stage)
    if (current.latest is None or current.latest.sha256 != state.latest.sha256
            or current.latest.version != state.latest.version or current.stale or not current.integrity_ok):
        outcome.error = "审核输入已经变化，请读取当前版本后重新审核"
        return outcome

    try:
        result = run_agent_loop(
            budgeted,
            model=settings.model_for("reviewer"),
            system=REVIEWER.system(
                run,
                extra=(
                    "# 你正在审的东西\n\n"
                    f"阶段：{stage}（第 {state.latest.version} 版）\n"
                    f"你可以读取的产物：{'、'.join(workspace.readable)}\n\n"
                    "你看不到创作者是谁、怎么想的、怎么自我评价的。这是故意的。\n\n" + context["text"]
                    + "\n登记方法目录（只读，不包含原始研究和旧审核）：\n"
                    + json.dumps(learning_service.skill_catalog('reviewer'), ensure_ascii=False)
                ),
                format=selection["format"],
            ),
            messages=[{"role": "user", "content": seed}],
            tools=tools_for("reviewer"),
            execute_tool=execute,
            max_rounds=settings.max_tool_rounds,
            max_continuations=settings.max_continuations,
            check_cancel=check_cancel,
            on_activity=lambda event: emit({"role": "reviewer", "stage": stage, **event}),
            on_turn=lambda t: emit(
                {"type": "agent_turn", "role": "reviewer", "stage": stage,
                 "stop_reason": t.stop_reason, "tools": [c.name for c in t.tool_calls]}
            ),
            on_tool=lambda r: emit(
                {"type": "agent_tool", "role": "reviewer", "stage": stage, **r}
            ),
        )
    except (BudgetExceeded, JobCancelled, SafePreflightError):
        # Same reasoning as `Dispatcher.run_role`: a review has no attempt to
        # close, so there is nothing to clean up — but turning this into
        # `outcome.error` would report an exhausted budget as a failed review
        # and invite a retry. See `run_agent_loop`.
        raise
    except PipelineError as exc:
        outcome.error = str(exc)
        return outcome
    except Exception as exc:
        outcome.error = f"{type(exc).__name__}: {exc}"
        return outcome

    outcome.text = result.text
    outcome.rounds = result.rounds
    outcome.stopped_because = result.stopped_because
    outcome.verdict = recorded.get("verdict")
    outcome.findings = recorded.get("findings", [])
    return outcome


def _seed(workspace: Workspace, stage: str, run, *, versions: dict[str, int] | None = None) -> str:
    versions = versions or {}
    parts = [
        "# 判定对象",
        "",
        f"阶段 {stage} 的当前版本如下。你要审的就是这一版。",
        "",
        workspace.render_artifact(stage, versions.get(stage)),
        "",
    ]
    # render_context includes the full pinned intake plus the actual selection;
    # don't pay to send that same fact document twice in one request.
    upstream = [s for s in workspace.pipeline.upstream_stages(stage)
                if s in workspace.readable and s != "intake"]
    for up in upstream:
        parts += [
            f"# 上游产物 {up}（用于核对依据，不是判定对象）",
            "",
            workspace.render_artifact(up, versions.get(up)),
            "",
        ]
    parts += [workspace.render_context(stage, versions=versions), ""]
    parts += [
        "# 怎么判",
        "",
        "按你的标准逐条检查，然后调用 finish_review 给出判定。",
        "通过要说明通过的理由；退回要给出具体问题，每条指名责任阶段。",
        "不要因为「看起来完整」就通过。字段齐全不构成通过的理由。",
    ]
    return "\n".join(parts)
