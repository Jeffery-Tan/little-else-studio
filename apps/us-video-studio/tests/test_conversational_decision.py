"""The F5 acceptance scenarios, driven through the real orchestrator loop.

These are the chains from ``docs/conversational-control.md`` §10. They go through
the actual agent loop, the actual registry and the actual server-side validator,
because the thing being fixed is precisely that the conversation had no way to
make a decision — a test of ``resolve`` alone would not show that.
"""
from __future__ import annotations

import json

import pytest

from fakes import ScriptedModel, final_turn, tool_turn
from studio.agents.orchestrator import Orchestrator
from studio.config import Pricing, Settings
from studio.decision import candidates, resolve
from studio.ledger import Ledger
from studio.pipeline import Pipeline
from studio.store import Store
from studio.tools.registry import tool_names_for

RUN = "f5-scenarios"


def settings_for(tmp_path, **over):
    base = dict(data_root=tmp_path, base_url="https://example.invalid/anthropic",
                budget_limit_usd=10.0,
                pricing=Pricing(source="test", input_usd_per_mtok=1.0,
                                output_usd_per_mtok=2.0, as_of="2026-09-17"),
                max_tool_rounds=8)
    base.update(over)
    return Settings(**base)


def seed_direction(tmp_path):
    """A run waiting on the direction decision, with three admissible options."""
    store = Store(tmp_path)
    store.create_run(RUN, "Lunch bag ad", "给上班族带饭的保温袋，做一条 15 秒短视频。")
    pipeline = Pipeline(store)
    for stage in ("intake", "direction"):
        attempt = pipeline.start_attempt(RUN, stage)
        structured = ({"options": [{"key": key, "claim": f"claim {key}",
                                    "satisfies": "goal.primary"} for key in "ABC"]}
                      if stage == "direction" else {})
        pipeline.submit(RUN, stage, attempt.token, content=f"Synthetic {stage}",
                        summary=stage, structured=structured)
    return store, pipeline


def orchestrator(store, pipeline, tmp_path, turns):
    model = ScriptedModel(turns)
    ledger = Ledger(store, settings_for(tmp_path))
    orch = Orchestrator(store, pipeline, settings_for(tmp_path), model, ledger, RUN)
    original_send = orch.send
    def trusted_send(text, **kwargs):
        from test_decision import received_message
        receipt = received_message(store, pipeline, RUN, text)
        return original_send(text, user_input_id=receipt['message_id'], **kwargs)
    orch.send = trusted_send
    return orch, model


def proposal_for(store, pipeline, action, **fields):
    """The tool call an agent would make, built from what the run really offers."""
    return tool_turn("pick", "propose_user_action",
                     {"action": action, **fields})


# ---------------------------------------------------------------- scenario A -- #

def test_scenario_a_adopting_a_direction_records_pending_followup(tmp_path):
    """Orchestrator alone records intent; HTTP integration verifies execution."""
    store, pipeline = seed_direction(tmp_path)
    question = candidates(store, pipeline, RUN)[0]
    orch, model = orchestrator(store, pipeline, tmp_path, [
        proposal_for(store, pipeline, "select_direction",
                     target={"stage": "direction", "version": question["version"],
                             "artifact_sha256": question["artifact_sha256"]},
                     option_id="B"),
        final_turn("已按你说的采用方向 B，团队接着往下做。"),
    ])
    result = orch.send("采用 B，继续出分镜。")

    approvals = store.list_approvals(RUN, "direction")
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval.decision == "approved"
    assert approval.selected_option_id == "B"
    assert approval.source == "conversation"
    assert approval.continue_after is True
    assert approval.artifact_version == question["version"]
    assert approval.artifact_sha256 == question["artifact_sha256"]

    # Exactly one model turn happened after the proposal: no media, no re-click.
    assert len(model.calls) == 2
    resolved = [e for e in store.read_events(RUN) if e["kind"] == "user_action_resolved"]
    assert len(resolved) == 1 and resolved[0]["action_id"] == "select_direction"

    from studio.decision import continuation_intent
    intent = continuation_intent(store, pipeline, RUN)
    assert intent is not None, "the adoption must open the follow-up work"


# ---------------------------------------------------------------- scenario B -- #

def test_scenario_b_a_constraint_rides_along_without_changing_what_was_adopted(tmp_path):
    """'B 吧，但第一镜太广告了，换自然一点，然后继续。'

    The decision is the adoption. The added constraint is the user's own sentence
    and rides along as the instruction for the *next* round, so nothing about the
    adopted version changes.
    """
    from studio.decision import accepting_user_words, continuation_intent, continuation_text

    store, pipeline = seed_direction(tmp_path)
    question = candidates(store, pipeline, RUN)[0]
    adopted_bytes = store.read_artifact_text(RUN, "direction", question["version"])
    message = "B 吧，但开头太像广告了，换自然一点，然后继续。"
    orch, _ = orchestrator(store, pipeline, tmp_path, [
        tool_turn("pick", "propose_user_action", {
            "action": "select_direction",
            "target": {"stage": "direction", "version": question["version"],
                       "artifact_sha256": question["artifact_sha256"]},
            "option_id": "B",
            "continuation_note": "开头太像广告了，换自然一点",
        }),
        final_turn("已采用 B；你说的开头更自然这条，我让团队在下一版里落实。"),
    ])
    orch.send(message)

    approval = store.list_approvals(RUN, "direction")[0]
    assert approval.selected_option_id == "B"
    assert approval.note == ""
    assert approval.continuation_note == "开头太像广告了，换自然一点"
    # What was adopted is byte-identical to what the user read.
    assert store.read_artifact_text(RUN, "direction", question["version"]) == adopted_bytes
    # The instruction reaches the follow-up work.
    assert "自然" in continuation_text(approval)
    assert continuation_intent(store, pipeline, RUN) is not None
    assert accepting_user_words("开头太像广告了，换自然一点", message)


def test_a_model_rewritten_instruction_is_not_attributed_to_the_user(tmp_path):
    """An instruction the user never said must not be recorded as theirs.

    The decision itself is verified against the run's state; this guards the step
    after it, where the model could otherwise author a requirement on the user's
    behalf.
    """
    from studio.decision import accepting_user_words

    message = "B 吧，但开头太像广告了，换自然一点，然后继续。"
    assert accepting_user_words("开头太像广告了，换自然一点", message)
    assert accepting_user_words("整片改成快节奏、加三个卖点、结尾加促销", message) == ""
    assert accepting_user_words("", message) == ""


def test_scenario_b2_the_agent_may_carry_the_constraint_through_the_tool(tmp_path):
    """The instruction is quoted from the message, not paraphrased by the model."""
    store, pipeline = seed_direction(tmp_path)
    question = candidates(store, pipeline, RUN)[0]
    orch, _ = orchestrator(store, pipeline, tmp_path, [
        tool_turn("pick", "propose_user_action", {
            "action": "select_direction",
            "target": {"stage": "direction", "version": question["version"],
                       "artifact_sha256": question["artifact_sha256"]},
            "option_id": "B",
            "continuation_note": "开头生活化一点",
        }),
        final_turn("好。"),
    ])
    orch.send("B 吧，开头生活化一点。")
    approval = store.list_approvals(RUN, "direction")[0]
    assert approval.note == ""
    assert approval.continuation_note == "开头生活化一点"


# ---------------------------------------------------------------- scenario C -- #

def test_scenario_c_a_bare_yes_answers_the_only_open_question(tmp_path):
    """'行。' against the one question the run is asking."""
    store, pipeline = seed_direction(tmp_path)
    store.append_chat(RUN, 'assistant', '是否采用方向 B？')
    question = candidates(store, pipeline, RUN)[0]
    orch, _ = orchestrator(store, pipeline, tmp_path, [
        # The agent answers with the adoption answer for the only open question.
        tool_turn("pick", "propose_user_action", {
            "action": "select_direction",
            "target": {"stage": "direction", "version": question["version"],
                       "artifact_sha256": question["artifact_sha256"]},
            "option_id": "B",
        }),
        final_turn("按 B 继续。"),
    ])
    orch.send("行。")
    approvals = store.list_approvals(RUN, "direction")
    assert len(approvals) == 1 and approvals[0].selected_option_id == "B"


def test_scenario_c2_a_bare_yes_on_a_single_script_question_is_enough(tmp_path):
    """The plain-adoption answer resolves when exactly one question is open."""
    store, pipeline = seed_direction(tmp_path)
    question = candidates(store, pipeline, RUN)[0]
    verdict = resolve([question], {"action": "select_direction",
                                   "target": dict(question["target"]), "option_id": "A"})
    assert verdict["status"] == "commit"


# ---------------------------------------------------------------- scenario D -- #

def test_scenario_d_a_bare_yes_with_two_open_questions_asks_instead(tmp_path):
    """Two questions open and no way to tell which one '行' answers."""
    from tests.test_decision import direction_question, script_question

    open_questions = [direction_question(), script_question()]
    verdict = resolve(open_questions, {"action": "approve_stage",
                                       "target": {"stage": "writing"}})
    assert verdict["status"] == "clarify"
    assert verdict["reason"] == "several_decisions_open"
    assert "创意方向" in verdict["question"] and "脚本" in verdict["question"]
    # Nothing was committed, so nothing was recorded.
    assert verdict.get("action") is None


def test_scenario_d2_a_bare_yes_with_nothing_open_asks_what_is_wanted(tmp_path):
    store, pipeline = seed_direction(tmp_path)
    orch, _ = orchestrator(store, pipeline, tmp_path, [
        tool_turn("pick", "propose_user_action", {"action": "approve_stage"}),
        final_turn("现在没有等你决定的事了，你想让我做什么？"),
    ])
    orch.send("行。")
    assert store.list_approvals(RUN, "direction") == []


# ---------------------------------------------------------------- scenario G -- #

def storyboard_run(tmp_path, shots=3):
    """A run with an adopted script and one prepared image input per shot."""
    from studio.production import DraftIn, prepare_draft, target_shots

    store, pipeline = seed_direction(tmp_path)
    pipeline.record_approval(RUN, "direction",
                             nonce=pipeline.issue_nonce(RUN, "direction"),
                             decision="approved", selected_option_id="A")
    from visual_fixtures import stage_structure
    picture = {"tc": "0-2s", "picture": "one", "line": "a", "sound": "s", "duration": 2,
               "character_ids": "", "scene_id": "table"}
    for stage in ("material_plan", "writing"):
        attempt = pipeline.start_attempt(RUN, stage)
        pipeline.submit(RUN, stage, attempt.token, content=f"doc {stage}", summary=stage,
                        structured={"segments": [
                            {**picture, "tc": f"{n * 2}-{n * 2 + 2}s",
                             "picture": f"shot {n + 1}", "line": f"line {n + 1}"}
                            for n in range(shots)], "total_duration": shots * 2}
                        if stage == "writing" else stage_structure(stage))
    pipeline.record_approval(RUN, "writing", nonce=pipeline.issue_nonce(RUN, "writing"),
                             decision="approved")
    source = store.latest_artifact(RUN, "writing")
    for index in range(1, shots + 1):
        prepare_draft(store, pipeline, RUN, DraftIn(
            request_id=f"frame-{index}", kind="image", prompt=f"frame {index}",
            source_version=source.version, source_sha256=source.sha256,
            model="codex-image", shot_id=f"shot-{index:02d}"), actor="agent")
    return store, pipeline, source


def test_scenario_g_a_shot_revision_is_scoped_and_commits_nothing_else(tmp_path):
    """'第二镜不行，重做一下。' — a revision request, not a group-wide reset.

    Driven through the real orchestrator, because the part that can fail is the
    agent's: it has to read the storyboard it was given, pick the second shot out
    of it, and replace exactly that one. The mechanism is only interesting if the
    conversation actually reaches it.
    """
    from studio.production import coverage, draft_records, target_shots

    store, pipeline, source = storyboard_run(tmp_path)
    before = {row["draft_id"]: row["draft_sha256"]
              for row in draft_records(store, RUN).values()}

    orch, model = orchestrator(store, pipeline, tmp_path, [
        # The designed order: declare what this message is for, look at the
        # storyboard, then replace exactly the shot the user named.
        tool_turn("route", "prepare_creative_context", {
            "intent": "edit", "format": "video", "deliverable": "production_input",
            "production_kind": "image"}),
        tool_turn("read", "read_production", {}),
        tool_turn("revise", "prepare_production", {
            "request_id": "frame-2-v2", "kind": "image",
            "prompt": "frame 2, more everyday",
            "source_version": source.version, "source_sha256": source.sha256,
            "model": "codex-image", "supersedes": "frame-2"}),
        final_turn("第二镜换了一版更日常的描述，其他镜头保持不动。"),
    ])
    orch.send("第二镜不行，重做一下。")

    # The agent was actually shown the shots, so "第二镜" had something to bind to.
    shown = json.dumps(model.calls[2]["messages"], ensure_ascii=False)
    assert "shot-02" in shown and "frame-2" in shown, (
        "the agent has to be able to resolve '第二镜' from what it was given")

    rows = draft_records(store, RUN)
    result = coverage(rows, "image", targets=target_shots(store, RUN, "image"))
    assert result["total"] == 3, "a revision must not become a fourth shot"
    assert result["shots"][1]["draft_id"] == "frame-2-v2"
    assert result["shots"][1]["superseded_draft_ids"] == ["frame-2"]
    # The other two shots are untouched: nothing group-wide was reset.
    assert result["shots"][0]["draft_id"] == "frame-1"
    assert result["shots"][2]["draft_id"] == "frame-3"
    assert rows["frame-2"]["is_current"] is False
    for key in ("frame-1", "frame-3"):
        assert rows[key]["draft_sha256"] == before[key], f"{key} must not be rewritten"
    # A revision is preparation, not a decision and not a spend.
    assert store.list_approvals(RUN, "writing") != []
    assert [e for e in store.read_events(RUN) if e["kind"] == "production_attempt"] == []


# ---------------------------------------------------------------- scenario E -- #

def test_scenario_e_a_generation_request_prepares_one_confirmation_and_submits_nothing(
        tmp_path):
    """'按这六镜生成吧。' — prepared to one confirmation, never submitted.

    The agent has no tool that can submit a generation; what it can do is read the
    exact set a confirmation would cover. This checks that it is given that set,
    that the set is the shots the script asks for, and that nothing reached a
    provider — the two halves of the boundary the design draws.
    """
    from studio.production import target_shots

    store, pipeline, _source = storyboard_run(tmp_path, shots=6)
    orch, model = orchestrator(store, pipeline, tmp_path, [
        tool_turn("route", "prepare_creative_context", {
            "intent": "edit", "format": "video", "deliverable": "production_media",
            "media_kind": "image"}),
        tool_turn("look", "read_user_actions", {}),
        final_turn("六面分镜的生成还差你最后确认一次；我不会替你提交。"),
    ])
    orch.send("按这六镜生成吧。")

    payload = json.dumps(model.calls[2]["messages"], ensure_ascii=False)
    assert "preparable_execution" in payload, (
        "the agent must be able to read the exact set a confirmation would cover")
    assert "authorise_generation" in payload
    assert "只准备" in payload or "needs_clarification" not in payload

    from studio.decision import prepare_generation_candidate

    candidate = prepare_generation_candidate(store, pipeline, RUN, "image")
    targets = target_shots(store, RUN, "image")
    assert candidate["count"] == len(targets["shots"]) == 6, (
        "the set is the shots the script asks for, not a fixed number")
    assert candidate["class"] == "execution"
    assert candidate["estimate"]["known"] is False, "unknown cost stays unknown"

    # Nothing was submitted: preparing is read-only.
    assert [e for e in store.read_events(RUN) if e["kind"] == "production_attempt"] == []
    approvals = store.list_approvals(RUN)
    assert all(a.stage in {"direction", "writing"} for a in approvals), (
        "a generation request must not record a creative decision")


def test_scenario_e_the_agent_has_no_way_to_submit_a_generation():
    """The boundary is the tool set, not the prompt's good intentions."""
    from studio.tools.registry import REGISTRY, tool_names_for

    names = set(tool_names_for("orchestrator"))
    assert "prepare_production" in names, "it may prepare an input"
    for forbidden in ("generate", "generate_media", "batch_generate", "submit_generation",
                      "authorise_generation", "approve"):
        assert forbidden not in names, forbidden
    # And no registered tool whose whole job is to spend.
    assert not any(tool.name.endswith("_generate") for tool in REGISTRY.values())


# ----------------------------------------------------------- authority bounds -- #

def test_the_decision_tools_are_orchestrator_only():
    """A specialised role must not be able to record the user's decision."""
    for role in ("director", "material", "writer", "reviewer"):
        names = tool_names_for(role)
        assert "propose_user_action" not in names, role
        assert "read_user_actions" not in names, role
    assert "propose_user_action" in tool_names_for("orchestrator")


def test_no_agent_tool_can_write_an_approval_directly():
    """The old red line, restated: the agent proposes, the server records."""
    from studio.tools.registry import REGISTRY

    for name, tool in REGISTRY.items():
        assert "approval" not in name or tool.roles == (), name
        assert name not in {"write_approval", "approve"}, name


def test_an_agent_cannot_adopt_a_version_the_user_never_saw(tmp_path):
    """The validator, not the prompt, is what refuses a wrong binding."""
    store, pipeline = seed_direction(tmp_path)
    question = candidates(store, pipeline, RUN)[0]
    orch, _ = orchestrator(store, pipeline, tmp_path, [
        tool_turn("pick", "propose_user_action", {
            "action": "select_direction",
            "target": {"stage": "direction", "version": question["version"] + 5,
                       "artifact_sha256": "0" * 64},
            "option_id": "B",
        }),
        final_turn("这版已经不是当前等待决定的版本，请核对后再确认。"),
    ])
    orch.send("采用 B。")
    assert store.list_approvals(RUN, "direction") == []
    rejected = [e for e in store.read_events(RUN) if e["kind"] == "user_action_rejected"]
    assert rejected, "a refused proposal has to leave a trace"


def test_the_recommended_option_is_never_substituted_for_the_users_choice(tmp_path):
    """A proposal with no option named must ask, not fall back to the advice."""
    store, pipeline = seed_direction(tmp_path)
    question = candidates(store, pipeline, RUN)[0]
    orch, model = orchestrator(store, pipeline, tmp_path, [
        tool_turn("pick", "propose_user_action", {
            "action": "select_direction",
            "target": {"stage": "direction", "version": question["version"],
                       "artifact_sha256": question["artifact_sha256"]},
        }),
        final_turn("你想采用哪个方向？A／B／C。"),
    ])
    orch.send("就用你说的那个吧。")
    assert store.list_approvals(RUN, "direction") == []
    # The agent was told to ask, with a question naming the real options.
    tool_result = json.dumps(model.calls[1]["messages"], ensure_ascii=False)
    assert "needs_clarification" in tool_result
    assert "A／B／C" in tool_result
