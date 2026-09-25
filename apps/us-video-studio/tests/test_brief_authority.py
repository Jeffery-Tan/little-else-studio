"""F2–F5 — the ActiveBrief gate, the goal's authority, and the redo hand-off.

Every one of these pins a way the workspace let something through:

* F2 — a draft that declared nothing was read as "符合约束" and could be adopted;
* F3 — rewriting intake redefined the effective goal without a decision;
* F4 — a *diagnosis* string was offered as the replacement body for the goal;
* F5 — approving an amendment invalidated the current direction and then did
  nothing, leaving the user to find the redo himself.

The HTTP cases drive the real routes on a loopback client with a scripted model:
no network, no spend, no media.
"""
from __future__ import annotations

import pytest
from local_http import TestClient

from fakes import ScriptedModel, final_turn, tool_turn
from studio import active_brief as ab
from studio.config import Pricing, Settings
from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.server import build_app
from studio.store import Store

RUN = "brief-gate-01"


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
def store(tmp_path):
    s = Store(tmp_path)
    s.create_run(RUN, "US 种草短视频", "做一个时尚达人种草向的带货视频，15 秒。")
    return s


def submit_intake(pipe, *, goal="US 线时尚达人种草向带货视频", constraints=("台词用美式英语",),
                  version_body="intake body"):
    attempt = pipe.start_attempt(RUN, "intake")
    return pipe.submit(
        RUN, "intake", attempt.token, content=version_body, summary="intake",
        structured={
            "facts": [{"label": "交付形态", "value": goal, "source": "u"}],
            "constraints": list(constraints),
            "open_questions": [],
        },
    )


# ------------------------------------------------------------------ F2 ----- #


def _gated_direction(store, *, options, recommended="A"):
    """A direction artifact that *is* bound to the current brief, ready to adopt."""
    pipe = Pipeline(store)
    submit_intake(pipe)
    attempt = pipe.start_attempt(RUN, "direction")
    artifact = pipe.submit(RUN, "direction", attempt.token, content="方向正文", summary="方向",
                           structured={"options": options, "recommended": recommended})
    assert "brief:snapshot" in artifact.inputs
    pipe.record_review(RUN, "direction", verdict="pass", findings=[])
    return pipe, artifact


def test_all_options_unmapped_cannot_be_adopted(store):
    """The first minimal input from the review: one option, no mapping at all."""
    pipe, _ = _gated_direction(store, options=[{"key": "A", "claim": "一个新方向"}])
    gate = pipe.stage_state(RUN, "direction").brief_gate
    assert gate["status"] == "blocked", "unmapped must not read as unblocked"
    assert gate["bound"] is True
    assert [b["kind"] for b in gate["blockers"]] == ["unmapped"]

    nonce = pipe.issue_nonce(RUN, "direction")
    with pytest.raises(PipelineError) as err:
        pipe.record_approval(RUN, "direction", nonce=nonce, decision="approved",
                             selected_option_id="A")
    assert err.value.rule == "brief"
    assert "unmapped" in str(err.value)


def test_a_mapped_recommendation_does_not_let_an_unmapped_option_be_selected(store):
    """The second minimal input: A is mapped, B is not, and B is what he picks."""
    pipe, _ = _gated_direction(
        store,
        options=[{"key": "A", "satisfies": "goal.primary"},
                 {"key": "B", "claim": "未映射备选"}],
    )
    gate = pipe.stage_state(RUN, "direction").brief_gate
    assert gate["status"] == "ok", "the recommended option is fine; B is the problem"
    assert [b["kind"] for b in gate["blockers"]] == ["unmapped"]

    nonce = pipe.issue_nonce(RUN, "direction")
    with pytest.raises(PipelineError) as err:
        pipe.record_approval(RUN, "direction", nonce=nonce, decision="approved",
                             override_reason="我就是要硬过", selected_option_id="B")
    assert err.value.rule == "brief"

    # ...and the mapped option is still adoptable, on a fresh nonce.
    approval = pipe.record_approval(RUN, "direction", nonce=pipe.issue_nonce(RUN, "direction"),
                                    decision="approved", selected_option_id="A")
    assert approval.selected_option_id == "A"


def test_an_alternative_that_only_maps_a_side_clause_is_blocked(store):
    """R1: the reviewer's counterexample, and it needed its own rule.

    B declares satisfaction of a real, non-goal clause — so the "declared
    nothing" and "unknown id" rules are both silent — while never being measured
    against the product goal. The goal check therefore applies to every option,
    not only to the recommended one.
    """
    pipe = Pipeline(store)
    submit_intake(pipe)
    language = next(c for c in ab.compile_active_brief(store, RUN)["clauses"]
                    if c["text"] == "台词用美式英语")
    attempt = pipe.start_attempt(RUN, "direction")
    pipe.submit(RUN, "direction", attempt.token, content="方向正文", summary="方向",
                structured={"options": [{"key": "A", "satisfies": "goal.primary"},
                                        {"key": "B", "satisfies": language["id"]}],
                            "recommended": "A"})
    pipe.record_review(RUN, "direction", verdict="pass", findings=[])

    gate = pipe.stage_state(RUN, "direction").brief_gate
    assert gate["status"] == "ok", "the recommendation is compliant"
    b_blockers = [b for b in gate["blockers"] if b["key"] == "B"]
    assert [b["kind"] for b in b_blockers] == ["goal_uncovered"]
    assert "goal.primary" in b_blockers[0]["detail"]

    nonce = pipe.issue_nonce(RUN, "direction")
    with pytest.raises(PipelineError) as err:
        pipe.record_approval(RUN, "direction", nonce=nonce, decision="approved",
                             override_reason="硬过", selected_option_id="B")
    assert err.value.rule == "brief"
    assert "goal_uncovered" in str(err.value)


def test_changing_only_the_recommendation_does_not_change_admission(store):
    """Same options, different `recommended`: the per-option answer must not move."""
    pipe = Pipeline(store)
    submit_intake(pipe)
    admission = {}
    for recommended in ("A", "B"):
        attempt = pipe.start_attempt(RUN, "direction")
        pipe.submit(RUN, "direction", attempt.token, content="方向正文", summary="方向",
                    structured={"options": [{"key": "A", "satisfies": "goal.primary"},
                                            {"key": "B", "satisfies": "goal.primary"}],
                                "recommended": recommended})
        gate = pipe.stage_state(RUN, "direction").brief_gate
        admission[recommended] = {r["key"]: r["admissible"] for r in gate["options"]}
    assert admission["A"] == admission["B"] == {"A": True, "B": True}


def test_a_compliant_alternative_stays_adoptable_when_the_recommendation_is_blocked(store):
    """R3: one bad recommendation must not take the good option's button away.

    The whole-stage card switches to `brief_amendment` only when *nothing* can be
    adopted; the approval route follows the same per-option answer.
    """
    from studio.config import Pricing, Settings
    from studio.ledger import Ledger
    from studio.timeline import _pending_action

    pipe, _ = _gated_direction(
        store,
        options=[{"key": "A", "violates": "goal.primary", "claim": "降级目标"},
                 {"key": "B", "satisfies": "goal.primary"}],
    )
    gate = pipe.stage_state(RUN, "direction").brief_gate
    assert gate["status"] == "blocked", "the recommendation is the blocked one"
    assert {r["key"]: r["admissible"] for r in gate["options"]} == {"A": False, "B": True}

    ledger = Ledger(store, Settings(data_root=store.root,
                                    base_url="https://example.invalid/anthropic",
                                    pricing=Pricing(source="test", input_usd_per_mtok=1.0,
                                                    output_usd_per_mtok=2.0, as_of="2026-09-17")))
    pending = _pending_action(pipe.state(RUN).to_dict(), ledger, store)
    assert pending["kind"] == "decision_request", "B still has an adoption entry"
    rows = {row["key"]: row for row in pending["options"]}
    assert rows["B"]["admissible"] is True
    assert rows["A"]["admissible"] is False
    assert rows["A"]["blocked_reason"], "the blocked option says why"

    # And the server agrees with the card it drew.
    approval = pipe.record_approval(RUN, "direction", nonce=pipe.issue_nonce(RUN, "direction"),
                                    decision="approved", selected_option_id="B")
    assert approval.selected_option_id == "B"


def test_an_unknown_clause_id_blocks_the_option_that_cites_it(store):
    pipe, _ = _gated_direction(
        store,
        options=[{"key": "A", "satisfies": "goal.primary"},
                 {"key": "B", "satisfies": "must.deadbeef"}],
    )
    gate = pipe.stage_state(RUN, "direction").brief_gate
    kinds = {b["kind"] for b in gate["blockers"] if b["key"] == "B"}
    assert "unknown_clause" in kinds

    nonce = pipe.issue_nonce(RUN, "direction")
    with pytest.raises(PipelineError):
        pipe.record_approval(RUN, "direction", nonce=nonce, decision="approved",
                             selected_option_id="B")


def test_a_declared_violation_still_blocks(store):
    pipe, _ = _gated_direction(
        store,
        options=[{"key": "A", "satisfies": "goal.primary", "violates": "goal.primary"}],
    )
    gate = pipe.stage_state(RUN, "direction").brief_gate
    assert any(b["kind"] == "violates" for b in gate["blockers"])
    nonce = pipe.issue_nonce(RUN, "direction")
    with pytest.raises(PipelineError):
        pipe.record_approval(RUN, "direction", nonce=nonce, decision="approved",
                             selected_option_id="A")


def test_a_conforming_plan_is_still_adoptable(store):
    """The gate must not become a wall: a declared, mapped plan goes through."""
    pipe, _ = _gated_direction(
        store,
        options=[{"key": "A", "satisfies": "goal.primary"},
                 {"key": "B", "satisfies": "goal.primary"}],
    )
    gate = pipe.stage_state(RUN, "direction").brief_gate
    assert gate["status"] == "ok" and gate["blockers"] == []
    approval = pipe.record_approval(RUN, "direction", nonce=pipe.issue_nonce(RUN, "direction"),
                                   decision="approved", selected_option_id="B")
    assert approval.decision == "approved"


def test_a_legacy_draft_without_the_brief_key_is_not_blocked_or_staled(tmp_path):
    """Old drafts stay readable and adoptable-ish: no silent mass invalidation.

    The bound flag is what separates the two. Without `brief:snapshot` there is
    no claim about *which* brief was checked, so the gate reports `unmapped` and
    declines to invent a blocker — the stage is simply not gated on this axis.
    """
    from studio.evidence import evidence_fingerprint, evidence_generation

    s = Store(tmp_path)
    s.create_run(RUN, "t", "时尚达人种草向带货视频")
    pipe = Pipeline(s)
    submit_intake(pipe)
    s.write_artifact(
        RUN, "direction", 1, "legacy direction", summary="legacy", author_role="director",
        attempt="direction#1",
        inputs={
            "intake": pipe.stage_state(RUN, "intake").latest.sha256,
            "evidence:snapshot": evidence_fingerprint(s, RUN),
            "evidence:generation": str(evidence_generation(s, RUN)),
        },
        structured={"options": [{"key": "A", "claim": "x"}], "recommended": "A"},
    )
    state = pipe.stage_state(RUN, "direction")
    assert not state.stale
    assert state.brief_gate["status"] == "unmapped"
    assert state.brief_gate["bound"] is False
    assert state.brief_gate["blockers"] == []
    assert state.to_dict()["brief_gate"]["bound"] is False


# ------------------------------------------------------------------ F3 ----- #


def test_rewriting_intake_cannot_change_the_effective_goal(store):
    """The model's reading may change freely; the objective may not."""
    pipe = Pipeline(store)
    submit_intake(pipe, goal="US 线时尚达人种草向带货视频")
    before = ab.compile_active_brief(store, RUN)
    assert before["goal"]["authority"] == "user"
    assert "时尚达人种草" in before["goal"]["text"]

    # The kind of rewrite the review describes: narrower delivery form, plus a
    # new hard constraint. This is the model doing its job on facts.
    attempt = pipe.start_attempt(RUN, "intake")
    pipe.submit(RUN, "intake", attempt.token, content="intake v2", summary="s2",
                structured={
                    "facts": [{"label": "交付形态", "value": "只要 8 秒设计细节演示",
                              "source": "u"}],
                    "constraints": ["台词用美式英语", "不得虚构购买经历"],
                    "open_questions": [],
                })
    after = ab.compile_active_brief(store, RUN)
    assert after["goal"]["text"] == before["goal"]["text"], "the goal did not move"
    assert after["goal"]["authority"] == "user"
    # The new constraint is a fact and does take effect.
    assert any("不得虚构购买经历" == c["text"] for c in after["clauses"])
    # The model's reworked reading is visible as a proposal, not as the goal.
    readings = [c for c in after["clauses"] if c.get("authority") == "intake_proposal"]
    assert len(readings) == 1 and "8 秒设计细节演示" in readings[0]["text"]


def test_only_an_approved_amendment_moves_the_goal(store):
    pipe = Pipeline(store)
    submit_intake(pipe)
    before = ab.fingerprint(ab.compile_active_brief(store, RUN))

    proposed = ab.propose_amendment(store, RUN, changes=[{
        "clause_id": "goal.primary", "op": "replace", "text": "目标收窄为设计细节种草"}],
        approve=False)
    assert proposed["status"] == "pending"
    assert ab.fingerprint(ab.compile_active_brief(store, RUN)) == before, "a pending one changes nothing"

    ab.decide_amendment(store, RUN, proposed["id"], decision="rejected")
    assert ab.fingerprint(ab.compile_active_brief(store, RUN)) == before, "a rejection changes nothing"

    approved = ab.propose_amendment(store, RUN, changes=[{
        "clause_id": "goal.primary", "op": "replace", "text": "目标收窄为设计细节种草"}],
        approve=True)
    moved = ab.compile_active_brief(store, RUN)
    assert moved["goal"]["text"] == "目标收窄为设计细节种草"
    assert moved["goal"]["authority"] == "amendment"
    assert moved["version"] == 2
    assert ab.fingerprint(moved) != before
    assert approved["status"] == "approved"


def test_a_goal_change_invalidates_the_direction_written_before_it(store):
    pipe, _ = _gated_direction(
        store, options=[{"key": "A", "satisfies": "goal.primary"}])
    assert not pipe.stage_state(RUN, "direction").stale

    ab.propose_amendment(store, RUN, changes=[{
        "clause_id": "goal.primary", "op": "replace", "text": "改拍设计细节种草"}],
        approve=True)
    after = pipe.stage_state(RUN, "direction")
    assert after.stale, "downstream work written against the old goal must not stay valid"
    assert "简报" in after.stale_reason


def test_ordinary_fact_additions_are_not_treated_as_a_goal_change(store):
    """The gate must not fire on every new fact, or it becomes noise."""
    pipe = Pipeline(store)
    submit_intake(pipe)
    before = ab.compile_active_brief(store, RUN)
    assert all(c.get("authority") != "amendment" for c in before["clauses"])
    assert before["version"] == 1


# ------------------------------------------------------------------ T1 ----- #


def _approve_clause(store, clause_id, text):
    return ab.apply_brief_amendment(store, RUN, changes=[{
        "clause_id": clause_id, "op": "replace", "text": text}], approve=True)


def _rewrite_intake(pipe, constraints):
    attempt = pipe.start_attempt(RUN, "intake")
    pipe.submit(RUN, "intake", attempt.token, content="intake rewrite", summary="s",
                structured={
                    "facts": [{"label": "交付形态", "value": "US 线时尚达人种草向带货视频",
                              "source": "u"}],
                    "constraints": list(constraints),
                    "open_questions": [],
                })


def test_a_chain_of_amendments_leaves_only_the_latest_decision(store):
    """T1: A → B → C, and the intake already carries C.

    Replaying the first record found neither A's id nor A's or B's body, so it
    recovered B and raised a conflict that the second record then failed to clear
    — the brief ended up carrying both B and C plus a doubt the user had already
    settled, and that doubt blocked every direction.
    """
    pipe = Pipeline(store)
    submit_intake(pipe, constraints=("台词用美式英语",))
    a = next(c for c in ab.compile_active_brief(store, RUN)["clauses"]
             if c["text"] == "台词用美式英语")
    b_text = "台词用美式英语，且不得虚构购买经历"
    c_text = "台词用自然美式英语，且不得虚构购买经历和保温实测"
    _approve_clause(store, a["id"], b_text)
    b_id = next(c["id"] for c in ab.compile_active_brief(store, RUN)["clauses"]
                if c["text"] == b_text)
    _approve_clause(store, b_id, c_text)

    # The team re-summarises intake and keeps exactly the newest approved C.
    _rewrite_intake(pipe, [c_text])

    brief = ab.compile_active_brief(store, RUN)
    texts = [c["text"] for c in brief["clauses"]]
    assert c_text in texts
    assert b_text not in texts, "the superseded decision is not a second clause"
    assert texts.count(c_text) == 1, "and the current one is not duplicated"
    assert brief["conflicts"] == [], "a settled disagreement is not an open conflict"


def test_a_chain_still_blocks_when_the_intake_truly_diverges(store):
    """The counter-case: a real divergence must survive the supersede logic."""
    pipe = Pipeline(store)
    submit_intake(pipe, constraints=("台词用美式英语",))
    a = next(c for c in ab.compile_active_brief(store, RUN)["clauses"]
             if c["text"] == "台词用美式英语")
    b_text = "台词用美式英语，且不得虚构购买经历"
    c_text = "台词用自然美式英语，且不得虚构购买经历和保温实测"
    _approve_clause(store, a["id"], b_text)
    b_id = next(c["id"] for c in ab.compile_active_brief(store, RUN)["clauses"]
                if c["text"] == b_text)
    _approve_clause(store, b_id, c_text)

    # Now intake carries neither the user's versions nor anything equivalent.
    _rewrite_intake(pipe, ["全片必须出现容量对比画面"])

    brief = ab.compile_active_brief(store, RUN)
    assert c_text in [c["text"] for c in brief["clauses"]], "the decision is still carried"
    assert brief["conflicts"], "and the divergence is still reported"
    assert brief["conflicts"][0]["amendment_clause_id"] == b_id

    gate = ab.brief_gate(store, RUN, {
        "options": [{"key": "A", "satisfies": "goal.primary"}], "recommended": "A"},
        bound=True)
    assert gate["options"][0]["admissible"] is False, "and it still blocks adoption"


def test_retiring_a_conflicting_clause_settles_the_conflict(store):
    """T1's third case: the user resolves it, so the block must go with it."""
    pipe = Pipeline(store)
    submit_intake(pipe, constraints=("台词用美式英语",))
    a = next(c for c in ab.compile_active_brief(store, RUN)["clauses"]
             if c["text"] == "台词用美式英语")
    b_text = "台词用美式英语，且不得虚构购买经历"
    _approve_clause(store, a["id"], b_text)
    _rewrite_intake(pipe, ["全片必须出现容量对比画面"])
    assert ab.compile_active_brief(store, RUN)["conflicts"]

    ab.apply_brief_amendment(store, RUN, changes=[{
        "clause_id": b_text and a["id"], "op": "retire"}], approve=True)

    brief = ab.compile_active_brief(store, RUN)
    assert b_text not in [c["text"] for c in brief["clauses"]]
    assert brief["conflicts"] == [], "the user settled it, so it stops blocking"
    gate = ab.brief_gate(store, RUN, {
        "options": [{"key": "A", "satisfies": "goal.primary"}], "recommended": "A"},
        bound=True)
    assert gate["options"][0]["admissible"] is True


def test_replaying_the_whole_history_is_stable(store):
    """Assert on the compiled brief, not on one change at a time."""
    pipe = Pipeline(store)
    submit_intake(pipe, constraints=("台词用美式英语", "不得虚构购买经历"))
    ids = {c["text"]: c["id"] for c in ab.compile_active_brief(store, RUN)["clauses"]}
    _approve_clause(store, ids["台词用美式英语"], "台词用自然美式英语")
    second = next(c["id"] for c in ab.compile_active_brief(store, RUN)["clauses"]
                  if c["text"] == "台词用自然美式英语")
    _approve_clause(store, second, "台词用自然美式英语，语速偏快")
    _rewrite_intake(pipe, ["台词用自然美式英语，语速偏快", "不得虚构购买经历"])

    first_pass = ab.compile_active_brief(store, RUN)
    second_pass = ab.compile_active_brief(store, RUN)
    assert first_pass["clauses"] == second_pass["clauses"], "compiling twice is idempotent"
    assert first_pass["conflicts"] == []
    assert [c["text"] for c in first_pass["clauses"]].count("台词用自然美式英语，语速偏快") == 1
    assert "台词用自然美式英语" not in [c["text"] for c in first_pass["clauses"]]


# ------------------------------------------------------------------ A1 ----- #


def test_a_retried_amendment_request_records_one_decision(http_client):
    """The lost-response case, which is why the baseline guard used to bite.

    The first request is recorded and then its answer is lost. Retrying the same
    click with the same request_id must resolve to that amendment — not buy a
    second decision, and not answer 409 because the brief it named has already
    changed underneath it. Retrying is the moment the guard would otherwise
    fire, since the request's own effect is what moved the brief.
    """
    http, store, pipe = http_client["http"], http_client["store"], http_client["pipe"]
    http.post("/api/runs", json={"run_id": RUN, "title": "t", "brief": "做一个种草视频。"})
    submit_intake(pipe)
    brief = ab.compile_active_brief(store, RUN)
    body = {
        "changes": [{"clause_id": "goal.primary", "op": "replace",
                     "text": "目标收窄为设计细节种草"}],
        "approve": True,
        "expected_brief_fingerprint": ab.fingerprint(brief),
        "request_id": "click-7f3a",
    }

    first = http.post(f"/api/runs/{RUN}/brief-amendment", json=body)
    assert first.status_code == 200, first.text
    assert len(ab.load_amendments(store, RUN)) == 1

    # Same click again: the page never saw the first answer.
    second = http.post(f"/api/runs/{RUN}/brief-amendment", json=body)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"], "the same decision, not a new one"
    assert len(ab.load_amendments(store, RUN)) == 1, "nothing was written twice"
    assert second.json()["brief_fingerprint"] == first.json()["brief_fingerprint"]
    # The redo is resolved by the amendment id, so the retry looks up the job
    # instead of starting a second one.
    assert second.json()["redo"]["job_id"] == first.json()["redo"]["job_id"]
    assert len(_job_creations(store)) == 1


def test_the_same_content_without_a_request_id_is_a_second_decision(http_client):
    """The boundary the key exists for.

    Two identical decisions minutes apart are indistinguishable on the server,
    and the second one is legitimate — so deduplicating by content would be
    wrong. Only a caller-supplied key makes a retry recognisable.
    """
    http, store, pipe = http_client["http"], http_client["store"], http_client["pipe"]
    http.post("/api/runs", json={"run_id": RUN, "title": "t", "brief": "做一个种草视频。"})
    submit_intake(pipe)
    first = http.post(f"/api/runs/{RUN}/brief-amendment", json={
        "changes": [{"clause_id": "goal.primary", "op": "replace",
                     "text": "目标收窄为设计细节种草"}],
        "approve": True, "request_id": "click-1",
    })
    assert first.status_code == 200, first.text

    # A different key is a different click, even with identical content — and it
    # is refused, because the brief already says this. (The first click's redo
    # may also still be running, which is its own valid refusal; what matters is
    # that this request was treated as new rather than deduplicated.)
    second = http.post(f"/api/runs/{RUN}/brief-amendment", json={
        "changes": [{"clause_id": "goal.primary", "op": "replace",
                     "text": "目标收窄为设计细节种草"}],
        "approve": True, "request_id": "click-2",
    })
    assert second.status_code in (400, 409), second.text
    assert second.status_code != 200
    assert len(ab.load_amendments(store, RUN)) == 1, "no second amendment was written"

    # And with no key at all the route records a new decision, as documented:
    # idempotency here is a property of the key, not of the content.
    fresh = ab.apply_brief_amendment(
        store, RUN,
        changes=[{"clause_id": "goal.primary", "op": "replace",
                  "text": "目标改为保温实测对比"}],
        approve=False)
    assert fresh["id"] != first.json()["id"]
    assert len(ab.load_amendments(store, RUN)) == 2
    assert "request_id" not in fresh, "a keyless proposal stores no identity to match on"


# ------------------------------------------------------------------ F4 ----- #


def test_an_amendment_that_changes_nothing_is_refused(store):
    pipe = Pipeline(store)
    submit_intake(pipe)
    goal_text = ab.compile_active_brief(store, RUN)["goal"]["text"]
    with pytest.raises(ValueError) as err:
        ab.apply_brief_amendment(store, RUN, changes=[{
            "clause_id": "goal.primary", "op": "replace", "text": goal_text}])
    assert "没有实际变更" in str(err.value)
    assert ab.load_amendments(store, RUN) == [], "a no-op must not become a decision"


def test_an_amendment_against_a_clause_that_does_not_exist_is_refused(store):
    pipe = Pipeline(store)
    submit_intake(pipe)
    with pytest.raises(ValueError) as err:
        ab.apply_brief_amendment(store, RUN, changes=[{
            "clause_id": "must.deadbeef", "op": "replace", "text": "改点什么"}])
    assert "不在当前有效简报中" in str(err.value)
    assert ab.load_amendments(store, RUN) == []


def test_adding_a_clause_is_validated_the_other_way_round(store):
    """A2: `add` exists to introduce a clause the brief does not have yet.

    Requiring the target to exist rejected every legitimate addition, and an
    `add` that reused an existing id with a new body was silently dropped.
    """
    pipe = Pipeline(store)
    submit_intake(pipe)
    amendment = ab.apply_brief_amendment(store, RUN, changes=[{
        "clause_id": "must.a11ce001", "op": "add", "kind": "must",
        "text": "片中必须出现容量对比画面"}], approve=True)
    assert amendment["status"] == "approved"
    brief = ab.compile_active_brief(store, RUN)
    added = [c for c in brief["clauses"] if c["id"] == "must.a11ce001"]
    assert added and added[0]["text"] == "片中必须出现容量对比画面"
    assert added[0]["authority"] == "amendment"

    # A body the brief already carries is refused, not silently dropped.
    with pytest.raises(ValueError) as err:
        ab.apply_brief_amendment(store, RUN, changes=[{
            "clause_id": "must.b0b0b0b0", "op": "add", "text": "片中必须出现容量对比画面"}])
    assert "已在当前有效简报里" in str(err.value)


# ------------------------------------------------------------------ R2 ----- #


def test_an_approved_amendment_survives_a_rewritten_intake_clause(store):
    """The decision is about a requirement, not about a frozen string.

    Clause ids are derived from clause text, so a model rewrite used to change
    the id and make the replay silently skip: the record still said "approved"
    while the brief no longer carried what the user had approved.
    """
    pipe = Pipeline(store)
    submit_intake(pipe, constraints=("台词用美式英语",))
    original = next(c for c in ab.compile_active_brief(store, RUN)["clauses"]
                    if c["text"] == "台词用美式英语")

    ab.apply_brief_amendment(store, RUN, changes=[{
        "clause_id": original["id"], "op": "replace",
        "text": "台词用美式英语，且不得虚构购买经历",
    }], approve=True)
    after_approval = ab.compile_active_brief(store, RUN)
    assert any("不得虚构购买经历" in c["text"] for c in after_approval["clauses"])
    assert after_approval["conflicts"] == []

    # The model rewords the *same* requirement. The clause keeps its meaning, so
    # there is nothing to report — but the id moves, and the replay has to follow
    # it rather than drop the ban.
    attempt = pipe.start_attempt(RUN, "intake")
    pipe.submit(RUN, "intake", attempt.token, content="intake v2", summary="s2",
                structured={
                    "facts": [{"label": "交付形态", "value": "US 线时尚达人种草向带货视频",
                              "source": "u"}],
                    "constraints": ["台词用美式英语，且不得虚构购买经历"],
                    "open_questions": [],
                })
    recompiled = ab.compile_active_brief(store, RUN)
    banned = [c for c in recompiled["clauses"] if "不得虚构购买经历" in c["text"]]
    assert banned, "the approved ban must not disappear with the reworded clause"
    assert banned[0]["authority"] == "amendment"
    assert recompiled["conflicts"] == [], "the same requirement is not a conflict"


def test_an_amendment_that_cannot_be_realigned_keeps_the_decision_and_reports_it(store):
    """R2: when the clause is gone, keep what the user approved and say so.

    Silently dropping the decision left a brief that looked fine; silently
    resurrecting the model's text would claim the user approved something they
    never saw. Neither is acceptable, so the approved body stands under the id it
    was approved as, and the divergence is reported for the user to settle.
    """
    pipe = Pipeline(store)
    submit_intake(pipe, constraints=("台词用美式英语",))
    original = next(c for c in ab.compile_active_brief(store, RUN)["clauses"]
                    if c["text"] == "台词用美式英语")
    approved_text = "台词用美式英语，且不得虚构购买经历"
    ab.apply_brief_amendment(store, RUN, changes=[{
        "clause_id": original["id"], "op": "replace", "text": approved_text,
    }], approve=True)

    # The rewrite replaces the clause with a different requirement entirely.
    attempt = pipe.start_attempt(RUN, "intake")
    pipe.submit(RUN, "intake", attempt.token, content="intake v2", summary="s2",
                structured={
                    "facts": [{"label": "交付形态", "value": "US 线时尚达人种草向带货视频",
                              "source": "u"}],
                    "constraints": ["全片必须出现容量对比画面"],
                    "open_questions": [],
                })
    recompiled = ab.compile_active_brief(store, RUN)
    kept = [c for c in recompiled["clauses"] if c["text"] == approved_text]
    assert kept, "what the user approved is still in the brief"
    assert kept[0]["id"] == original["id"]
    assert kept[0]["authority"] == "amendment"

    assert recompiled["conflicts"], "the divergence must be reported"
    assert recompiled["conflicts"][0]["kind"] == "amendment_unresolved"
    assert original["id"] in recompiled["conflicts"][0]["detail"]

    # And the gate refuses to admit anything against such a brief.
    gate = ab.brief_gate(store, RUN, {
        "options": [{"key": "A", "satisfies": "goal.primary"}], "recommended": "A"},
        bound=True)
    assert gate["conflicts"], "the option projection carries the conflict"
    assert gate["options"][0]["admissible"] is False
    assert gate["status"] == "blocked"


def test_option_amend_proposal_never_falls_back_to_a_diagnosis():
    structured = {"options": [{"key": "A", "claim": "x", "amend_clause": "goal.primary"}]}
    assert ab.option_amend_proposal(structured, "A") == {
        "clause_id": "goal.primary", "text": ""}
    assert ab.option_amend_proposal(structured, "Z") == {"clause_id": "", "text": ""}
    assert ab.option_amend_proposal(None, "A") == {"clause_id": "", "text": ""}


def test_blocker_detail_is_a_diagnosis_and_proposed_text_is_the_proposal(store):
    """The gate keeps both, and they are different fields."""
    pipe, _ = _gated_direction(
        store,
        options=[{"key": "A", "violates": "goal.primary", "amend_clause": "goal.primary"}],
    )
    gate = pipe.stage_state(RUN, "direction").brief_gate
    needs = next(b for b in gate["blockers"] if b["kind"] == "needs_amendment")
    assert needs["proposed_text"] == "", "no proposal was written"
    assert "没有写出修订正文" in needs["detail"]
    assert needs["detail"] != needs["proposed_text"], "the diagnosis is not the body"


# ------------------------------------------------------------------ F5 ----- #


@pytest.fixture()
def http_client(tmp_path):
    box = {}

    def factory():
        return box.get("model") or ScriptedModel([final_turn("好。")])

    app = build_app(settings_for(tmp_path), client_factory=factory)
    box["app"] = app
    box["http"] = TestClient(app)
    box["store"] = Store(tmp_path)
    box["pipe"] = Pipeline(box["store"])
    box["tmp_path"] = tmp_path
    return box


def _blocked_direction(store, pipe):
    """A direction whose only option violates the goal but proposes a fix."""
    submit_intake(pipe)
    attempt = pipe.start_attempt(RUN, "direction")
    pipe.submit(RUN, "direction", attempt.token, content="方向正文", summary="方向",
                structured={
                    "options": [{
                        "key": "A", "claim": "设计细节演示",
                        "violates": "goal.primary",
                        "amend_clause": "goal.primary",
                        "amend_proposal": "目标收窄为设计细节种草，不要求出镜穿搭",
                    }],
                    "recommended": "A",
                })
    pipe.record_review(RUN, "direction", verdict="return", findings=[
        {"kind": "content", "issue": "未承接种草"}])
    return pipe.stage_state(RUN, "direction").latest


def _job_creations(store):
    return [row for row in store.read_events(RUN) if row.get("kind") == "job_created"]


def test_approving_an_amendment_starts_exactly_one_redo(http_client):
    http, store, pipe = http_client["http"], http_client["store"], http_client["pipe"]
    http.post("/api/runs", json={"run_id": RUN, "title": "t", "brief": "做一个种草视频。"})
    latest = _blocked_direction(store, pipe)

    body = {
        "changes": [{"clause_id": "goal.primary", "op": "replace",
                     "text": "目标收窄为设计细节种草，不要求出镜穿搭"}],
        "note": "门禁批准",
        "approve": True,
        "expected_artifact_sha256": latest.sha256,
        "expected_version": latest.version,
    }
    first = http.post(f"/api/runs/{RUN}/brief-amendment", json=body)
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["redo"]["status"] == "started"
    created = _job_creations(store)
    assert len(created) == 1
    assert created[0]["request_id"] == f"brief-amendment:{payload['id']}"

    # A lost response retried with the same approval must not buy a second job.
    second = http.post(f"/api/runs/{RUN}/brief-amendment", json=body)
    assert second.status_code in (200, 409)
    assert len(_job_creations(store)) == 1, "the request identity is derived from the amendment"

    # And the explicit recovery route resolves the same job, not a new one.
    again = http.post(f"/api/runs/{RUN}/brief-amendment/{payload['id']}/redo", json={})
    assert again.status_code == 200, again.text
    assert again.json()["job_id"] == created[0]["job_id"]
    assert len(_job_creations(store)) == 1


def test_the_redo_is_text_work_and_never_media(http_client):
    http, store, pipe = http_client["http"], http_client["store"], http_client["pipe"]
    http.post("/api/runs", json={"run_id": RUN, "title": "t", "brief": "做一个种草视频。"})
    latest = _blocked_direction(store, pipe)
    res = http.post(f"/api/runs/{RUN}/brief-amendment", json={
        "changes": [{"clause_id": "goal.primary", "op": "replace",
                     "text": "目标收窄为设计细节种草"}],
        "approve": True,
        "expected_artifact_sha256": latest.sha256,
        "expected_version": latest.version,
    })
    assert res.status_code == 200, res.text
    created = _job_creations(store)[0]
    assert created["job_kind"] == "conversation"
    assert "不" in created["text"] and "生成" in created["text"], created["text"]
    # Nothing about media ran: no production draft, no image or video attempt.
    kinds = {row.get("kind") for row in store.read_events(RUN)}
    assert not {"production_draft_saved", "image_submitted", "video_submitted"} & kinds


def test_a_redo_that_cannot_start_keeps_the_approval_and_offers_recovery(http_client, monkeypatch):
    http, store, pipe = http_client["http"], http_client["store"], http_client["pipe"]
    http.post("/api/runs", json={"run_id": RUN, "title": "t", "brief": "做一个种草视频。"})
    latest = _blocked_direction(store, pipe)

    from studio.jobs import JobManager

    real_submit = JobManager.submit

    def refuse(self, run_id, text, request_id, **kwargs):
        if str(request_id).startswith("brief-amendment:"):
            raise PipelineError("job_busy", "制片室正在运行另一个任务。")
        return real_submit(self, run_id, text, request_id, **kwargs)

    monkeypatch.setattr(JobManager, "submit", refuse)
    res = http.post(f"/api/runs/{RUN}/brief-amendment", json={
        "changes": [{"clause_id": "goal.primary", "op": "replace",
                     "text": "目标收窄为设计细节种草"}],
        "approve": True,
        "expected_artifact_sha256": latest.sha256,
        "expected_version": latest.version,
    })
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "approved", "the decision is saved either way"
    assert res.json()["redo"]["status"] == "not_started"
    assert "正在运行" in res.json()["redo"]["reason"]
    assert _job_creations(store) == []

    # The timeline exposes the recovery entry, keyed by the amendment.
    pending = http.get(f"/api/runs/{RUN}/timeline").json()["pending_brief_redo"]
    assert pending and pending["amendment_id"] == res.json()["id"]
    assert "还没有启动" in pending["message"]

    monkeypatch.setattr(JobManager, "submit", real_submit)
    recovered = http.post(f"/api/runs/{RUN}/brief-amendment/{pending['amendment_id']}/redo", json={})
    assert recovered.status_code == 200, recovered.text
    assert len(_job_creations(store)) == 1


def test_a_redo_cannot_be_started_for_an_unapproved_amendment(http_client):
    http, store, pipe = http_client["http"], http_client["store"], http_client["pipe"]
    http.post("/api/runs", json={"run_id": RUN, "title": "t", "brief": "做一个种草视频。"})
    submit_intake(pipe)
    pending = ab.propose_amendment(store, RUN, changes=[{
        "clause_id": "goal.primary", "op": "replace", "text": "先不动"}], approve=False)
    res = http.post(f"/api/runs/{RUN}/brief-amendment/{pending['id']}/redo", json={})
    assert res.status_code == 409
    assert _job_creations(store) == []


def test_the_amendment_route_refuses_a_no_op_before_writing_anything(http_client):
    http, store, pipe = http_client["http"], http_client["store"], http_client["pipe"]
    http.post("/api/runs", json={"run_id": RUN, "title": "t", "brief": "做一个种草视频。"})
    submit_intake(pipe)
    goal_text = ab.compile_active_brief(store, RUN)["goal"]["text"]
    res = http.post(f"/api/runs/{RUN}/brief-amendment", json={
        "changes": [{"clause_id": "goal.primary", "op": "replace", "text": goal_text}],
        "approve": True,
    })
    assert res.status_code == 400
    assert "没有实际变更" in res.text
    assert ab.load_amendments(store, RUN) == []
    assert _job_creations(store) == []


# ------------------------------------------------------------------ R4 ----- #


def test_a_stale_redo_stops_being_offered_once_newer_work_covers_it(http_client, monkeypatch):
    """R4: the recovery entry is work, and it has to stop being work when done.

    Three ways it stops being pending, each checked here: a later amendment
    replaces it as the brief's current intent, a direction draft already
    reflects the current brief, or its redo job exists.
    """
    from studio.active_brief import compile_active_brief, fingerprint as brief_fp
    from studio.jobs import JobManager

    http, store, pipe = http_client["http"], http_client["store"], http_client["pipe"]
    http.post("/api/runs", json={"run_id": RUN, "title": "t", "brief": "做一个种草视频。"})
    latest = _blocked_direction(store, pipe)

    real_submit = JobManager.submit

    def refuse(self, run_id, text, request_id, **kwargs):
        if str(request_id).startswith("brief-amendment:"):
            raise PipelineError("job_busy", "制片室正在运行另一个任务。")
        return real_submit(self, run_id, text, request_id, **kwargs)

    def approve(changes):
        # The direction guard is read fresh: a brief decision is about the brief,
        # and reading it while a newer direction is on screen is legitimate.
        res = http.post(f"/api/runs/{RUN}/brief-amendment", json={
            "changes": changes, "approve": True,
        })
        assert res.status_code == 200, res.text
        return res.json()

    def redo_entry():
        return http.get(f"/api/runs/{RUN}/timeline").json()["pending_brief_redo"]

    monkeypatch.setattr(JobManager, "submit", refuse)

    # (1) Neither redo started, so the newest amendment is genuinely pending.
    first = approve([{"clause_id": "goal.primary", "op": "replace",
                      "text": "目标收窄为设计细节种草"}])
    assert redo_entry()["amendment_id"] == first["id"]

    # (2) A later amendment replaces it as the brief's current intent.
    second = approve([{"clause_id": "goal.primary", "op": "replace",
                       "text": "目标收窄为设计细节种草，时长压到 8 秒"}])
    assert second["id"] != first["id"]
    assert redo_entry()["amendment_id"] == second["id"], "only the newest decision is pending work"

    # (3) An ordinary turn produces a direction that already matches the brief,
    #     so redoing it would spend a model call on a finished job.
    brief = compile_active_brief(store, RUN)
    attempt = pipe.start_attempt(RUN, "direction")
    pipe.submit(RUN, "direction", attempt.token, content="方向正文", summary="方向",
                structured={"options": [{"key": "A", "satisfies": "goal.primary"}],
                            "recommended": "A"})
    assert pipe.stage_state(RUN, "direction").latest.inputs["brief:snapshot"] == brief_fp(brief)
    assert redo_entry() is None, "the work this entry described has been done"

    # (4) Its own job existing is the third, independent reason.
    monkeypatch.setattr(JobManager, "submit", real_submit)
    third = approve([{"clause_id": "goal.primary", "op": "replace",
                      "text": "目标收窄为设计细节种草，时长压到 8 秒，保留容量对比"}])
    assert third["redo"]["status"] == "started"
    assert redo_entry() is None


# ------------------------------------------------------------------ U3 ----- #
# One submission identity stands for exactly one immutable payload. The id is
# only a *retry* token: it resolves to the recorded amendment when — and only
# when — the request still carries the payload that id was minted for.


def _u3_brief_fp(store):
    return ab.fingerprint(ab.compile_active_brief(store, RUN))


def _u3_post(http, *, request_id, text, fp, approve=True, note=None):
    body = {
        "changes": [{"clause_id": "goal.primary", "op": "replace", "text": text}],
        "approve": approve,
        "expected_brief_fingerprint": fp,
        "request_id": request_id,
    }
    if note is not None:
        body["note"] = note
    return http.post(f"/api/runs/{RUN}/brief-amendment", json=body)


def _u3_started(http_client):
    http, store, pipe = http_client["http"], http_client["store"], http_client["pipe"]
    http.post("/api/runs", json={"run_id": RUN, "title": "t", "brief": "做一个种草视频。"})
    submit_intake(pipe)
    return http, store, pipe


@pytest.mark.parametrize("field", ["text", "note", "approve", "brief"])
def test_a_changed_payload_under_the_same_request_id_is_a_conflict(http_client, field):
    """Same id, different payload: an explicit conflict, never the old success.

    Each field is one the decision actually turns on — the body, the user's
    note, whether it was approved, and the brief baseline it was framed
    against. Returning the recorded amendment for any of these would answer
    "yes" to a request the server never saw (U3).
    """
    http, store, pipe = _u3_started(http_client)
    fp = _u3_brief_fp(store)
    first = _u3_post(http, request_id="click-same", text="目标收窄为设计细节种草",
                     fp=fp, note="第一次")
    assert first.status_code == 200, first.text
    original = first.json()["id"]

    overrides = {
        "text": {"text": "改回真人出镜种草"},
        "note": {"note": "补充意见"},
        "approve": {"approve": False},
        "brief": {"fp": "brief-since-moved"},
    }[field]
    second = _u3_post(http, request_id="click-same",
                      text=overrides.get("text", "目标收窄为设计细节种草"),
                      fp=overrides.get("fp", fp),
                      note=overrides.get("note", "第一次"),
                      approve=overrides.get("approve", True))

    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert detail["reason"] == "request_conflict", detail
    rows = ab.load_amendments(store, RUN)
    assert len(rows) == 1, "the conflict wrote nothing"
    assert rows[0]["id"] == original, "and did not swap the recorded decision"
    assert ab.compile_active_brief(store, RUN)["goal"]["text"] == "目标收窄为设计细节种草", \
        "the old decision stands; the new payload never executed"


def test_a_pending_record_does_not_gain_approval_from_a_retry(http_client):
    """The retry's approve flag cannot promote what the record never was.

    A propose-only submission is pending. Re-sending it with approve=true is a
    *different* payload — it conflicts; it must not start the redo and must not
    read as approved (U3).
    """
    http, store, pipe = _u3_started(http_client)
    fp = _u3_brief_fp(store)
    first = _u3_post(http, request_id="click-pending", text="目标收窄为设计细节种草",
                     fp=fp, approve=False)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "pending"
    assert _job_creations(store) == []

    promoted = _u3_post(http, request_id="click-pending", text="目标收窄为设计细节种草",
                        fp=fp, approve=True)
    assert promoted.status_code == 409, promoted.text
    assert promoted.json()["detail"]["reason"] == "request_conflict"
    rows = ab.load_amendments(store, RUN)
    assert rows[0]["status"] == "pending", "the record kept what it actually is"
    assert _job_creations(store) == [], "an unapproved record owes no redo"


def _u3_refuse_redo(monkeypatch):
    """Silence the background redo so assertions race no running job."""
    from studio.jobs import JobManager
    from studio.models import PipelineError

    real_submit = JobManager.submit

    def refuse(self, run_id, text, request_id, **kwargs):
        if str(request_id).startswith("brief-amendment:"):
            raise PipelineError("job_busy", "制片室正在运行另一个任务。")
        return real_submit(self, run_id, text, request_id, **kwargs)

    monkeypatch.setattr(JobManager, "submit", refuse)


def test_the_lost_response_scenario_new_proposal_applies_under_its_own_identity(http_client, monkeypatch):
    """The full U3 flow: B recorded, response lost, C proposed after a refresh.

    C carries the same click id the page still holds — the server must refuse
    that id with an explicit conflict (it names a different decision), keep B,
    and accept C once the page mints a fresh identity for it.
    """
    _u3_refuse_redo(monkeypatch)
    http, store, pipe = _u3_started(http_client)
    fp_before = _u3_brief_fp(store)
    first = _u3_post(http, request_id="click-lost", text="只拍设计细节种草", fp=fp_before)
    assert first.status_code == 200, first.text
    amd_b = first.json()["id"]
    assert len(ab.load_amendments(store, RUN)) == 1

    # The page never saw that answer. Refreshed, it now proposes C — same id
    # still in localStorage, but a different body and a different brief.
    fp_after = _u3_brief_fp(store)
    assert fp_after != fp_before, "approving B moved the brief"
    second = _u3_post(http, request_id="click-lost", text="改回真人出镜种草", fp=fp_after)
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["reason"] == "request_conflict"
    rows = ab.load_amendments(store, RUN)
    assert len(rows) == 1 and rows[0]["id"] == amd_b, "B is untouched; C did not execute"
    assert ab.compile_active_brief(store, RUN)["goal"]["text"] == "只拍设计细节种草"

    # With its own identity, the same decision C is a legitimate new amendment.
    third = _u3_post(http, request_id="click-fresh", text="改回真人出镜种草", fp=fp_after)
    assert third.status_code == 200, third.text
    assert third.json()["id"] != amd_b
    assert len(ab.load_amendments(store, RUN)) == 2
    assert ab.compile_active_brief(store, RUN)["goal"]["text"] == "改回真人出镜种草"
    assert third.json()["redo"]["status"] == "not_started", "the decision is saved either way"


def test_a_legacy_row_without_a_frozen_payload_still_checks_what_it_kept(http_client):
    """Compatibility for rows written before payloads were frozen.

    Such a row carries the changes and the note — that much can still be
    compared. Fields it never stored are not claimed as verified; a retry that
    *disagrees* on what IS stored still conflicts, instead of resolving to a
    success it cannot vouch for.
    """
    http, store, pipe = _u3_started(http_client)
    fp = _u3_brief_fp(store)
    first = _u3_post(http, request_id="click-legacy", text="目标收窄为设计细节种草",
                     fp=fp, note="第一次")
    assert first.status_code == 200, first.text

    # Simulate a pre-fix row: no frozen request payload on it.
    rows = ab.load_amendments(store, RUN)
    assert rows[0].pop("request_fingerprint"), "new rows freeze their payload"
    ab._save_amendments(store, RUN, rows)

    same = _u3_post(http, request_id="click-legacy", text="目标收窄为设计细节种草",
                    fp=fp, note="第一次")
    assert same.status_code == 200, same.text
    assert same.json()["id"] == first.json()["id"]

    different = _u3_post(http, request_id="click-legacy", text="改回真人出镜种草",
                         fp=fp, note="第一次")
    assert different.status_code == 409, different.text
    assert different.json()["detail"]["reason"] == "request_conflict"
    assert len(ab.load_amendments(store, RUN)) == 1


def test_the_locked_retry_scan_resolves_only_identical_payloads(store):
    """The in-lock path (HTTP's early scan can miss a concurrent twin).

    The busy lock serialises writers, so the authoritative check is the scan
    inside ``apply_brief_amendment``: the second arrival of the *same* request
    resolves to one amendment; the same id wearing a different payload is
    refused there too — one identity, one payload, whoever wins the race.
    """
    pipe = Pipeline(store)
    submit_intake(pipe)
    fp = _u3_brief_fp(store)
    changes = [{"clause_id": "goal.primary", "op": "replace",
                "text": "目标收窄为设计细节种草"}]

    first = ab.apply_brief_amendment(store, RUN, changes=changes, approve=True,
                                     expected_brief_fingerprint=fp, request_id="race-1")
    same = ab.apply_brief_amendment(store, RUN, changes=changes, approve=True,
                                    expected_brief_fingerprint=fp, request_id="race-1")
    assert same["id"] == first["id"]
    assert len(ab.load_amendments(store, RUN)) == 1

    with pytest.raises(ab.BriefRequestConflict):
        ab.apply_brief_amendment(
            store, RUN,
            changes=[{"clause_id": "goal.primary", "op": "replace",
                      "text": "改回真人出镜种草"}],
            approve=True, expected_brief_fingerprint=fp, request_id="race-1")
    assert len(ab.load_amendments(store, RUN)) == 1, "the refused twin wrote nothing"
    assert ab.compile_active_brief(store, RUN)["goal"]["text"] == "目标收窄为设计细节种草"


def test_a_new_identity_applies_a_new_decision_without_body_dedup(http_client, monkeypatch):
    """New id, legitimate new decision: normal validation, no old success.

    Two different decisions minutes apart are indistinguishable by content and
    both are legitimate — the id is what distinguishes a retry, and without
    one this route has never promised idempotency (kept deliberate).
    """
    _u3_refuse_redo(monkeypatch)
    http, store, pipe = _u3_started(http_client)
    fp0 = _u3_brief_fp(store)
    first = _u3_post(http, request_id="click-a", text="目标收窄为设计细节种草", fp=fp0)
    assert first.status_code == 200, first.text
    fp1 = _u3_brief_fp(store)
    second = _u3_post(http, request_id="click-b", text="改回真人出镜种草", fp=fp1)
    assert second.status_code == 200, second.text
    assert second.json()["id"] != first.json()["id"]
    assert len(ab.load_amendments(store, RUN)) == 2
    assert ab.compile_active_brief(store, RUN)["goal"]["text"] == "改回真人出镜种草"
