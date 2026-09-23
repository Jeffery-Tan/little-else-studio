"""Connection settings and durable execution HTTP routes."""
from __future__ import annotations
import asyncio
import json
import uuid
from typing import Any, Iterator
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from .connection_checks import ConnectionCheckError
from .connections import ConnectionConflict
from .http_contracts import DeepSeekCheckIn, ReviewActionIn, JobIn, MessageIn, redact_http_event
from .models import PipelineError
from .project_brief import project_brief, latest_execution
from . import vocabulary


def install_execution_routes(app, connections, settings, ledger, jobs, store, pipeline, require_run, prepare_job, client_factory):
    # ------------------------------------------------------------ conversation --

    def sse(events: Iterator[dict[str, Any] | None]) -> Iterator[str]:
        """Serialize each event, adding the plain-language sentence for it.

        The wording is added here rather than in JavaScript so the live stream and
        the reloaded timeline say the same thing — both go through
        ``vocabulary``. The raw fields travel alongside, because the
        "机器细节" panel needs them; ``say`` is what gets shown by default.
        Attempt tokens are stripped first (CLAUDE.md red line 11).
        """
        for event in events:
            if event is None:
                break
            payload = redact_http_event(dict(event))
            payload["say"] = vocabulary.describe_event(event)
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    @app.get("/api/connections")
    def connection_settings():
        return connections.public()

    @app.get('/api/capabilities')
    def application_capabilities():
        """The same declared boundaries the producer can inspect, without credentials."""
        from .tools.context import capabilities
        return capabilities(connections.settings(settings), 'orchestrator')

    @app.get('/api/runtime')
    def application_runtime():
        """Loaded process identity, not a live model or media-quality verification."""
        return app.state.runtime_info

    @app.post('/api/connections/deepseek/check')
    def check_deepseek_connection(payload: DeepSeekCheckIn):
        try:
            return connections.check_deepseek(**payload.model_dump())
        except ConnectionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ConnectionCheckError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.put("/api/connections")
    def update_connections(payload: dict[str, Any]):
        try:
            result = connections.save(payload)
            if client_factory is None:
                ledger.settings = connections.settings(settings)
            return result
        except ConnectionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    def present_job(job):
        if job is not None:
            for event in job["events"]:
                # Shared redaction: job polling must not return attempt tokens.
                redact_http_event(event)
                event["say"] = vocabulary.describe_event(event)
            job['brief'] = project_brief(pipeline.state(job['run_id']).to_dict(), latest_execution(store, job['run_id']))
        return job

    @app.post('/api/runs/{run_id}/actions/review')
    def review_stage_action(run_id: str, payload: ReviewActionIn):
        require_run(run_id)
        try:
            return present_job(jobs.submit(run_id,
                f'请审核「{vocabulary.stage_label(payload.stage)}」第 {payload.expected_version} 版。',
                payload.request_id, kind='stage_review', payload=payload.model_dump(exclude={'request_id'}),
                prepare=lambda rid: prepare_job(rid, review=payload)))
        except (PipelineError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/runs/{run_id}/jobs")
    def create_job(run_id: str, payload: JobIn):
        require_run(run_id)
        try:
            if payload.question_id:
                from .interactions import check_answer
                def prepare_answer(rid):
                    check_answer(store, rid, payload.question_id)
                    return prepare_job(rid, reference_consent=payload.reference_consent)
                return present_job(jobs.submit(run_id, payload.text, payload.request_id,
                    payload={'question_id': payload.question_id, **({'reference_consent': True} if payload.reference_consent else {})}, prepare=prepare_answer))
            return present_job(jobs.submit(run_id, payload.text, payload.request_id,
                payload={'reference_consent': True} if payload.reference_consent else None,
                prepare=lambda rid: prepare_job(rid, reference_consent=payload.reference_consent)))
        except (PipelineError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/runs/{run_id}/jobs/latest")
    def latest_job(run_id: str, after_seq: int = 0):
        require_run(run_id)
        return present_job(jobs.latest(run_id, max(0, after_seq)))

    @app.get("/api/runs/{run_id}/jobs/{job_id}")
    def get_job(run_id: str, job_id: str, after_seq: int = 0):
        require_run(run_id)
        try:
            return present_job(jobs.get(run_id, job_id, max(0, after_seq)))
        except PipelineError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/runs/{run_id}/jobs/{job_id}/cancel")
    def cancel_job(run_id: str, job_id: str):
        require_run(run_id)
        try:
            return present_job(jobs.cancel(run_id, job_id))
        except PipelineError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/runs/{run_id}/message")
    def send_message(run_id: str, payload: MessageIn, request: Request) -> StreamingResponse:
        require_run(run_id)
        try:
            job = jobs.submit(run_id, payload.text, str(uuid.uuid4()))
        except (PipelineError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        async def observe():
            cursor = 0
            while not await request.is_disconnected():
                current = jobs.get(run_id, job['job_id'], cursor)
                for chunk in sse(iter(current['events'])):
                    yield chunk
                cursor = current['cursor']
                if current['status'] in {'succeeded', 'failed', 'blocked', 'cancelled', 'interrupted'}:
                    return
                await asyncio.sleep(.1)

        return StreamingResponse(
            observe(),
            media_type="text/event-stream",
            headers={"cache-control": "no-store", "x-accel-buffering": "no",
                     "x-studio-job-id": job['job_id']},
        )
