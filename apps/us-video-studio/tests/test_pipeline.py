"""Pipeline rules.

Each test here stands for a rule that must hold even when an agent gets
confused, because a prompt cannot enforce any of them.
"""
from __future__ import annotations

import pytest

from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.store import Store

RUN = "lunch-bag-01"


@pytest.fixture()
def pipe(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, "Lunch bag ad", "brief")
    return Pipeline(store)


def submit_intake(pipe, text="intake body v1"):
    a = pipe.start_attempt(RUN, "intake")
    return pipe.submit(RUN, "intake", a.token, content=text, summary="intake done")


def submit_direction(pipe, text="direction body v1"):
    a = pipe.start_attempt(RUN, "direction")
    return pipe.submit(RUN, "direction", a.token, content=text, summary="direction done")


def review_pass(pipe, stage):
    return pipe.record_review(RUN, stage, verdict="pass", findings=[])


def approve(pipe, stage, override_reason=""):
    nonce = pipe.issue_nonce(RUN, stage)
    return pipe.record_approval(
        RUN, stage, nonce=nonce, decision="approved", override_reason=override_reason
    )


# --------------------------------------------------------------- ordering --


def test_cannot_start_direction_before_intake_exists(pipe):
    with pytest.raises(PipelineError) as err:
        pipe.start_attempt(RUN, "direction")
    assert err.value.rule == "prereq"
    assert "intake" in str(err.value)


def test_intake_opens_direction(pipe):
    submit_intake(pipe)
    attempt = pipe.start_attempt(RUN, "direction")
    assert attempt.role == "director"


def test_gated_stage_blocks_the_next_stage_until_the_user_approves(pipe):
    submit_intake(pipe)
    submit_direction(pipe)
    review_pass(pipe, "direction")

    with pytest.raises(PipelineError) as err:
        pipe.start_attempt(RUN, "material_plan")
    assert err.value.rule == "prereq"

    approve(pipe, "direction")
    assert pipe.start_attempt(RUN, "material_plan").role == "material"


def test_critical_material_gap_blocks_full_script_until_plan_is_revised(pipe):
    submit_intake(pipe)
    submit_direction(pipe)
    review_pass(pipe, "direction")
    approve(pipe, "direction")
    attempt = pipe.start_attempt(RUN, 'material_plan')
    pipe.submit(RUN, 'material_plan', attempt.token, content='', summary='Missing proof',
                structured={'items': [{'shot': 'Core proof', 'status': '无法获得'}],
                            'gaps': [{'gap': 'Core proof', 'consequence': 'Claim unsupported',
                                      'blocking': '阻塞'}]})
    state = pipe.stage_state(RUN, 'material_plan')
    assert state.status == 'needs_resolution'
    assert state.can_start
    assert not pipe.is_complete(RUN, 'material_plan')
    with pytest.raises(PipelineError, match='upstream'):
        pipe.start_attempt(RUN, 'writing')

    revision = pipe.start_attempt(RUN, 'material_plan')
    pipe.submit(RUN, 'material_plan', revision.token, content='', summary='Alternative shot',
                structured={'items': [{'shot': 'Alternative proof', 'status': '已有'}],
                            'gaps': [{'gap': 'Polish', 'consequence': 'Can refine later',
                                      'blocking': '可后补'}]})
    assert pipe.is_complete(RUN, 'material_plan')
    assert pipe.start_attempt(RUN, 'writing').role == 'writer'


def test_recent_activity_does_not_open_a_stage_by_itself(pipe):
    """Direction being written is not direction being accepted."""
    submit_intake(pipe)
    submit_direction(pipe)
    with pytest.raises(PipelineError):
        pipe.start_attempt(RUN, "material_plan")


def test_only_one_open_attempt_per_stage(pipe):
    submit_intake(pipe)
    pipe.start_attempt(RUN, "direction")
    with pytest.raises(PipelineError) as err:
        pipe.start_attempt(RUN, "direction")
    assert err.value.rule == "attempt"


def test_a_rejected_attempt_cannot_submit_late(pipe):
    submit_intake(pipe)
    attempt = pipe.start_attempt(RUN, "direction")
    pipe.reject(RUN, "direction", attempt.token, "no product evidence cited")

    with pytest.raises(PipelineError) as err:
        pipe.submit(RUN, "direction", attempt.token, content="late", summary="late")
    assert "rejected" in str(err.value)

    # A fresh attempt is the way back in, and it is numbered, not silent.
    again = pipe.start_attempt(RUN, "direction")
    assert again.number == 2


def test_rejection_needs_a_reason(pipe):
    submit_intake(pipe)
    attempt = pipe.start_attempt(RUN, "direction")
    with pytest.raises(PipelineError):
        pipe.reject(RUN, "direction", attempt.token, "   ")


def test_submitting_with_a_made_up_token_fails(pipe):
    submit_intake(pipe)
    pipe.start_attempt(RUN, "direction")
    with pytest.raises(PipelineError) as err:
        pipe.submit(RUN, "direction", "guessed-token", content="x", summary="x")
    assert err.value.rule == "attempt"


# --------------------------------------------------------------- approvals --


def test_approval_without_a_valid_nonce_is_refused(pipe):
    submit_intake(pipe)
    submit_direction(pipe)
    review_pass(pipe, "direction")

    for bad in ("", "made-up", "0" * 40):
        with pytest.raises(PipelineError) as err:
            pipe.record_approval(RUN, "direction", nonce=bad, decision="approved")
        assert err.value.rule == "approval"


def test_nonce_is_single_use(pipe):
    submit_intake(pipe)
    submit_direction(pipe)
    review_pass(pipe, "direction")

    nonce = pipe.issue_nonce(RUN, "direction")
    first = pipe.record_approval(RUN, "direction", nonce=nonce, decision="approved")
    assert pipe.record_approval(RUN, "direction", nonce=nonce, decision="approved") == first
    with pytest.raises(PipelineError):
        pipe.record_approval(RUN, "direction", nonce=nonce, decision="returned", note="different decision")


def test_same_version_cannot_be_approved_twice(pipe):
    """A second nonce must not re-decide one already-approved artifact."""
    submit_intake(pipe)
    submit_direction(pipe)
    review_pass(pipe, "direction")

    first = approve(pipe, "direction")
    assert first.decision == "approved"
    with pytest.raises(PipelineError) as err:
        pipe.issue_nonce(RUN, "direction")
    assert "already has decision" in str(err.value)
    with pytest.raises(PipelineError) as err:
        approve(pipe, "direction")
    assert "already has decision" in str(err.value) or "not valid" in str(err.value)
    state = pipe.stage_state(RUN, "direction")
    assert state.approval is not None
    assert state.approval.decision == first.decision
    assert state.approval.nonce_sha256 == first.nonce_sha256


def test_nonce_is_bound_to_its_stage(pipe):
    submit_intake(pipe)
    submit_direction(pipe)
    review_pass(pipe, "direction")

    nonce = pipe.issue_nonce(RUN, "direction")
    # writing is the other gated stage; a direction nonce must not work there.
    with pytest.raises(PipelineError):
        pipe.record_approval(RUN, "writing", nonce=nonce, decision="approved")


def test_plaintext_nonce_is_never_written_to_disk(pipe, tmp_path):
    """The run directory is readable by the agents. The nonce must not be in it.

    Only its hash is persisted, so an agent that reads every file still cannot
    produce a click.
    """
    submit_intake(pipe)
    submit_direction(pipe)
    review_pass(pipe, "direction")
    nonce = pipe.issue_nonce(RUN, "direction")

    blob = "".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in tmp_path.rglob("*")
        if p.is_file()
    )
    assert nonce not in blob
    assert "nonce_sha256" in blob


def test_unreviewed_draft_can_be_approved_without_an_override(pipe):
    submit_intake(pipe)
    submit_direction(pipe)
    assert pipe.stage_state(RUN, "direction").can_approve
    approval = approve(pipe, "direction")
    assert approval.decision == "approved"
    assert pipe.is_complete(RUN, "direction")
    assert not pipe.stage_state(RUN, "direction").can_review
    with pytest.raises(PipelineError, match="already has a user decision"):
        review_pass(pipe, "direction")


def test_returning_a_stage_needs_a_note_the_writer_can_act_on(pipe):
    """The mirror of the override rule next door, and the same kind of rule.

    A return goes to the agent that wrote the document, which acts on this text
    and nothing else. An empty one leaves it to guess or to redo the work
    unchanged — a wrong fix or a wasted turn, both of them paid for. Gitea and
    Forgejo refuse this server-side for the same reason.
    """
    submit_intake(pipe)
    submit_direction(pipe)
    review_pass(pipe, "direction")

    nonce = pipe.issue_nonce(RUN, "direction")
    with pytest.raises(PipelineError) as err:
        pipe.record_approval(RUN, "direction", nonce=nonce, decision="returned")
    assert "note" in str(err.value)

    # Whitespace is not a note either — a space is what a required field gets
    # typed into when the field is the only thing in the way.
    nonce = pipe.issue_nonce(RUN, "direction")
    with pytest.raises(PipelineError):
        pipe.record_approval(RUN, "direction", nonce=nonce, decision="returned", note="   ")

    nonce = pipe.issue_nonce(RUN, "direction")
    approval = pipe.record_approval(
        RUN, "direction", nonce=nonce, decision="returned", note="开场缺了容量对比"
    )
    assert approval.decision == "returned"
    assert approval.note == "开场缺了容量对比"
    # One decision per version: return freezes this artifact; a new version
    # is required before another decision (CLAUDE.md red line 8).
    with pytest.raises(PipelineError) as err:
        pipe.issue_nonce(RUN, "direction")
    assert "already has decision" in str(err.value)
    with pytest.raises(PipelineError) as err:
        approve(pipe, "direction")
    assert "already has decision" in str(err.value) or "not valid" in str(err.value)


@pytest.mark.parametrize("continue_after", [False, True])
def test_approval_cannot_carry_unresolved_changes(pipe, continue_after):
    submit_intake(pipe)
    submit_direction(pipe)
    review_pass(pipe, "direction")
    nonce = pipe.issue_nonce(RUN, "direction")

    with pytest.raises(PipelineError, match="cannot include requested changes"):
        pipe.record_approval(
            RUN, "direction", nonce=nonce, decision="approved",
            note="选 A，但改开头", continue_after=continue_after,
        )

    # A rejected payload has not used the nonce or approved the old version.
    approval = pipe.record_approval(
        RUN, "direction", nonce=nonce, decision="returned",
        note="参考 A，但先修改开头", continue_after=True,
    )
    assert approval.decision == "returned"


def test_historical_reviewer_return_does_not_block_user_decision(pipe):
    submit_intake(pipe)
    submit_direction(pipe)
    pipe.record_review(
        RUN,
        "direction",
        verdict="return",
        findings=[{"stage": "direction", "issue": "no evidence for the hook claim"}],
    )
    state = pipe.stage_state(RUN, "direction")
    assert state.can_approve
    assert not state.can_review
    assert state.status == "awaiting_decision"
    assert state.review.verdict == "return"
    assert approve(pipe, "direction").decision == "approved"
    pipe.start_attempt(RUN, "material_plan")


def test_a_return_needs_at_least_one_finding(pipe):
    submit_intake(pipe)
    submit_direction(pipe)
    with pytest.raises(PipelineError):
        pipe.record_review(RUN, "direction", verdict="return", findings=[])


# ----------------------------------------------------------- invalidation --


def test_revising_a_gated_stage_voids_its_own_approval_and_closes_the_next(pipe):
    submit_intake(pipe)
    submit_direction(pipe)
    review_pass(pipe, "direction")
    approve(pipe, "direction")
    pipe.start_attempt(RUN, "material_plan")

    # Direction gets rewritten. The new version has never been approved.
    state = pipe.state(RUN)
    assert state.stages["direction"].status == "approved"

    revised = submit_direction(pipe, "direction body v2")

    state = pipe.state(RUN)
    assert state.stages["direction"].status == "awaiting_decision"
    assert state.stages["direction"].approval is None
    assert revised.version == 2


def test_revising_upstream_invalidates_downstream_and_keeps_history(pipe):
    submit_intake(pipe)
    submit_direction(pipe)
    review_pass(pipe, "direction")
    approve(pipe, "direction")

    a = pipe.start_attempt(RUN, "material_plan")
    pipe.submit(RUN, "material_plan", a.token, content="material v1", summary="m1")

    before = pipe.state(RUN)
    assert before.stages["material_plan"].stale is False
    assert before.stages["material_plan"].latest.version == 1

    # Rewrite intake. Direction becomes stale, and material_plan with it.
    a2 = pipe.start_attempt(RUN, "intake")
    pipe.submit(RUN, "intake", a2.token, content="intake body v2", summary="intake revised")

    after = pipe.state(RUN)
    assert after.stages["direction"].stale is True
    assert "intake" in after.stages["direction"].stale_reason
    assert after.stages["material_plan"].stale is True
    assert after.stages["material_plan"].stale_reason

    # Nothing was deleted: both intake versions and the material plan survive.
    assert [a.version for a in pipe.store.list_artifacts(RUN, "intake")] == [1, 2]
    assert after.stages["material_plan"].latest.version == 1
    # intake v2 is itself fine; it is direction that now has to be redone.
    assert after.stages["intake"].stale is False
    assert after.next_stage == "direction"


def test_stale_downstream_cannot_be_approved_or_extended(pipe):
    submit_intake(pipe)
    submit_direction(pipe)
    review_pass(pipe, "direction")
    approve(pipe, "direction")
    a2 = pipe.start_attempt(RUN, "intake")
    pipe.submit(RUN, "intake", a2.token, content="intake v2", summary="revised")

    with pytest.raises(PipelineError) as err:
        pipe.record_review(RUN, "direction", verdict="pass", findings=[])
    assert err.value.rule == "review"

    with pytest.raises(PipelineError):
        pipe.start_attempt(RUN, "material_plan")


def test_an_attempt_briefed_on_old_upstream_cannot_submit_after_it_moves(pipe):
    """Re-checked at submit time, not just at start time."""
    submit_intake(pipe)
    direction_attempt = pipe.start_attempt(RUN, "direction")

    intake_attempt = pipe.start_attempt(RUN, "intake")
    pipe.submit(RUN, "intake", intake_attempt.token, content="intake v2", summary="moved")

    with pytest.raises(PipelineError):
        pipe.submit(
            RUN, "direction", direction_attempt.token, content="stale dir", summary="s"
        )


# -------------------------------------------------------------- integrity --


def test_tampering_with_an_artifact_blocks_approval(pipe, tmp_path):
    submit_intake(pipe)
    submit_direction(pipe)
    review_pass(pipe, "direction")
    nonce = pipe.issue_nonce(RUN, "direction")

    path = tmp_path / "runs" / RUN / "artifacts" / "direction-v1.md"
    path.write_text("quietly edited after review", encoding="utf-8")

    state = pipe.state(RUN)
    assert state.stages["direction"].integrity_ok is False

    with pytest.raises(PipelineError) as err:
        pipe.record_approval(RUN, "direction", nonce=nonce, decision="approved")
    assert "fingerprint" in str(err.value)


def test_ungated_stages_have_no_review_or_approval_path(pipe):
    submit_intake(pipe)
    with pytest.raises(PipelineError):
        pipe.record_review(RUN, "intake", verdict="pass", findings=[])
    with pytest.raises(PipelineError):
        pipe.issue_nonce(RUN, "intake")


# ------------------------------------------------------------------ shape --


def test_full_happy_path_reaches_writing(pipe):
    submit_intake(pipe)
    submit_direction(pipe)
    review_pass(pipe, "direction")
    approve(pipe, "direction")

    a = pipe.start_attempt(RUN, "material_plan")
    pipe.submit(RUN, "material_plan", a.token, content="material v1", summary="m")

    assert pipe.start_attempt(RUN, "writing").role == "writer"
    state = pipe.state(RUN)
    assert [state.stages[s].status for s in ("intake", "direction")] == ["done", "approved"]
    assert state.stages["material_plan"].status == "done"


def test_state_reports_what_is_possible_right_now(pipe):
    submit_intake(pipe)
    state = pipe.state(RUN)
    # Redoing intake must stay possible: that is how upstream gets revised.
    assert state.stages["intake"].can_start is True
    assert state.stages["direction"].can_start is True
    assert state.stages["direction"].can_approve is False
    assert state.stages["material_plan"].can_start is False
    assert state.next_stage == "direction"


def test_unknown_stage_is_rejected(pipe):
    with pytest.raises(PipelineError):
        pipe.stage_state(RUN, "layout_preview")
