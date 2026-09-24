"""Project identity, artifacts, state and timeline HTTP routes."""
from __future__ import annotations
import hashlib
from typing import Any
from fastapi import HTTPException
from .http_contracts import RunIn, JobIn, redact_http_event, redact_http_events
from .models import PipelineError, STAGE_SPEC
from .product_cards import create_with_card
from .project_brief import project_brief, latest_execution
from .progress import project_progress
from .structured import open_questions, view
from .timeline import build_timeline
from . import vocabulary


def install_run_routes(app, store, pipeline, ledger, jobs, settings, require_run, pending_continuation):
    # ------------------------------------------------------------------ runs --

    @app.get("/api/runs")
    def list_runs() -> list[dict[str, Any]]:
        out = []
        for run in store.list_runs():
            if store.deletion_state(run.run_id):
                continue
            totals = ledger.totals(run.run_id)
            state = pipeline.state(run.run_id).to_dict()
            rows = store.read_events(run.run_id)
            brief = project_brief(state, latest_execution(store, run.run_id, rows))
            selection = next((row['selection'] for row in reversed(rows)
                              if row.get('kind') == 'creative_context_selected'
                              and row.get('selection', {}).get('format') in ('video', 'photo')), None)
            out.append({**run.to_dict(), "spend": totals.to_dict(),
                        "progress": {**project_progress(state),
                                     'title': brief['title'], 'detail': brief['detail']}, 'project_brief': brief,
                        'updated_at': rows[-1]['at'] if rows else run.created_at,
                        'content_format': selection['format'] if selection else None})
        return sorted(out, key=lambda r: r['updated_at'], reverse=True)

    @app.get('/api/trash')
    def list_deleted_projects():
        return sorted([
            {**run.to_dict(), 'deleted_at': deleted['at']}
            for run in store.list_runs()
            if (deleted := store.deletion_state(run.run_id))
        ], key=lambda row: row['deleted_at'], reverse=True)

    @app.delete('/api/runs/{run_id}')
    def delete_project(run_id: str):
        with jobs.lock:
            require_run(run_id, include_deleted=True)
            if jobs.active and jobs.active[0] == run_id:
                raise HTTPException(409, '项目正在执行。请先停止任务，等待结束后再删除。')
            store.set_deleted(run_id, True)
        return {'run_id': run_id, 'deleted': True}

    @app.post('/api/runs/{run_id}/restore')
    def restore_project(run_id: str):
        with jobs.lock:
            require_run(run_id, include_deleted=True)
            store.set_deleted(run_id, False)
        return store.read_run(run_id).to_dict()

    @app.post("/api/runs")
    def create_run(payload: RunIn) -> dict[str, Any]:
        try:
            with jobs.lock:
                run = (create_with_card(store, payload) if payload.product_card
                       else store.create_run(payload.run_id, payload.title, payload.brief))
        except (ValueError, PipelineError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return run.to_dict()

    @app.post('/api/conversations')
    def begin_conversation(payload: JobIn):
        """Reserve one implicit project; uploads and the first message follow.

        Retrying a lost response resolves the same run, never a second project.
        """
        text = payload.text.strip()
        if not text:
            raise HTTPException(422, '先说说你想做什么，也可以附上参考素材。')
        run_id = 'chat-' + hashlib.sha256(payload.request_id.encode()).hexdigest()[:24]
        with jobs.lock:
            existing = next((r for r in store.list_runs() if r.run_id == run_id), None)
            if existing:
                require_run(run_id)
                if existing.brief != text:
                    raise HTTPException(409, '这次发送已保存，请使用原内容重试。')
                return existing.to_dict()
            return store.create_run(run_id, text.splitlines()[0][:48], text).to_dict()

    @app.get("/api/runs/{run_id}/state")
    def state(run_id: str) -> dict[str, Any]:
        require_run(run_id)
        out = pipeline.state(run_id).to_dict()
        # Attempt tokens stay on disk for submit checks; HTTP must not return
        # them (CLAUDE.md red line 11). Same rule as /events, SSE and jobs.
        for stage_row in out.get("stages") or []:
            redact_http_event(stage_row)
        totals = ledger.totals(run_id)
        out["spend"] = totals.to_dict()
        out["budget"] = {
            "limit_usd": settings.budget_limit_usd,
            "remaining_usd": ledger.remaining_usd(),
            "limit_tokens": settings.budget_limit_tokens,
            "remaining_tokens": ledger.remaining_tokens(),
            "limit_mode": "none" if settings.budget_limit_usd is None and settings.budget_limit_tokens is None else "soft_threshold",
            "pricing": ledger.price_note(),
        }
        out["pipeline"] = [
            {"stage": s.name, "owner": s.owner_role, "gated": s.gated,
             "requires": list(s.requires)}
            for s in STAGE_SPEC.values()
        ]
        return out

    @app.get("/api/runs/{run_id}/chat")
    def chat(run_id: str) -> list[dict[str, Any]]:
        require_run(run_id)
        return store.read_chat(run_id)

    @app.get("/api/runs/{run_id}/artifacts")
    def artifacts(run_id: str, stage: str | None = None) -> list[dict[str, Any]]:
        require_run(run_id)
        return [
            {**a.to_dict(), "version_label": vocabulary.version_label(a.version)}
            for a in store.list_artifacts(run_id, stage)
        ]

    @app.get("/api/runs/{run_id}/artifacts/{stage}/{version}")
    def artifact(run_id: str, stage: str, version: int) -> dict[str, Any]:
        require_run(run_id)
        try:
            record = store.read_artifact(run_id, stage, version)
            text = store.read_artifact_text(run_id, stage, version)
        except PipelineError as exc:
            raise HTTPException(404, str(exc)) from exc
        # The labels ride along so the reader can name the stage and its author
        # the way he says them. The frontend holds no wording of its own.
        return {
            **record.to_dict(),
            "content": text,
            "integrity_ok": store.verify_artifact(run_id, stage, version),
            "stage_label": vocabulary.stage_label(stage),
            "numbered": vocabulary.numbered_stage(stage),
            "version_label": vocabulary.version_label(version),
            "author": vocabulary.role_label(record.author_role),
            # The same blocks the conversation card carries, so opening a version
            # shows the same layout as reading it in the stream. Built here rather
            # than in JavaScript: `structured.view` is where the stage shapes live.
            "blocks": view(stage, record.structured),
            "questions": open_questions(record.structured, stage),
        }

    @app.get("/api/runs/{run_id}/ledger")
    def read_ledger(run_id: str) -> dict[str, Any]:
        require_run(run_id)
        return {
            "rows": [r.to_dict() for r in ledger.entries(run_id)],
            "totals": ledger.totals(run_id).to_dict(),
            "pricing": ledger.price_note(),
        }

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, limit: int = 200) -> list[dict[str, Any]]:
        require_run(run_id)
        # Attempt tokens stay on disk for server-side submit checks; HTTP must
        # not return the plaintext (CLAUDE.md red line 11).
        return redact_http_events(store.read_events(run_id)[-limit:])

    @app.get("/api/runs/{run_id}/timeline")
    def timeline(run_id: str) -> dict[str, Any]:
        """The conversation as the screen shows it: turns, cards, and what is pending.

        Read-only, and derived fresh from disk on every request — there is no
        cached view that could disagree with the run directory.
        """
        require_run(run_id)
        with store.run_lock(run_id):
            result = build_timeline(store, pipeline, ledger, run_id, latest_execution(store, run_id))
            # Shared redact across the whole view payload (red line 11): any
            # nested open_attempt/token is stripped the same way as /events.
            def _redact_tree(node):
                if isinstance(node, dict):
                    redact_http_event(node)
                    for value in node.values():
                        _redact_tree(value)
                elif isinstance(node, list):
                    for item in node:
                        _redact_tree(item)
            _redact_tree(result)
            from .creation_mode import current
            result['creation_mode'] = current(store, run_id)
            result['pending_continuation'] = pending_continuation(run_id)
            return result
