"""Version-bound user decision HTTP routes."""
from __future__ import annotations
from typing import Any
from fastapi import HTTPException
from .http_contracts import BriefAmendmentDecisionIn, BriefAmendmentIn, DecisionIn, DecisionContinuationIn
from .models import PipelineError, STAGE_SPEC


def install_approval_routes(app, jobs, pipeline, store, require_run, continuation_job):
    # -------------------------------------------------------------- brief amendments --

    @app.post("/api/runs/{run_id}/brief-amendment")
    def propose_brief_amendment(run_id: str, payload: BriefAmendmentIn) -> dict[str, Any]:
        """Propose (and optionally approve) an ActiveBrief amendment.

        Bound to the direction artifact the user is looking at when
        expected_* is provided, so a stale page cannot rewrite the brief
        against a version he is no longer reading.
        """
        from .active_brief import fingerprint as brief_fp, propose_amendment

        require_run(run_id)
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
                amendment = propose_amendment(
                    store, run_id,
                    changes=[c.model_dump(exclude_none=True) for c in payload.changes],
                    note=payload.note,
                    source="user",
                    approve=payload.approve,
                )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except PipelineError as exc:
            raise HTTPException(409, str(exc)) from exc
        result = dict(amendment)
        if payload.approve:
            from .active_brief import compile_active_brief
            result["brief_fingerprint"] = brief_fp(compile_active_brief(store, run_id))
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
