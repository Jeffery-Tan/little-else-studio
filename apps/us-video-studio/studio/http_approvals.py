"""Version-bound user decision HTTP routes."""
from __future__ import annotations
from typing import Any
from fastapi import HTTPException
from .http_contracts import DecisionIn, DecisionContinuationIn
from .models import PipelineError, STAGE_SPEC


def install_approval_routes(app, jobs, pipeline, store, require_run, continuation_job):
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
