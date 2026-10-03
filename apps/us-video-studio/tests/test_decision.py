"""Natural-language decisions: what is offered, what resolves, what must ask.

The review's F5 asked for a control model where the conversation can make a
decision, the server decides whether it is safe to record, and an ambiguous
message asks instead of guessing. These tests pin the rules from
``docs/conversational-control.md`` §6 — the short-reply rules are where a wrong
guess records the wrong irreversible thing.
"""
from __future__ import annotations

import pytest

from studio.decision import CREATIVE, EXECUTION, actions, candidates, resolve
from studio.models import STAGE_SPEC
from studio.pipeline import Pipeline
from studio.store import Store

RUN = "decisions"
SHA_A = "a" * 64
SHA_B = "b" * 64


def direction_question(version=7, sha=SHA_A, options=("A", "B", "C")):
    return {
        "question": "这一版创意方向要采用还是退回？", "kind": "decision_request",
        "stage": "direction", "version": version, "artifact_sha256": sha,
        "label": "创意方向", "target": {"stage": "direction", "version": version,
                                        "artifact_sha256": sha},
        "answers": [
            {"action": "select_direction", "class": CREATIVE, "label": "采用某个方向",
             "options": [{"id": key, "label": f"方向 {key}"} for key in options],
             "requires": ["option_id"]},
            {"action": "return_stage", "class": CREATIVE, "label": "退回并修改",
             "requires": ["note"]},
        ],
    }


def script_question(action="approve_stage", version=2, sha=SHA_B):
    """A question on the script gate. Both answers are always offered."""
    return {"question": "这一版脚本要采用还是退回？", "kind": "decision_request",
            "stage": "writing", "version": version, "artifact_sha256": sha,
            "label": "脚本", "target": {"stage": "writing", "version": version,
                                        "artifact_sha256": sha},
            "answers": [{"action": "approve_stage", "class": CREATIVE, "label": "采用当前脚本"},
                        {"action": "return_stage", "class": CREATIVE, "label": "退回并修改",
                         "requires": ["note"]}]}


def only_answer(question, action):
    """The one legitimate answer of this action id, as an agent would propose it."""
    answer = next(a for a in question["answers"] if a["action"] == action)
    return {**answer, "target": dict(question["target"]), "stage": question["stage"],
            "version": question["version"], "artifact_sha256": question["artifact_sha256"]}


# --------------------------------------------------------------- resolution -- #

def test_an_empty_run_asks_rather_than_acting():
    result = resolve([], {"action": "approve_stage"})
    assert result["status"] == "clarify"
    assert result["reason"] == "nothing_open"


def test_no_proposal_at_all_asks():
    assert resolve([script_question()], None)["status"] == "clarify"
    assert resolve([script_question()], {})["reason"] == "no_action_proposed"


def test_a_single_open_question_accepts_a_bare_adoption():
    """Case C: one question open, '行' answers it."""
    result = resolve([script_question()], {"action": "approve_stage"})
    assert result["status"] == "commit"
    assert result["action"]["target"]["stage"] == "writing"


def test_several_open_questions_never_resolve_from_a_bare_reply():
    """Case D: '行' cannot say which question it answers."""
    result = resolve([direction_question(), script_question()],
                     {"action": "approve_stage", "target": {"stage": "writing"}})
    assert result["status"] == "clarify"
    assert result["reason"] == "several_decisions_open"
    # The question has to name the real alternatives, not a generic apology.
    assert "脚本" in result["question"] and "创意方向" in result["question"]


def test_selecting_a_direction_resolves_inside_a_set_of_options():
    """Case A: naming an option is what makes it unambiguous."""
    result = resolve([direction_question()],
                     {**only_answer(direction_question(), "select_direction"),
                      "target": {**direction_question()["target"], "option_id": "B"}})
    assert result["status"] == "commit"
    assert result["action"]["target"]["option_id"] == "B"


def test_selecting_a_direction_without_naming_one_asks_instead_of_recommending():
    """The recommended option is not a substitute for the user's answer."""
    result = resolve([direction_question()], only_answer(direction_question(), "select_direction"))
    assert result["status"] == "clarify"
    assert result["reason"] == "option_not_named"
    assert "A／B／C" in result["question"]


def test_selecting_an_option_that_is_not_offered_is_refused():
    result = resolve([direction_question(options=("A", "C"))],
                     {**only_answer(direction_question(options=("A", "C")), "select_direction"),
                      "target": {"stage": "direction", "version": 7,
                                 "artifact_sha256": SHA_A, "option_id": "B"}})
    assert result["status"] == "reject"
    assert result["reason"] == "option_not_available"


def test_a_decision_bound_to_an_old_version_is_refused():
    """Accurate binding is the property that makes a conversation decision safe."""
    result = resolve([direction_question(version=7)],
                     {**only_answer(direction_question(), "select_direction"),
                      "target": {"stage": "direction", "version": 6,
                                 "artifact_sha256": SHA_A, "option_id": "B"}})
    assert result["status"] == "reject"
    assert result["reason"] == "version_not_current"


def test_a_decision_bound_to_the_wrong_fingerprint_is_refused():
    result = resolve([direction_question(version=7, sha=SHA_A)],
                     {**only_answer(direction_question(), "select_direction"),
                      "target": {"stage": "direction", "version": 7,
                                 "artifact_sha256": SHA_B, "option_id": "B"}})
    assert result["status"] == "reject"
    assert result["reason"] == "version_not_current"


def test_returning_without_saying_what_to_change_asks():
    result = resolve([script_question()], only_answer(script_question(), "return_stage"))
    assert result["status"] == "clarify"
    assert result["reason"] == "return_needs_note"

    with_note = resolve([script_question()],
                        {**only_answer(script_question(), "return_stage"), "note": "第二镜太像广告"})
    assert with_note["status"] == "commit"
    assert with_note["action"]["note"] == "第二镜太像广告"


def test_an_action_the_run_does_not_offer_asks_instead_of_inventing_one():
    result = resolve([direction_question()],
                     {"action": "authorise_generation", "target": {"stage": "direction"}})
    assert result["status"] == "clarify"
    assert result["reason"] in {"action_not_available"}


# --------------------------------------------------------------- candidates -- #

SCRIPT = {
    "segments": [
        {"tc": "0-2s", "picture": "包出现在桌面", "line": "One.", "sound": "ambient", "duration": 2},
        {"tc": "2-4s", "picture": "拉链打开", "line": "Two.", "sound": "zip", "duration": 2},
    ],
    "total_duration": 4,
}


def seeded(tmp_path, *, approve_direction=False):
    """A run sitting exactly on the direction gate, unless told otherwise.

    ``material_plan`` cannot start until direction is adopted, so the stage the
    pending decision is on has to be the last one written. Writing more here
    would leave the fixture unable to represent the state under test.
    """
    store = Store(tmp_path)
    store.create_run(RUN, "Decision fixture", "Synthetic only")
    pipeline = Pipeline(store)
    for stage in ["intake", "direction"]:
        attempt = pipeline.start_attempt(RUN, stage)
        structured = ({"options": [{"key": "A", "claim": "One", "satisfies": "goal.primary"},
                                   {"key": "B", "claim": "Two", "satisfies": "goal.primary"},
                                   {"key": "C", "claim": "Three", "satisfies": "goal.primary"}]}
                      if stage == "direction" else {})
        pipeline.submit(RUN, stage, attempt.token, content=f"Synthetic {stage}",
                        summary=stage, structured=structured)
    if approve_direction:
        pipeline.record_approval(RUN, "direction", nonce=pipeline.issue_nonce(RUN, "direction"),
                                 decision="approved", selected_option_id="A")
    return store, pipeline


def received_message(store, pipeline, run_id, text):
    """The same persisted view+ingress used by HTTP, in an isolated fixture."""
    import uuid
    from studio.conversation_input import display_context, receive
    from studio.ledger import Ledger
    from studio.timeline import build_timeline
    from test_server import settings_for
    ledger = Ledger(store, settings_for(store.root))
    timeline = build_timeline(store, pipeline, ledger, run_id, None)
    context = display_context(store, pipeline, run_id, timeline)
    return receive(store, run_id, text, uuid.uuid4().hex, context)


def test_a_waiting_direction_offers_every_admissible_option(tmp_path):
    store, pipeline = seeded(tmp_path)
    decisions = candidates(store, pipeline, RUN)
    assert len(decisions) == 1
    question = decisions[0]
    assert question["stage"] == "direction" and question["version"] == 1
    selecting = next(a for a in question["answers"] if a["action"] == "select_direction")
    assert {option["id"] for option in selecting["options"]} == {"A", "B", "C"}
    assert selecting["class"] == CREATIVE
    # Adopting a version that proposes options *is* naming one of them.
    assert "approve_stage" not in {a["action"] for a in question["answers"]}


def test_a_decided_direction_offers_nothing_more(tmp_path):
    store, pipeline = seeded(tmp_path, approve_direction=True)
    assert candidates(store, pipeline, RUN) == []


def test_candidates_come_from_the_same_projection_the_page_shows(tmp_path):
    """Two derivations of "what is waiting on him" is how screen and chat drift."""
    from studio.ledger import Ledger
    from studio.timeline import build_timeline
    from test_server import settings_for

    store, pipeline = seeded(tmp_path)
    timeline = build_timeline(store, pipeline, Ledger(store, settings_for(tmp_path)), RUN, None)
    pending = timeline["pending"]
    assert len(candidates(store, pipeline, RUN)) == 1
    question = candidates(store, pipeline, RUN)[0]
    assert question["version"] == pending["version"]
    assert question["artifact_sha256"] == pending["artifact_sha256"]

    # Every answer the resolver would accept is one the page also offers.
    page_actions = set()
    for answer in question["answers"]:
        page_actions.add(answer["action"])
    assert "select_direction" in page_actions
    assert all(row["action"] in page_actions for row in actions([question]))


def test_a_decision_records_its_source_and_opens_the_follow_up_work(tmp_path):
    store, pipeline = seeded(tmp_path)
    from studio.decision import commit, continuation_intent

    question = candidates(store, pipeline, RUN)[0]
    selecting = next(a for a in question["answers"] if a["action"] == "select_direction")
    words = "采用 B，继续出分镜；开头别那么像家庭广告，生活化一点"
    receipt = received_message(store, pipeline, RUN, words)
    result = commit(store, pipeline, RUN,
                    {**selecting, "target": dict(question["target"]), "option_id": "B",
                     "message_id": receipt['message_id']},
                    source="conversation",
                    user_message="采用 B，继续出分镜；开头别那么像家庭广告，生活化一点",
                    note="开头别那么像家庭广告，生活化一点")
    assert result["decision"] == "approved"
    assert result["selected_option_id"] == "B"

    approval = store.list_approvals(RUN, "direction")[-1]
    assert approval.source == "conversation"
    assert approval.continuation_note == "开头别那么像家庭广告，生活化一点"
    assert approval.note == "", "an adoption cannot carry changes to the version it adopts"

    intent = continuation_intent(store, pipeline, RUN)
    assert intent is not None and intent["stage"] == "direction"
    assert intent["request_id"] == f"decision:{approval.nonce_sha256}"

    events = [e for e in store.read_events(RUN) if e["kind"] == "user_action_resolved"]
    assert len(events) == 1 and events[0]["source"] == "conversation"


def test_a_decision_that_moved_underneath_is_refused_not_guessed(tmp_path):
    store, pipeline = seeded(tmp_path)
    from studio.decision import DecisionError, commit

    stale = {"action": "select_direction", "option_id": "B",
             "target": {"stage": "direction", "version": 0, "artifact_sha256": "c" * 64}}
    with pytest.raises(DecisionError):
        commit(store, pipeline, RUN, stale, source="conversation")
    assert store.list_approvals(RUN, "direction") == []


def test_provenance_fields_are_not_source_facts(tmp_path, monkeypatch):
    """Which channel a decision came through must not invalidate anything.

    The snapshot legitimately moves when a decision *is made* — the adopted
    option is a source fact. What must not move it is the provenance that rides
    along with that decision, or every conversation decision would invalidate the
    production baseline that followed it.

    Read through the projection itself rather than by appending a second record:
    a store holds one approval per version, so a hand-appended twin would be
    testing something the system never produces.
    """
    from dataclasses import replace

    from studio import production
    from studio.store import Store

    store, pipeline = seeded(tmp_path)
    pipeline.record_approval(RUN, "direction", nonce=pipeline.issue_nonce(RUN, "direction"),
                             decision="approved", selected_option_id="A")
    plain = [a for a in store.list_approvals(RUN) if a.stage == "direction"][-1]

    def with_approval(approval):
        return lambda self, run_id, stage=None: (
            [approval] if stage in (None, "direction") else [])

    monkeypatch.setattr(Store, "list_approvals",
                        with_approval(replace(plain, selected_option_id="A",
                                              source="conversation",
                                              continuation_note="下一轮把开头改生活化")))
    provenance_only = production.source_snapshot(store, RUN)

    monkeypatch.setattr(Store, "list_approvals",
                        with_approval(replace(plain, selected_option_id="C",
                                              source="conversation",
                                              continuation_note="下一轮把开头改生活化")))
    decided_differently = production.source_snapshot(store, RUN)

    monkeypatch.setattr(Store, "list_approvals",
                        with_approval(replace(plain, selected_option_id="C",
                                              source="ui", continuation_note="")))
    plain_but_other_option = production.source_snapshot(store, RUN)

    # Provenance is invisible; the adopted option is not.
    assert decided_differently != provenance_only
    assert decided_differently == plain_but_other_option


def test_an_execution_authorisation_can_only_be_prepared_not_submitted(tmp_path):
    """The cost-bearing step reaches 'one confirmation away' and no further.

    What the conversation may do is describe exactly what a confirmation would
    cover. It must not create that confirmation, and it must not submit anything —
    the numbers have to match what the workbench page would show, or the card in
    the chat and the page would describe different sets.
    """
    from studio.decision import EXECUTION, prepare_generation_candidate
    from studio.production import DraftIn, prepare_draft, target_shots
    from visual_fixtures import stage_structure

    store, pipeline = seeded(tmp_path)
    pipeline.record_approval(RUN, "direction", nonce=pipeline.issue_nonce(RUN, "direction"),
                             decision="approved", selected_option_id="A")
    for stage in ("material_plan", "writing"):
        attempt = pipeline.start_attempt(RUN, stage)
        pipeline.submit(RUN, stage, attempt.token, content=f"doc {stage}", summary=stage,
                        structured={"segments": [
                            {"tc": "0-2s", "picture": "one", "line": "a", "sound": "s", "duration": 2,
                             "character_ids": "", "scene_id": "table"},
                            {"tc": "2-4s", "picture": "two", "line": "b", "sound": "s", "duration": 2,
                             "character_ids": "", "scene_id": "table"},
                        ], "total_duration": 4} if stage == "writing" else stage_structure(stage))
    pipeline.record_approval(RUN, "writing", nonce=pipeline.issue_nonce(RUN, "writing"),
                             decision="approved")

    # Nothing prepared yet: there is nothing to confirm, and no card is offered.
    assert prepare_generation_candidate(store, pipeline, RUN, "image") is None

    source = store.latest_artifact(RUN, "writing")
    for index in (1, 2):
        prepare_draft(store, pipeline, RUN, DraftIn(
            request_id=f"shot-{index}", kind="image", prompt=f"frame {index}",
            source_version=source.version, source_sha256=source.sha256,
            model="codex-image", shot_id=f"shot-{index:02d}"), actor="agent")

    candidate = prepare_generation_candidate(store, pipeline, RUN, "image")
    assert candidate is not None
    assert candidate["class"] == EXECUTION
    assert candidate["count"] == len(target_shots(store, RUN, "image")["shots"]) == 2
    assert [row["draft_id"] for row in candidate["target"]["drafts"]] == ["shot-1", "shot-2"]
    assert candidate["target"]["source_sha256"] == source.sha256
    assert candidate["estimate"]["known"] is False, "unknown cost stays unknown"

    # Preparing is read-only: no attempt, no provider call, no authorisation.
    attempts = [e for e in store.read_events(RUN) if e["kind"] == "production_attempt"]
    assert attempts == [], "preparing a confirmation must not start a generation"


def test_the_conversation_channel_uses_a_nonce_no_agent_ever_sees(tmp_path):
    """The nonce still exists; only its holder differs."""
    from studio.decision import commit

    store, pipeline = seeded(tmp_path)
    question = candidates(store, pipeline, RUN)[0]
    selecting = next(a for a in question["answers"] if a["action"] == "select_direction")
    receipt = received_message(store, pipeline, RUN, "用 B")
    commit(store, pipeline, RUN,
           {**selecting, "target": dict(question["target"]), "option_id": "B",
            "message_id": receipt['message_id']},
           source="conversation", user_message="用 B")

    approval = store.list_approvals(RUN, "direction")[-1]
    assert approval.source == "conversation"
    # Issued through the same nonce machinery, written down only as a hash.
    issued = [e for e in store.read_events(RUN) if e["kind"] == "nonce_issued"]
    assert any(e["nonce_sha256"] == approval.nonce_sha256 for e in issued)
    assert all("nonce" not in e for e in issued), "the plaintext must never be written down"


def test_gated_stages_are_exactly_the_ones_a_conversation_may_decide(tmp_path):
    """The class boundary is a domain fact, not a judgement call in the prompt."""
    gated = {name for name, spec in STAGE_SPEC.items() if spec.gated}
    assert gated == {"direction", "writing"}
