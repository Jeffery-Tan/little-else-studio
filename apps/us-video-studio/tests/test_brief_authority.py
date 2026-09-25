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


# ------------------------------------------------------------------ F4 ----- #


def test_an_amendment_that_changes_nothing_is_refused(store):
    pipe = Pipeline(store)
    submit_intake(pipe)
    goal_text = ab.compile_active_brief(store, RUN)["goal"]["text"]
    with pytest.raises(ValueError) as err:
        ab.propose_amendment(store, RUN, changes=[{
            "clause_id": "goal.primary", "op": "replace", "text": goal_text}])
    assert "没有实际变更" in str(err.value)
    assert ab.load_amendments(store, RUN) == [], "a no-op must not become a decision"


def test_an_amendment_against_a_clause_that_does_not_exist_is_refused(store):
    pipe = Pipeline(store)
    submit_intake(pipe)
    with pytest.raises(ValueError) as err:
        ab.propose_amendment(store, RUN, changes=[{
            "clause_id": "must.deadbeef", "op": "replace", "text": "改点什么"}])
    assert "不在当前有效简报中" in str(err.value)
    assert ab.load_amendments(store, RUN) == []


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
