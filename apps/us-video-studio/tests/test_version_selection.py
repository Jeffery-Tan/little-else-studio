"""Exact displayed versions, real direction selection, and immutable history."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from fakes import ScriptedModel, final_turn, tool_turn
from studio.agents.reviewer import run_review
from studio.config import Settings
from studio.ledger import Ledger
from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.store import Store, sha256_text
from studio.tools.workspace import Workspace

RUN = "version-case"
OPTIONS = {"options": [{"key": "A", "claim": "Spoken idea", "satisfies": "goal.primary"},
                       {"key": "B", "claim": "Quiet idea", "satisfies": "goal.primary"}],
           "recommended": "A"}


@pytest.fixture
def pipe(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, "Version case", "Factful brief")
    return Pipeline(store)


def submit(pipe, stage, *, content=None, structured=None):
    attempt = pipe.start_attempt(RUN, stage)
    return pipe.submit(RUN, stage, attempt.token, content=content or f"{stage} same body",
                       summary=f"{stage} summary", structured=structured)


def direction(pipe):
    submit(pipe, "intake", content="UNIQUE-INTAKE-FACT; shipping is unverified")
    art = submit(pipe, "direction", structured=OPTIONS)
    pipe.record_review(RUN, "direction", verdict="pass", findings=[])
    return art


def approve(pipe, option="B", nonce=None):
    return pipe.record_approval(RUN, "direction", decision="approved",
                                nonce=nonce or pipe.issue_nonce(RUN, "direction"),
                                selected_option_id=option)


def test_old_nonce_cannot_approve_a_new_version(pipe):
    old = direction(pipe)
    nonce = pipe.issue_nonce(RUN, "direction", expected_version=old.version,
                             expected_artifact_sha256=old.sha256)
    new = submit(pipe, "direction", content="new body", structured=OPTIONS)
    pipe.record_review(RUN, "direction", verdict="pass", findings=[])
    with pytest.raises(PipelineError):
        approve(pipe, nonce=nonce)
    with pytest.raises(PipelineError, match="version_conflict"):
        pipe.issue_nonce(RUN, "direction", expected_version=old.version,
                         expected_artifact_sha256=old.sha256)
    assert pipe.stage_state(RUN, "direction").latest == new
    assert pipe.store.list_approvals(RUN) == []


def test_crlf_body_verifies_without_normalizing_stored_bytes(pipe):
    art = submit(pipe, "intake", content="first\r\nsecond\r\n")
    assert pipe.store.verify_artifact(RUN, "intake", art.version)
    assert pipe.store.read_artifact_text(RUN, "intake", art.version) == "first\r\nsecond\r\n"


def test_structured_only_change_invalidates_review_approval_and_downstream(pipe):
    old = direction(pipe)
    approve(pipe)
    submit(pipe, "material_plan")
    changed = {**OPTIONS, "recommended": "B"}
    new = submit(pipe, "direction", structured=changed)
    assert old.sha256 != new.sha256
    state = pipe.stage_state(RUN, "direction")
    assert state.review is None and state.approval is None
    assert pipe.stage_state(RUN, "material_plan").stale
    assert pipe.store.read_artifact(RUN, "direction", old.version).structured == old.structured


@pytest.mark.parametrize("field,value", [("summary", "different summary"),
                                        ("evidence", ["new unsupported claim"]),
                                        ("limitations", []),
                                        ("structured", {"recommended": "B"}),
                                        ("inputs", {"intake": "invented"})])
def test_sidecar_tampering_is_detected(pipe, field, value):
    art = direction(pipe)
    meta = pipe.store.run_dir(RUN) / "artifacts" / f"direction-v{art.version}.json"
    record = json.loads(meta.read_text(encoding="utf-8"))
    if value == record.get(field):
        value = ["changed"]
    record[field] = value
    meta.write_text(json.dumps(record), encoding="utf-8")
    assert not pipe.store.verify_artifact(RUN, "direction", art.version)
    assert not pipe.stage_state(RUN, "direction").can_approve


def test_choice_b_is_consumed_and_second_approval_is_refused(pipe):
    art = direction(pipe)
    decision = approve(pipe)
    assert decision.selected_option_id == "B" and decision.artifact_version == art.version
    attempt = pipe.start_attempt(RUN, "material_plan")
    assert attempt.inputs["direction"] == art.sha256
    assert attempt.inputs["direction:selection"]
    assert attempt.inputs["intake"]
    context = Workspace(pipe.store, pipe, RUN, "material").render_context("material_plan")
    assert "Quiet idea" in context and "B" in context
    material = pipe.submit(RUN, "material_plan", attempt.token, content="Plan for B", summary="B")
    writing = pipe.start_attempt(RUN, "writing")
    assert writing.inputs["direction:selection"] == material.inputs["direction:selection"]
    assert writing.inputs["intake"] == material.inputs["intake"]
    # One version, one decision: A cannot replace B on the same artifact.
    with pytest.raises(PipelineError, match="already has decision"):
        approve(pipe, option="A")
    assert len(pipe.store.list_approvals(RUN)) == 1
    assert pipe.stage_state(RUN, "direction").approval.selected_option_id == "B"
    assert not pipe.stage_state(RUN, "material_plan").stale


def test_selection_requires_explicit_unique_existing_option(pipe):
    direction(pipe)
    for option in (None, "C", ""):
        with pytest.raises(PipelineError, match="option"):
            approve(pipe, option=option)
    assert not pipe.store.list_approvals(RUN)


def test_returned_selection_and_tampered_upstream_invalidate_downstream(pipe):
    direction(pipe)
    # Return first (no prior approval), then dependents only after a later approve.
    returned = pipe.record_approval(RUN, "direction", nonce=pipe.issue_nonce(RUN, "direction"),
                                    decision="returned", note="change the ending")
    assert returned.decision == "returned"
    # Returned decision is not a completion: dependents cannot proceed.
    with pytest.raises(PipelineError, match="not complete"):
        pipe.start_attempt(RUN, "material_plan")
    # Produce a new direction version and approve it.
    submit(pipe, "direction", content="direction after return", structured=OPTIONS)
    pipe.record_review(RUN, "direction", verdict="pass", findings=[])
    approve(pipe)
    submit(pipe, "material_plan")
    writing = submit(pipe, "writing")
    pipe.record_review(RUN, "writing", verdict="pass", findings=[])
    # Writing can be approved while direction is approved and complete.
    nonce = pipe.issue_nonce(RUN, "writing")
    approval = pipe.record_approval(RUN, "writing", nonce=nonce, decision="approved")
    assert approval.decision == "approved"
    # Approved writing cannot be re-approved or returned on the same version.
    with pytest.raises(PipelineError, match="already has decision"):
        pipe.issue_nonce(RUN, "writing")
    assert not pipe.stage_state(RUN, "writing").stale
    # Tampered upstream intake still invalidates writing.
    intake = pipe.store.run_dir(RUN) / "artifacts" / "intake-v1.md"
    intake.write_text("tampered facts", encoding="utf-8")
    assert pipe.stage_state(RUN, "writing").stale
    intake = pipe.store.run_dir(RUN) / "artifacts" / "intake-v1.md"
    intake.write_text("tampered facts", encoding="utf-8")
    assert pipe.stage_state(RUN, "writing").stale
    assert not pipe.stage_state(RUN, "writing").can_approve


def test_same_nonce_retry_is_idempotent_but_cannot_change_selection(pipe):
    direction(pipe)
    nonce = pipe.issue_nonce(RUN, "direction")
    first = approve(pipe, nonce=nonce)
    assert approve(pipe, nonce=nonce) == first
    assert len(pipe.store.list_approvals(RUN)) == 1
    with pytest.raises(PipelineError, match="different decision"):
        approve(pipe, option="A", nonce=nonce)


def test_second_approval_cannot_overwrite_selected_option(pipe):
    """One version, one decision: a fresh nonce cannot swap A for B."""
    direction(pipe)
    first = approve(pipe, option="B")
    assert first.selected_option_id == "B"
    with pytest.raises(PipelineError, match="already has decision"):
        pipe.issue_nonce(RUN, "direction")
    with pytest.raises(PipelineError, match="already has decision"):
        approve(pipe, option="A")
    assert len(pipe.store.list_approvals(RUN)) == 1
    assert pipe.stage_state(RUN, "direction").approval.selected_option_id == "B"


def test_his_own_return_also_closes_the_gate(pipe):
    """One version, one decision — his return counts as the decision too.

    The projection the screen reads (``can_approve``, which becomes
    ``_pending_action``) has to agree with the server rule in ``issue_nonce``.
    Otherwise the gate keeps drawing an approve button it has just refused, and
    the pending card goes on saying he may still decide this version.
    """
    direction(pipe)
    returned = pipe.record_approval(RUN, "direction", nonce=pipe.issue_nonce(RUN, "direction"),
                                    decision="returned", note="开场先换掉")
    assert returned.decision == "returned"
    state = pipe.stage_state(RUN, "direction")
    assert state.approval.decision == "returned"
    assert state.can_approve is False
    assert not any(s.can_approve for s in pipe.state(RUN).stages.values())
    with pytest.raises(PipelineError, match="already has decision"):
        pipe.issue_nonce(RUN, "direction")


def test_historical_reviewer_return_allows_plain_user_approval(pipe):
    """A retired reviewer verdict does not add an override decision type."""
    submit(pipe, "intake", content="facts")
    submit(pipe, "direction", structured=OPTIONS)
    pipe.record_review(RUN, "direction", verdict="return",
                       findings=[{"stage": "direction", "issue": "没有证据支持这个钩子"}])
    state = pipe.stage_state(RUN, "direction")
    assert state.approval is None
    assert state.can_approve is True

    approval = pipe.record_approval(
        RUN, "direction", nonce=pipe.issue_nonce(RUN, "direction"),
        decision="approved", selected_option_id="A", override_reason="",
    )
    assert approval.decision == "approved"


def test_concurrent_same_nonce_records_one_decision_across_pipeline_instances(pipe):
    direction(pipe)
    nonce = pipe.issue_nonce(RUN, "direction")
    other = Pipeline(Store(pipe.store.root))
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda p: approve(p, nonce=nonce), (pipe, other)))
    assert results[0] == results[1]
    assert len(pipe.store.list_approvals(RUN)) == 1


def test_lost_event_after_approval_write_retries_without_duplicate(pipe, monkeypatch):
    direction(pipe)
    nonce = pipe.issue_nonce(RUN, "direction")
    original = pipe.store.append_event

    def fail_after_decision(run_id, kind, **fields):
        if kind == "stage_decided":
            raise OSError("synthetic event write failure")
        return original(run_id, kind, **fields)

    monkeypatch.setattr(pipe.store, "append_event", fail_after_decision)
    with pytest.raises(OSError):
        approve(pipe, nonce=nonce)
    assert len(pipe.store.list_approvals(RUN)) == 1
    assert approve(pipe, nonce=nonce).selected_option_id == "B"
    assert len(pipe.store.list_approvals(RUN)) == 1


def test_legacy_digest_can_be_read_without_modifying_any_bytes(pipe):
    old = direction(pipe)
    meta = pipe.store.run_dir(RUN) / "artifacts" / f"direction-v{old.version}.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data.pop("digest_algorithm")
    data["sha256"] = sha256_text(pipe.store.read_artifact_text(RUN, "direction", old.version))
    meta.write_text(json.dumps(data), encoding="utf-8")
    before = meta.read_bytes()
    assert pipe.store.read_artifact(RUN, "direction", old.version).digest_algorithm == "body-v1"
    assert pipe.store.verify_artifact(RUN, "direction", old.version)
    pipe.state(RUN)
    assert before == meta.read_bytes()
    with pytest.raises(PipelineError, match="legacy"):
        pipe.issue_nonce(RUN, "direction")


def test_same_named_material_bytes_change_invalidates_input_and_open_attempt(pipe):
    base = pipe.store.run_dir(RUN) / "materials"
    base.mkdir()
    image = base / "photo.jpg"
    image.write_bytes(b"synthetic source one")
    submit(pipe, "intake")
    attempt = pipe.start_attempt(RUN, "direction")
    image.write_bytes(b"synthetic source two")
    assert pipe.stage_state(RUN, "intake").stale
    with pytest.raises(PipelineError):
        pipe.submit(RUN, "direction", attempt.token, content="old source", summary="old")


def test_reviewer_cannot_pass_new_version_it_never_saw(pipe, tmp_path):
    submit(pipe, "intake")
    original = submit(pipe, "direction", structured=OPTIONS)

    class RacingModel(ScriptedModel):
        def complete(self, **kwargs):
            if not self.calls:
                submit(pipe, "direction", content="UNSEEN NEW VERSION", structured=OPTIONS)
            return super().complete(**kwargs)

    model = RacingModel([tool_turn("r1", "finish_review", {"verdict": "pass", "findings": []}),
                         final_turn("finished")])
    settings = Settings(data_root=tmp_path, base_url="https://example.invalid")
    outcome = run_review(store=pipe.store, pipeline=pipe, settings=settings,
                         client=model, ledger=Ledger(pipe.store, settings), run_id=RUN,
                         stage="direction")
    assert outcome.verdict is None
    assert not pipe.store.list_reviews(RUN)
    assert "UNSEEN NEW VERSION" not in model.calls[0]["messages"][0]["content"]
    assert pipe.stage_state(RUN, "direction").latest.version > original.version
