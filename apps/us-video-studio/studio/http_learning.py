"""Learning and delivery HTTP routes."""
from __future__ import annotations
from fastapi import HTTPException, Query
from pydantic import Field
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from .delivery import delivery_snapshot, delivery_zip, photo_delivery_zip
from .http_contracts import LearningProposalIn, LearningApplyIn, LearningRejectIn, LearningRefreshIn
from .learning import LearningConflict
from .learning_session import session_snapshot
from .models import PipelineError


class LearningMethodRefreshIn(LearningRefreshIn):
    expected_run_methods_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    expected_current_methods_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')


def install_learning_routes(app, learning, learning_assets, creative_packages, store, pipeline, jobs, require_run):
    # Read-only history and explicit human review share the canonical US library.
    @app.get('/api/learning')
    def learning_catalog():
        return {**learning.catalog(), 'archives': learning_assets.archives()}

    @app.get('/api/learning/migration-preview')
    def learning_migration_preview():
        try:
            return learning.migration_preview()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/learning/documents/{document_id}')
    def learning_document(document_id: str):
        try:
            return learning.document(document_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    def learning_file(path):
        headers = {'X-Content-Type-Options': 'nosniff',
                   'Cache-Control': 'no-store',
                   'Content-Security-Policy': "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; media-src 'self'"}
        return FileResponse(path, headers=headers)

    @app.get('/api/learning/cases/{case_id}/media')
    def learning_case_media(case_id: str):
        try:
            return learning_file(learning_assets.case_media(learning.catalog(), case_id))
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get('/api/learning/archive/{archive_id}')
    def learning_archive(archive_id: str):
        try:
            return learning_file(learning_assets.archive(archive_id))
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get('/api/learning/photo-archive/{filename}')
    def learning_photo_archive(filename: str):
        try:
            return learning_file(learning_assets.photo_archive_file(filename))
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get('/api/runs/{run_id}/learning')
    def run_learning(run_id: str):
        require_run(run_id)
        try:
            pin = learning.pin_status(run_id) if store.read_run(run_id).kind == 'creative' else None
        except (LearningConflict, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {'pin_status': pin,
                'artifacts': [{'stage': a.stage, 'version': a.version,
                              'artifact_sha256': a.sha256, 'title': a.summary}
                             for a in store.list_artifacts(run_id)],
                'contexts': [{**event, 'snapshot': learning.context_record(run_id, event['snapshot_sha256'])}
                             for event in learning.contexts(run_id)],
                'proposals': learning.proposals(run_id),
                'method_usage': [event for event in store.read_events(run_id)
                                 if event['kind'] == 'method_usage_recorded']}

    @app.post('/api/runs/{run_id}/learning/refresh')
    def refresh_creative_library(run_id: str, payload: LearningMethodRefreshIn):
        require_run(run_id)
        try:
            with jobs.lock:
                if jobs.has_executions():
                    raise HTTPException(409, '当前任务仍在运行，请结束后再刷新学习库。')
                return learning.refresh_run_pin(run_id, payload.expected_run_sha256,
                    payload.expected_current_sha256, payload.request_id,
                    expected_run_methods_sha256=payload.expected_run_methods_sha256,
                    expected_current_methods_sha256=payload.expected_current_methods_sha256)
        except (LearningConflict, ValueError, PipelineError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/learning/method-results')
    def learning_method_results(run_id: str):
        require_run(run_id)
        from .method_outcomes import method_results
        try:
            return {'results': method_results(store, learning, run_id)}
        except (ValueError, PipelineError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/learning/method-comparison')
    def learning_method_comparison(run_id: str, baseline: str = Query(min_length=1, max_length=128),
                                   candidate: str = Query(min_length=1, max_length=128)):
        require_run(run_id)
        from .method_outcomes import compare_methods
        try:
            return compare_methods(store, learning, run_id, baseline, candidate)
        except (ValueError, PipelineError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/learning/session')
    def learning_session(run_id: str):
        require_run(run_id)
        try:
            return session_snapshot(store, learning, run_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/api/runs/{run_id}/learning/proposals')
    def learning_propose(run_id: str, payload: LearningProposalIn):
        require_run(run_id)
        methods = {'feedback': learning.propose_feedback, 'source': learning.propose_source,
                   'method': learning.propose_method, 'observation': learning.propose_observation,
                   'delta': learning.propose_delta, 'migration': learning.propose_migration}
        if payload.type not in methods:
            raise HTTPException(400, '未知学习变更类型。')
        if payload.type in {'delta', 'migration'} and store.read_run(run_id).kind != 'learning':
            raise HTTPException(409, '独立学习变更需要学习记录。')
        try:
            with jobs.lock:
                if jobs.has_executions():
                    raise HTTPException(409, '当前制作仍在运行，请结束后再整理学习记录。')
                return (methods[payload.type](run_id, actor='user') if payload.type == 'migration'
                        else methods[payload.type](run_id, payload.payload, actor='user'))
        except LearningConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, PipelineError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/api/runs/{run_id}/learning/proposals/{proposal_id}/apply')
    def learning_apply(run_id: str, proposal_id: str, payload: LearningApplyIn):
        require_run(run_id)
        try:
            with jobs.lock:
                if jobs.has_executions():
                    raise HTTPException(409, '当前制作仍在运行，请结束后再应用学习变更。')
                proposal = learning.proposal(run_id, proposal_id)
                if proposal['type'] in {'delta', 'migration'} and (
                        not payload.expected_proposal_sha256 or not payload.request_id):
                    raise HTTPException(400, '请提交页面展示的准确提案指纹与本次确认身份。')
                return learning.apply(run_id, proposal_id, payload.expected_library_sha256,
                                      expected_proposal_sha256=payload.expected_proposal_sha256,
                                      request_id=payload.request_id)
        except LearningConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, PipelineError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/api/runs/{run_id}/learning/proposals/{proposal_id}/reject')
    def learning_reject(run_id: str, proposal_id: str, payload: LearningRejectIn):
        require_run(run_id)
        try:
            with jobs.lock:
                if jobs.has_executions():
                    raise HTTPException(409, '当前任务仍在运行，请结束后再拒绝提案。')
                return learning.reject(run_id, proposal_id, payload.expected_proposal_sha256,
                                       payload.request_id)
        except LearningConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, PipelineError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/api/runs/{run_id}/learning/close')
    def close_learning(run_id: str):
        require_run(run_id)
        if store.read_run(run_id).kind != 'learning':
            raise HTTPException(409, '只有独立学习记录可以结束学习。')
        with jobs.lock:
            if jobs.has_executions():
                raise HTTPException(409, '当前任务仍在运行，请结束后再关闭。')
            with store.run_lock(run_id):
                rows = store.read_events(run_id)
                if not rows or rows[-1]['kind'] != 'learning_closed':
                    store.append_event(run_id, 'learning_closed')
        return session_snapshot(store, learning, run_id)

    @app.get('/api/runs/{run_id}/creative-packages')
    def packages(run_id: str):
        require_run(run_id)
        try:
            return {'packages': creative_packages.packages(run_id)}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/delivery')
    def delivery(run_id: str):
        require_run(run_id)
        try:
            return delivery_snapshot(store, pipeline, creative_packages, run_id)
        except (ValueError, PipelineError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/delivery/download')
    def download_delivery(run_id: str):
        require_run(run_id)
        try:
            payload = delivery_zip(store, pipeline, creative_packages, run_id)
            return Response(payload, media_type='application/zip', headers={
                'Content-Disposition': f'attachment; filename="{run_id}-planning.zip"',
                'Cache-Control': 'no-store'})
        except (ValueError, PipelineError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/delivery/photos/download')
    def download_photos(run_id: str, expected_snapshot_sha256: str = Query(pattern=r'^[a-f0-9]{64}$')):
        require_run(run_id)
        try:
            payload, version = photo_delivery_zip(store, pipeline, run_id, expected_snapshot_sha256,
                                                  ledger=jobs.ledger, execution_active=jobs.has_executions())
        except (ValueError, PipelineError) as exc:
            raise HTTPException(409, str(exc)) from exc
        async def chunks():
            try:
                while chunk := payload.read(64 * 1024):
                    yield chunk
            finally:
                payload.close()
        return StreamingResponse(chunks(), media_type='application/zip',
            background=BackgroundTask(payload.close), headers={
                'Content-Disposition': f'attachment; filename="{run_id}-v{version}-photos.zip"',
                'Cache-Control': 'no-store'})

    @app.get('/api/runs/{run_id}/creative-packages/{package_id}/files/{filename:path}')
    def package_file(run_id: str, package_id: str, filename: str):
        require_run(run_id)
        try:
            return learning_file(creative_packages.file(run_id, package_id, filename))
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
