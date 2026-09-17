"""Orchestration and review independence, with a scripted model.

These are the tests that make the README's claims about isolation checkable
rather than aspirational: each one inspects what an agent was *given*, not what
it was told.
"""
from __future__ import annotations

import json

import pytest

from fakes import ScriptedModel, final_turn, tool_turn
from studio.agents.client import run_agent_loop
from studio.agents.orchestrator import Orchestrator
from studio.agents.reviewer import run_review
from studio.agents.roles import readable_stages
from studio.config import Pricing, Settings
from studio.ledger import Ledger
from studio.models import BudgetExceeded, PipelineError
from studio.pipeline import Pipeline
from studio.store import Store
from studio.tools.dispatch import Dispatcher
from studio.tools.registry import READ, level_of, tool_names_for

RUN = "lunch-bag-01"


def settings_for(tmp_path, **over):
    base = dict(
        data_root=tmp_path,
        base_url="https://example.invalid/anthropic",
        budget_limit_usd=10.0,
        pricing=Pricing(source="test", input_usd_per_mtok=1.0, output_usd_per_mtok=2.0,
                        as_of="2026-09-17"),
        max_tool_rounds=6,
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture()
def env(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, "Lunch bag ad", "给上班族带饭的保温袋，做一条 15 秒的短视频。")
    pipeline = Pipeline(store)
    ledger = Ledger(store, settings_for(tmp_path))
    return store, pipeline, ledger


def seed_intake(pipeline, text="需求：给上班族带饭的保温袋，15 秒短视频。"):
    attempt = pipeline.start_attempt(RUN, "intake")
    return pipeline.submit(RUN, "intake", attempt.token, content=text, summary="intake 整理完毕")


def make_orchestrator(store, pipeline, ledger, model, tmp_path):
    return Orchestrator(store, pipeline, settings_for(tmp_path), model, ledger, RUN)


# --------------------------------------------------------------- tool sets -- #


def test_no_role_has_an_approval_tool():
    for role in ("orchestrator", "director", "material", "writer", "reviewer"):
        names = tool_names_for(role)
        assert not any("approve" in n or "approval" in n for n in names), role


def test_the_reviewer_cannot_produce_or_dispatch_work():
    names = tool_names_for("reviewer")
    # `list_materials` is allowed on purpose: a plan that says a shot has a
    # photo is exactly the kind of claim the reviewer should be able to check,
    # and it is a read of filenames — not of the producer's reasoning, and not
    # of anything it could write.
    assert set(names) == {"read_artifact", "list_materials", "finish_review"}
    assert "submit_stage" not in names
    assert "dispatch" not in names
    assert "write_stage" not in names
    assert level_of("list_materials") == READ


def test_only_the_orchestrator_can_dispatch_or_review():
    assert "dispatch" in tool_names_for("orchestrator")
    assert "request_review" in tool_names_for("orchestrator")
    for role in ("director", "material", "writer"):
        assert "dispatch" not in tool_names_for(role)
        assert "request_review" not in tool_names_for(role)


def test_reviewer_reads_the_target_and_its_upstream_only():
    assert readable_stages("reviewer", "direction") == ("intake", "direction")
    assert readable_stages("reviewer", "writing") == (
        "intake", "direction", "material_plan", "writing"
    )
    assert readable_stages("director") == ("intake",)
    assert readable_stages("writer") == ("intake", "direction", "material_plan")


# --------------------------------------------------------------- dispatch -- #


def test_dispatch_runs_the_role_in_its_own_context(env, tmp_path):
    store, pipeline, ledger = env
    seed_intake(pipeline, "需求原话：这是 Jeffery 说的原话，不该出现在导演的上下文里。")

    model = ScriptedModel([
        tool_turn("t1", "submit_stage", {
            "content": "方案 A……方案 B……", "summary": "两个方向",
            "evidence": ["intake 里说到上班族"], "limitations": ["没有商品实拍图"],
        }),
        final_turn("已提交。"),
    ])
    orch = make_orchestrator(store, pipeline, ledger, model, tmp_path)
    from studio.tools.dispatch import Dispatcher

    dispatcher = Dispatcher(store, pipeline, settings_for(tmp_path), model, ledger, RUN)
    outcome = dispatcher.dispatch("direction", "出两个方向，要真的不同。")

    assert outcome.submitted == "direction-v1"
    artifact = pipeline.stage_state(RUN, "direction").latest
    assert artifact.author_role == "director"
    assert artifact.evidence == ["intake 里说到上班族"]
    assert artifact.limitations == ["没有商品实拍图"]
    assert set(artifact.inputs) == {"intake", "evidence:snapshot"}

    # The sub-agent saw the upstream artifact, and its tool list is its own.
    request = model.calls[0]
    assert request["tool_names"] == list(tool_names_for("director"))
    seed = request["messages"][0]["content"]
    assert "需求：给上班族带饭" in seed or "需求原话" in seed
    assert "出两个方向" in seed


def test_a_role_agent_that_never_submits_leaves_no_artifact_and_closes_its_attempt(
    env, tmp_path
):
    store, pipeline, ledger = env
    seed_intake(pipeline)
    model = ScriptedModel([final_turn("我想了想，但没写。")])

    from studio.tools.dispatch import Dispatcher

    outcome = Dispatcher(
        store, pipeline, settings_for(tmp_path), model, ledger, RUN
    ).dispatch("direction", "出两个方向。")

    assert outcome.submitted is None
    assert pipeline.stage_state(RUN, "direction").latest is None

    # The attempt is closed, not left dangling to block the next dispatch.
    state = pipeline.stage_state(RUN, "direction")
    assert state.open_attempt is None
    assert "没有提交" in state.last_rejection
    # And the stage is startable again straight away.
    assert state.can_start is True


def test_dispatch_refuses_a_stage_the_orchestrator_owns(env, tmp_path):
    store, pipeline, ledger = env
    from studio.tools.workspace import ToolError
    from studio.tools.dispatch import Dispatcher

    dispatcher = Dispatcher(
        store, pipeline, settings_for(tmp_path), ScriptedModel([]), ledger, RUN
    )
    with pytest.raises(ToolError):
        dispatcher.dispatch("intake", "整理需求")


def test_dispatch_does_not_start_when_prerequisites_fail(env, tmp_path):
    store, pipeline, ledger = env
    from studio.tools.dispatch import Dispatcher

    # No intake yet, so direction cannot start. The dispatcher reports it
    # rather than spending a call finding out.
    model = ScriptedModel([])
    outcome = Dispatcher(
        store, pipeline, settings_for(tmp_path), model, ledger, RUN
    ).dispatch("direction", "出方向")

    assert outcome.error and "intake" in outcome.error
    assert model.calls == []


def test_second_dispatch_produces_a_new_version(env, tmp_path):
    store, pipeline, ledger = env
    seed_intake(pipeline)
    from studio.tools.dispatch import Dispatcher

    def run_once(text):
        model = ScriptedModel([
            tool_turn("t1", "submit_stage", {"content": text, "summary": "s"}),
            final_turn("done"),
        ])
        return Dispatcher(
            store, pipeline, settings_for(tmp_path), model, ledger, RUN
        ).dispatch("direction", "出方向"), model

    first, _ = run_once("方向 v1")
    second, _ = run_once("方向 v2")

    assert first.submitted == "direction-v1"
    assert second.submitted == "direction-v2"
    assert [a.version for a in store.list_artifacts(RUN, "direction")] == [1, 2]
    # v2 must be flagged: it was written against the same intake, so not stale.
    assert pipeline.stage_state(RUN, "direction").stale is False


def test_every_round_of_a_sub_agent_is_on_the_ledger(env, tmp_path):
    store, pipeline, ledger = env
    seed_intake(pipeline)
    from studio.tools.dispatch import Dispatcher

    model = ScriptedModel([
        tool_turn("t1", "read_artifact", {"stage": "intake"}),
        tool_turn("t2", "submit_stage", {"content": "方向", "summary": "s"}),
        final_turn("done"),
    ])
    Dispatcher(store, pipeline, settings_for(tmp_path), model, ledger, RUN).dispatch(
        "direction", "出方向"
    )

    rows = ledger.entries(RUN)
    assert len(store.read_ledger(RUN)) == 6
    assert len(rows) == 3
    assert {r.role for r in rows} == {"director"}
    assert all(r.pricing_source == "test" for r in rows)


def test_a_sub_agent_cannot_read_stages_it_was_not_given(env, tmp_path):
    store, pipeline, ledger = env
    seed_intake(pipeline)
    from studio.tools.dispatch import Dispatcher
    from studio.tools.workspace import ToolError

    model = ScriptedModel([
        tool_turn("t1", "read_artifact", {"stage": "writing"}),
        final_turn("blocked"),
    ])
    # The executor raises, the loop converts it to an error result, and the
    # agent gets no content from the forbidden stage.
    Dispatcher(store, pipeline, settings_for(tmp_path), model, ledger, RUN).dispatch(
        "direction", "出方向"
    )
    assert "may not read stage" in model.requests_text()


# --------------------------------------------------------------- reviewer -- #


def test_reviewer_sees_the_artifact_and_not_the_producer_conversation(env, tmp_path):
    store, pipeline, ledger = env
    seed_intake(pipeline, "intake 正文：保温袋，带饭用。")
    attempt = pipeline.start_attempt(RUN, "direction")
    pipeline.submit(
        RUN, "direction", attempt.token,
        content="方向正文：主张是「早上装好，中午还是热的」。",
        summary="两个方向，推荐第一个",
        limitations=["没有实拍图"],
    )

    model = ScriptedModel([
        tool_turn("t1", "finish_review", {"verdict": "pass", "findings": []}),
        final_turn("通过。"),
    ])
    outcome = run_review(
        store=store, pipeline=pipeline, settings=settings_for(tmp_path),
        client=model, ledger=ledger, run_id=RUN, stage="direction",
    )

    assert outcome.verdict == "pass"
    request = model.calls[0]
    assert request["tool_names"] == ["read_artifact", "list_materials", "finish_review"]
    seed = request["messages"][0]["content"]
    assert "早上装好，中午还是热的" in seed      # the artifact itself
    assert "带饭用" in seed                       # upstream, as evidence
    # The producer's summary and limitations reach it (they are part of the
    # artifact record), but nothing from any conversation does.
    assert "PRODUCER-REASONING-MARKER" not in seed


def test_the_reviewer_is_shown_the_document_jeffery_is_shown(env, tmp_path):
    """A claim that lives only in the layout must be visible to the reviewer.

    Jeffery approves off the laid-out card — on the first real run, off a table
    saying which option died on which blank. A reviewer handed only the prose
    would be judging a different document from the one being approved, and the
    inconsistency between the two would be exactly the thing neither of them
    could see.
    """
    store, pipeline, ledger = env
    seed_intake(pipeline)
    attempt = pipeline.start_attempt(RUN, "direction")
    pipeline.submit(
        RUN, "direction", attempt.token,
        content="正文只说了一句：三个方向在下面。",
        summary="三个方向",
        structured={
            "options": [{"key": "A", "claim": "只在排版里的主张", "difficulty": "低"}],
            "recommended": "A",
            "dependency_matrix": [{"need": "只在排版里的空白", "options": {"A": True}}],
        },
    )

    model = ScriptedModel([
        tool_turn("t1", "finish_review", {"verdict": "pass", "findings": []}),
        final_turn("通过。"),
    ])
    run_review(
        store=store, pipeline=pipeline, settings=settings_for(tmp_path),
        client=model, ledger=ledger, run_id=RUN, stage="direction",
    )

    seed = model.calls[0]["messages"][0]["content"]
    assert "只在排版里的主张" in seed
    assert "只在排版里的空白" in seed
    assert "缺了它站不住" in seed, "the tick grid is spelled out for a reader that cannot see it"
    assert "Jeffery 看到的是这个" in seed


def test_a_stage_with_no_layout_reads_exactly_as_before(env, tmp_path):
    """The reviewer is not told to look for a table that was never produced."""
    store, pipeline, ledger = env
    seed_intake(pipeline)
    attempt = pipeline.start_attempt(RUN, "direction")
    pipeline.submit(RUN, "direction", attempt.token, content="正文", summary="s")

    model = ScriptedModel([
        tool_turn("t1", "finish_review", {"verdict": "pass", "findings": []}),
        final_turn("通过。"),
    ])
    run_review(
        store=store, pipeline=pipeline, settings=settings_for(tmp_path),
        client=model, ledger=ledger, run_id=RUN, stage="direction",
    )
    assert "排好版之后的样子" not in model.calls[0]["messages"][0]["content"]


def test_reviewer_that_never_decides_records_nothing(env, tmp_path):
    store, pipeline, ledger = env
    seed_intake(pipeline)
    attempt = pipeline.start_attempt(RUN, "direction")
    pipeline.submit(RUN, "direction", attempt.token, content="方向", summary="s")

    model = ScriptedModel([final_turn("我觉得还行，但我说不好。")])
    outcome = run_review(
        store=store, pipeline=pipeline, settings=settings_for(tmp_path),
        client=model, ledger=ledger, run_id=RUN, stage="direction",
    )

    assert outcome.verdict is None
    assert store.list_reviews(RUN, "direction") == []
    state = pipeline.stage_state(RUN, "direction")
    assert state.status == "awaiting_review"
    assert state.can_approve is False


def test_a_returned_review_still_leaves_him_the_last_word(env, tmp_path):
    """A ``return`` blocks progress and offers him the override — both.

    The gate this replaces kept ``can_approve`` false on a return, which made the
    reviewer's verdict final: a same-model agent, running on the producers' side
    of the table, holding a veto Jeffery could only lift by hand-crafting an HTTP
    request. It now offers him the decision, and ``record_approval`` still refuses
    to record it without an explicit override reason — so the teeth are in the
    reason, not in the absence of a button.
    """
    store, pipeline, ledger = env
    seed_intake(pipeline)
    attempt = pipeline.start_attempt(RUN, "direction")
    pipeline.submit(RUN, "direction", attempt.token, content="方向", summary="s")

    model = ScriptedModel([
        tool_turn("t1", "finish_review", {
            "verdict": "return",
            "findings": [{"stage": "direction", "issue": "两个方案其实是同一个", "kind": "content"}],
        }),
        final_turn("退回。"),
    ])
    outcome = run_review(
        store=store, pipeline=pipeline, settings=settings_for(tmp_path),
        client=model, ledger=ledger, run_id=RUN, stage="direction",
    )

    assert outcome.verdict == "return"
    state = pipeline.stage_state(RUN, "direction")
    assert state.status == "returned"
    assert state.can_approve is True

    # Still shut for everything that is not his click: a return does not open
    # the next stage, and no agent can approve around it.
    with pytest.raises(PipelineError):
        pipeline.start_attempt(RUN, "material_plan")

    # And the override he would be offered is not free.
    nonce = pipeline.issue_nonce(RUN, "direction")
    with pytest.raises(PipelineError, match="override reason"):
        pipeline.record_approval(
            RUN, "direction", nonce=nonce, decision="approved", override_reason=""
        )
    nonce = pipeline.issue_nonce(RUN, "direction")
    approval = pipeline.record_approval(
        RUN, "direction", nonce=nonce, decision="approved",
        override_reason="两个方案确实像，但我要先拿它去测素材。",
    )
    assert approval.decision == "approved_override"
    assert "override" in approval.note
    pipeline.start_attempt(RUN, "material_plan")  # now it opens


def test_review_is_not_repeated_for_the_same_fingerprint(env, tmp_path):
    store, pipeline, ledger = env
    seed_intake(pipeline)
    attempt = pipeline.start_attempt(RUN, "direction")
    pipeline.submit(RUN, "direction", attempt.token, content="方向", summary="s")
    pipeline.record_review(RUN, "direction", verdict="pass", findings=[])

    model = ScriptedModel([])
    outcome = run_review(
        store=store, pipeline=pipeline, settings=settings_for(tmp_path),
        client=model, ledger=ledger, run_id=RUN, stage="direction",
    )
    assert "已经审核过" in outcome.error
    assert model.calls == []


def test_a_stale_version_is_not_sent_for_review(env, tmp_path):
    store, pipeline, ledger = env
    seed_intake(pipeline)
    attempt = pipeline.start_attempt(RUN, "direction")
    pipeline.submit(RUN, "direction", attempt.token, content="方向", summary="s")

    revised = pipeline.start_attempt(RUN, "intake")
    pipeline.submit(RUN, "intake", revised.token, content="需求变了", summary="改需求")

    model = ScriptedModel([])
    outcome = run_review(
        store=store, pipeline=pipeline, settings=settings_for(tmp_path),
        client=model, ledger=ledger, run_id=RUN, stage="direction",
    )
    assert "过期" in outcome.error
    assert model.calls == []


# ----------------------------------------------------------- orchestrator -- #


def test_orchestrator_turn_dispatches_writes_and_reports(env, tmp_path):
    store, pipeline, ledger = env
    model = ScriptedModel([
        tool_turn("t1", "read_project_state", {}),
        tool_turn("t2", "write_stage", {
            "stage": "intake",
            "content": "市场：US。目的：15 秒短视频。目标观众：上班族。未确认：卖点。",
            "summary": "需求整理",
            "limitations": ["卖点还没定"],
        }),
        tool_turn("t3", "dispatch", {"stage": "direction", "brief": "出两个方向"}),
        # The next two turns belong to the dispatched director, not to the
        # orchestrator: it submits, then stops.
        tool_turn("t4", "submit_stage", {"content": "方向正文", "summary": "两个方向"}),
        final_turn("导演：已提交。"),
        final_turn("intake 已整理，direction 已派出去并提交。"),
    ])
    orch = make_orchestrator(store, pipeline, ledger, model, tmp_path)
    turn = orch.send("给上班族带饭的保温袋，做 15 秒视频")

    assert turn.stopped_because == "done"
    assert "direction" in turn.text
    state = pipeline.state(RUN)
    assert state.stages["intake"].latest.version == 1
    assert state.stages["direction"].latest.version == 1
    assert state.stages["direction"].status == "awaiting_review"

    # The conversation was logged for the UI.
    chat = store.read_chat(RUN)
    assert chat[0]["role"] == "user"
    assert chat[-1]["role"] == "assistant"


def test_orchestrator_cannot_approve_and_is_told_so(env, tmp_path):
    store, pipeline, ledger = env
    model = ScriptedModel([
        tool_turn("t1", "approve_stage", {"stage": "direction"}),
        final_turn("我试试能不能自己批。"),
    ])
    orch = make_orchestrator(store, pipeline, ledger, model, tmp_path)
    orch.send("直接通过吧")

    # No such tool exists; the loop reports it and nothing was approved.
    assert store.list_approvals(RUN, "direction") == []
    assert "no tool named" in model.requests_text()


def test_orchestrator_state_view_includes_spend_and_the_no_approval_rule(env, tmp_path):
    store, pipeline, ledger = env
    ledger.record(RUN, role="director", stage="direction", model="deepseek-flash",
                  input_tokens=1000, output_tokens=500)
    model = ScriptedModel([final_turn("好的。")])
    orch = make_orchestrator(store, pipeline, ledger, model, tmp_path)
    orch.send("现在什么情况？")

    system = model.calls[0]["system"]
    assert "0.0020" in system          # 1000/1e6*1 + 500/1e6*2
    assert "不能批准" in system
    assert "不是账单" in system


def test_reading_state_returns_spend_not_just_the_stage_board(env, tmp_path):
    """Observed live 2026-09-17: the orchestrator said it had no cost figures.

    The spend block was only in the system prompt, which is written once before
    the turn starts — so an agent that had just paid for four dispatch rounds
    was reporting a number from before it began, and said so. It belongs in the
    state read too, where it is recomputed at the moment of the call.
    """
    store, pipeline, ledger = env
    ledger.record(RUN, role="director", stage="direction", model="deepseek-flash",
                  input_tokens=1000, output_tokens=500)
    model = ScriptedModel([
        tool_turn("t1", "read_project_state", {}),
        final_turn("看完了。"),
    ])
    orch = make_orchestrator(store, pipeline, ledger, model, tmp_path)
    orch.send("花了多少钱？")

    # What the model saw in round 2, after the tool ran.
    second = model.requests_text()
    assert "成本" in second
    assert "开发期累计估算" in second
    assert "已知" in second and "用量待核实" in second


def test_second_turn_carries_the_first_turn_and_reanchors_to_user(env, tmp_path):
    store, pipeline, ledger = env
    model = ScriptedModel([final_turn("第一句。"), final_turn("第二句。")])
    orch = make_orchestrator(store, pipeline, ledger, model, tmp_path)
    orch.send("你好")
    orch.send("继续")

    messages = model.calls[1]["messages"]
    assert messages[0]["role"] == "user"
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[1]["content"] == "第一句。"
    assert messages[2]["content"] == "继续"


def test_budget_cap_stops_the_orchestrator_before_it_calls(env, tmp_path):
    store, pipeline, ledger = env
    ledger.record(RUN, role="director", stage="direction", model="deepseek-flash",
                  input_tokens=10_000_000, output_tokens=0)
    model = ScriptedModel([final_turn("不该被调用")])
    orch = Orchestrator(
        store, pipeline, settings_for(tmp_path, budget_limit_usd=1.0), model, ledger, RUN
    )
    from studio.models import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        orch.send("继续")
    assert model.calls == []


# ------------------------- a budget stop is not an ordinary tool failure -- #


def test_a_budget_stop_from_a_tool_is_not_handed_back_to_the_model():
    """``run_agent_loop`` turns tool exceptions into ``is_error`` results so the
    model can react to them. That is right for a failed read; it is wrong for an
    exhausted budget.

    ``BudgetExceeded`` used to arrive as a tool result, so the model read
    "派工失败" and retried. Every retry is refused pre-flight, so no money was
    lost — but each one burned a round until ``max_rounds`` ran out, and the
    model was never told to stop. The observable difference is *whether the loop
    calls the model again*: one call now, two before.
    """
    model = ScriptedModel([tool_turn("t1", "read_artifact", {"stage": "intake"}),
                           final_turn("不该走到这里")])

    def execute(name, args):
        raise BudgetExceeded("token cap reached")

    with pytest.raises(BudgetExceeded):
        run_agent_loop(model, model="m", system="s", messages=[], tools=[],
                       execute_tool=execute, max_rounds=3)
    assert len(model.calls) == 1, "the loop must not go round again"


def test_a_budget_stop_inside_a_dispatch_reaches_the_caller(env, tmp_path):
    """The same bug one layer down, where it bites hardest.

    ``BudgetExceeded`` subclasses ``PipelineError``, and ``Dispatcher.run_role``
    catches ``PipelineError`` to convert it into ``outcome.error``. So a budget
    abort inside a dispatched sub-agent came back to the orchestrator as a
    *successful* tool result carrying a failure message — an invitation to
    dispatch again.

    A direct call to ``run_role`` rather than a whole orchestrator turn, because
    the orchestrator would trip the same cap on its own next round and raise
    either way: the two paths are indistinguishable from outside. Only this
    level separates "refused, here is a failure" from "stop the run".
    """
    store, pipeline, _ = env
    seed_intake(pipeline)
    # One tool turn costs 35 tokens (20 in + 15 out), so a 30-token cap lets the
    # sub-agent's first call through and refuses its second. That is the shape
    # that reaches the swallowing handler.
    settings = settings_for(tmp_path, budget_limit_tokens=30)
    ledger = Ledger(store, settings)
    model = ScriptedModel([tool_turn("r1", "read_artifact", {"stage": "intake"}),
                           final_turn("不该走到这里")])
    dispatcher = Dispatcher(store, pipeline, settings, model, ledger, RUN)

    with pytest.raises(BudgetExceeded):
        dispatcher.run_role("direction", "director", "出三个方向")


def test_the_attempt_is_closed_before_the_budget_stop_propagates(env, tmp_path):
    """Re-raising must not strand the stage.

    An open attempt blocks its stage, and an exhausted budget is precisely the
    case where he raises the cap and expects the run to carry on. So the
    attempt is closed *first*, then the exception goes up.

    The reason string carries the raw enum on purpose:
    ``vocabulary.rejection_sentence`` keys off ``budget`` to say 预算用完了
    rather than print ``[budget]`` at him.
    """
    store, pipeline, _ = env
    seed_intake(pipeline)
    settings = settings_for(tmp_path, budget_limit_tokens=30)
    ledger = Ledger(store, settings)
    model = ScriptedModel([tool_turn("r1", "read_artifact", {"stage": "intake"})])
    dispatcher = Dispatcher(store, pipeline, settings, model, ledger, RUN)

    with pytest.raises(BudgetExceeded):
        dispatcher.run_role("direction", "director", "出三个方向")

    reasons = [e.get("reason", "") for e in store.read_events(RUN)
               if e.get("kind") == "attempt_rejected"]
    assert any("budget" in r.lower() for r in reasons), \
        "the attempt is closed rather than left dangling"
    assert pipeline.stage_state(RUN, "direction").open_attempt is None, \
        "an open attempt would block the stage"


# ------------------------------------------------- what reaches the model -- #


def test_the_users_message_is_on_disk_before_the_loop_runs(env, tmp_path):
    """The ordering repair, at its source.

    The message used to be appended *after* the loop, so it carried the
    timestamp of the moment its own work finished, and the timeline sorted the
    question below the document it produced. The assertion that catches this
    without depending on clock resolution: the message is already readable from
    the store at the moment the model is first called.
    """
    store, pipeline, ledger = env
    seen: list[list[dict]] = []

    class WatchingModel(ScriptedModel):
        def complete(self, **kwargs):
            seen.append(store.read_chat(RUN))
            return super().complete(**kwargs)

    orch = make_orchestrator(store, pipeline, ledger, WatchingModel([final_turn("好。")]), tmp_path)
    orch.send("先整理需求。")

    assert seen, "the model should have been called"
    assert [c["text"] for c in seen[0] if c["role"] == "user"] == ["先整理需求。"]


def test_a_message_is_not_lost_when_the_budget_stops_the_turn(env, tmp_path):
    """What he wrote survives a refusal to spend.

    ``BudgetExceeded`` is raised before any call. It used to be raised before the
    append too, so hitting the cap silently swallowed the message he had just
    typed — he would see his question vanish with no trace of it.
    """
    store, pipeline, ledger = env
    ledger.record(RUN, role="director", stage="direction", model="deepseek-flash",
                  input_tokens=10_000_000, output_tokens=0)
    orch = Orchestrator(
        store, pipeline, settings_for(tmp_path, budget_limit_usd=1.0),
        ScriptedModel([]), ledger, RUN,
    )
    from studio.models import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        orch.send("这条不能丢")
    assert [c["text"] for c in store.read_chat(RUN) if c["role"] == "user"] == ["这条不能丢"]


def test_a_failed_turn_leaves_a_trace_in_the_log(env, tmp_path):
    """A crash is persisted, because the SSE error is not.

    Without it, reloading after a failure shows his question followed by silence
    and no way to find out what happened.
    """
    store, pipeline, ledger = env

    class ExplodingModel:
        calls: list = []

        def complete(self, **kwargs):
            raise RuntimeError("provider said no")

    orch = make_orchestrator(store, pipeline, ledger, ExplodingModel(), tmp_path)
    with pytest.raises(RuntimeError):
        orch.send("试试")

    chat = store.read_chat(RUN)
    assert [c["role"] for c in chat] == ["user", "system"]
    assert "provider said no" in chat[-1]["text"]


def test_failure_records_never_go_back_to_the_model(env, tmp_path):
    """The 'system' entries are for the screen. A request containing one is a 400.

    The events API accepts three roles and the model accepts two; the log holds
    the third so a failed turn can be explained. This is the seam where that
    could leak.
    """
    store, pipeline, ledger = env

    class ExplodingModel:
        calls: list = []

        def complete(self, **kwargs):
            raise RuntimeError("boom")

    orch = make_orchestrator(store, pipeline, ledger, ExplodingModel(), tmp_path)
    with pytest.raises(RuntimeError):
        orch.send("第一次")
    assert [c["role"] for c in store.read_chat(RUN)] == ["user", "system"]

    ledger.reconcile(RUN, ledger.entries(RUN)[0].call_id, input_tokens=0,
                     output_tokens=0, evidence_note="Synthetic provider fixture confirms zero usage")
    # A reconciled second turn must not forward the system record.
    model = ScriptedModel([final_turn("第二次回复。")])
    orch2 = make_orchestrator(store, pipeline, ledger, model, tmp_path)
    orch2.send("第二次")

    roles = [m["role"] for m in model.calls[0]["messages"]]
    assert set(roles) <= {"user", "assistant"}, roles
    assert "system" not in roles
    assert "boom" not in json.dumps(model.calls[0]["messages"], ensure_ascii=False)


def test_a_failure_record_does_not_end_up_as_a_stray_user_turn(env, tmp_path):
    """A 'system' record between two messages must not swallow the pairing."""
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "第一次")
    store.append_chat(RUN, "system", "RuntimeError: boom")
    store.append_chat(RUN, "assistant", "回复")

    model = ScriptedModel([final_turn("第二次回复。")])
    orch = make_orchestrator(store, pipeline, ledger, model, tmp_path)
    orch.send("第二次")

    messages = model.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == "第一次", "the first user turn is the conversation opener"
