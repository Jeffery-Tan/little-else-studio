"""Version-bound user decision HTTP routes."""
from __future__ import annotations
from typing import Any
from fastapi import HTTPException
from .http_contracts import BriefAmendmentDecisionIn, BriefAmendmentIn, DecisionIn, DecisionContinuationIn
from .models import PipelineError, STAGE_SPEC


def install_approval_routes(app, jobs, pipeline, store, require_run, continuation_job,
                            amendment_redo_job=None):
    # -------------------------------------------------------------- brief amendments --

    def _start_amendment_redo(run_id: str, amendment_id: str) -> dict[str, Any]:
        """Start (or resolve) the redo that an approved amendment owes.

        The user's click is the decision; the redo is what makes the decision
        mean something. Identity comes from the amendment id, so a retried
        request — or the explicit recovery button after a failed start — looks
        up the original job instead of submitting a second one. This only ever
        starts文字策划 work; it cannot submit image or video generation.
        """
        if amendment_redo_job is None:
            return {"status": "not_started", "reason": "当前服务没有接通重做入口。"}
        try:
            job = amendment_redo_job(run_id, amendment_id)
        except (PipelineError, ValueError) as exc:
            return {"status": "not_started", "reason": str(exc)}
        return {"status": "started", "job_id": job["job_id"]}

    def _amendment_result(run_id: str, amendment: dict[str, Any]) -> dict[str, Any]:
        """The response for one amendment, whether new or resolved as a retry.

        A retry lands here too, so it has to produce the same shape — including
        the redo. Resolving the redo is idempotent by construction (its identity
        is the amendment id), so a retry looks up the original job rather than
        starting a second one. What the *record* says decides that, not a flag
        on this request: a pending or rejected row gains nothing from a retry
        that happens to carry approve=true (U3).
        """
        from .active_brief import compile_active_brief, fingerprint as brief_fp

        result = dict(amendment)
        if amendment.get("status") == "approved":
            result["brief_fingerprint"] = brief_fp(compile_active_brief(store, run_id))
            # F5: the approval invalidates the current direction version, so the
            # same click has to restart the team's work. The amendment is saved
            # either way; a failed start is reported as such, not swallowed.
            result["redo"] = _start_amendment_redo(run_id, amendment["id"])
        return result

    @app.post("/api/runs/{run_id}/brief-amendment")
    def propose_brief_amendment(run_id: str, payload: BriefAmendmentIn) -> dict[str, Any]:
        """Propose (and optionally approve) an ActiveBrief amendment.

        Bound to the direction artifact the user is looking at when
        expected_* is provided, so a stale page cannot rewrite the brief
        against a version he is no longer reading — and bound to the *brief*
        itself via ``expected_brief_fingerprint``, because the direction can
        stand still while a second amendment moves the brief underneath it
        (A1). ``request_id`` is this submission's identity: a retry carrying the
        one it used before — *and the payload that identity was minted for* —
        resolves to the amendment it already recorded, so a lost response is
        answered with neither a second decision nor a 409. The same id wearing
        a different payload is an explicit ``request_conflict`` (U3): the
        server compares the frozen payload, not just the id, so a new decision
        can never borrow an old decision's success. ``apply_brief_amendment``
        does the change-level validation.
        """
        from .active_brief import (
            BriefBaselineChanged,
            BriefRequestConflict,
            apply_brief_amendment,
            compile_active_brief,
            fingerprint as brief_fp,
            resolve_amendment_by_request,
        )

        require_run(run_id)
        # Resolved before the busy guard. A retry is not a new decision: the
        # amendment is already on disk, and answering it records nothing — but
        # the first request's own redo is exactly what makes the run busy, so
        # checking busy first would refuse the retry it is meant to resolve.
        # `_amendment_result` re-resolves the redo by amendment id, which looks
        # up the running job rather than starting another.
        if payload.request_id:
            try:
                existing = resolve_amendment_by_request(
                    store, run_id,
                    request_id=payload.request_id,
                    changes=[c.model_dump(exclude_none=True) for c in payload.changes],
                    note=payload.note,
                    approve=payload.approve,
                    expected_brief_fingerprint=payload.expected_brief_fingerprint,
                    expected_artifact_sha256=payload.expected_artifact_sha256,
                    expected_version=payload.expected_version,
                )
            except BriefRequestConflict as exc:
                raise HTTPException(409, {
                    "detail": str(exc),
                    "reason": "request_conflict",
                }) from exc
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            if existing is not None:
                return _amendment_result(run_id, existing)
        try:
            with jobs.lock:
                if jobs.busy(run_id):
                    raise HTTPException(409, "当前制作仍在运行，请等这一轮结束后修订简报。")
                if payload.expected_artifact_sha256 is not None:
                    state = pipeline.stage_state(run_id, "direction")
                    if state.latest is None:
                        raise HTTPException(409, "创意方向尚无当前版本。")
                    if (state.latest.sha256 != payload.expected_artifact_sha256
                            or (payload.expected_version is not None
                                and state.latest.version != payload.expected_version)):
                        raise HTTPException(409, "方向版本已变化，请刷新后再修订简报。")
                amendment = apply_brief_amendment(
                    store, run_id,
                    changes=[c.model_dump(exclude_none=True) for c in payload.changes],
                    note=payload.note,
                    source="user",
                    approve=payload.approve,
                    expected_brief_fingerprint=payload.expected_brief_fingerprint,
                    request_id=payload.request_id,
                    expected_artifact_sha256=payload.expected_artifact_sha256,
                    expected_version=payload.expected_version,
                )
        except BriefRequestConflict as exc:
            # A concurrent twin wrote this id first with a different payload —
            # the in-lock scan caught what the early scan could not (U3).
            raise HTTPException(409, {
                "detail": str(exc),
                "reason": "request_conflict",
            }) from exc
        except BriefBaselineChanged as exc:
            # Not a bad request: the decision was real, it simply belongs to a
            # brief that has since moved. Say which brief is current so the page
            # can reload and re-decide instead of blaming the user's input.
            raise HTTPException(409, {
                "detail": str(exc),
                "reason": "brief_changed",
                "current_brief_fingerprint": exc.current_fingerprint,
            }) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except PipelineError as exc:
            raise HTTPException(409, str(exc)) from exc
        return _amendment_result(run_id, amendment)

    @app.post("/api/runs/{run_id}/brief-amendment/{amendment_id}/redo")
    def resume_brief_amendment_redo(run_id: str, amendment_id: str) -> dict[str, Any]:
        """Explicit recovery for an amendment whose redo never started."""
        from .active_brief import load_amendments

        require_run(run_id)
        with jobs.lock:
            row = next((r for r in load_amendments(store, run_id)
                        if r.get("id") == amendment_id), None)
            if row is None:
                raise HTTPException(409, "没有这条简报修订记录。")
            if row.get("status") != "approved":
                raise HTTPException(409, "这条简报修订还没有被批准。")
            result = _start_amendment_redo(run_id, amendment_id)
        if result["status"] != "started":
            raise HTTPException(409, result.get("reason") or "重做任务暂时无法启动。")
        return result

    @app.post("/api/runs/{run_id}/brief-amendment/decide")
    def decide_brief_amendment(run_id: str, payload: BriefAmendmentDecisionIn) -> dict[str, Any]:
        from .active_brief import decide_amendment

        require_run(run_id)
        try:
            with jobs.lock:
                if jobs.busy(run_id):
                    raise HTTPException(409, "当前制作仍在运行，请等这一轮结束后再决定。")
                return decide_amendment(
                    store, run_id, payload.amendment_id,
                    decision=payload.decision, note=payload.note,
                )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    # -------------------------------------------------------------- approvals --

    @app.post("/api/runs/{run_id}/nonce")
    def issue_nonce(run_id: str, stage: str, expected_artifact_sha256: str,
                    expected_version: int) -> dict[str, str]:
        """Mint the nonce that makes a click a click.

        Returned to this caller and nowhere else. Nothing writes it to disk.
        """
        require_run(run_id)
        if stage not in STAGE_SPEC or not STAGE_SPEC[stage].gated:
            raise HTTPException(400, f"{stage} is not a gated stage")
        try:
            with jobs.lock:
                if jobs.busy(run_id):
                    raise HTTPException(409, "当前制作仍在运行，请等这一轮结束后确认。")
                nonce = pipeline.issue_nonce(run_id, stage,
                    expected_artifact_sha256=expected_artifact_sha256,
                    expected_version=expected_version)
        except PipelineError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"stage": stage, "nonce": nonce}

    @app.post("/api/runs/{run_id}/decision")
    def decide(run_id: str, payload: DecisionIn) -> dict[str, Any]:
        require_run(run_id)
        if payload.stage not in STAGE_SPEC:
            raise HTTPException(400, "unknown stage")
        try:
            with jobs.lock:
                if jobs.busy(run_id):
                    raise HTTPException(409, "当前制作仍在运行，请等这一轮结束后确认。")
                approval = pipeline.record_approval(
                    run_id, payload.stage, nonce=payload.nonce,
                    decision=payload.decision, note=payload.note,
                    override_reason=payload.override_reason,
                    expected_artifact_sha256=payload.expected_artifact_sha256,
                    expected_version=payload.expected_version,
                    selected_option_id=payload.selected_option_id,
                    continue_after=payload.continue_after,
                )
        except PipelineError as exc:
            raise HTTPException(409, str(exc)) from exc
        result = approval.to_dict()
        if payload.continue_after:
            try:
                job = continuation_job(run_id, approval)
                result['continuation'] = {'status': 'started', 'job_id': job['job_id']}
            except (PipelineError, ValueError) as exc:
                # The approval is durable. The same explicit user decision can
                # resume the exact request without replacing or replaying a job.
                result['continuation'] = {'status': 'not_started', 'reason': str(exc)}
        return result

    @app.post('/api/runs/{run_id}/decision-continuation')
    def resume_decision_continuation(run_id: str, payload: DecisionContinuationIn):
        require_run(run_id)
        with jobs.lock:
            approval = next((a for a in reversed(store.list_approvals(run_id, payload.stage))
                             if a.continue_after and a.artifact_version == payload.expected_version
                             and a.artifact_sha256 == payload.expected_artifact_sha256), None)
            if approval is None:
                raise HTTPException(409, '这一版没有已保存的接续决定。')
            current = pipeline.stage_state(run_id, payload.stage).latest
            if not current or current.version != payload.expected_version or current.sha256 != payload.expected_artifact_sha256:
                raise HTTPException(409, '方案已有新版本，请先阅读当前内容。')
            try:
                job = continuation_job(run_id, approval)
            except (PipelineError, ValueError) as exc:
                raise HTTPException(409, str(exc)) from exc
        return {'status': 'started', 'job_id': job['job_id']}
