"""Loopback HTTP surface for durable jobs, role connections and reviewed artifacts.

Host/origin and JSON checks protect local mutations from third-party pages.
Browser polling and legacy SSE observe the same execution journal. Approval
nonces remain outside logs and model context; only the decision route approves.
"""
from __future__ import annotations

import argparse
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agents.orchestrator import Orchestrator, OrchestratorTurn
from .agents.reviewer import run_review
from .config import Settings, load_settings
from .ledger import Ledger
from .models import PipelineError, STAGES, valid_run_id
from .pipeline import Pipeline
from .store import Store
from . import vocabulary
from .jobs import JobManager
from .recovery import install_recovery_routes
from .connections import ConnectionStore
from .learning import LearningService
from .learning_assets import LearningAssets, REPOSITORY, US_MEDIA
from .creative_packages import CreativePackages
from .image_analysis import install_image_analysis_routes
from .revisions import install_revision_routes
from .product_cards import install_product_card_routes
from .references import install_reference_routes
from .outcomes import install_outcome_routes
from .production import install_production_routes
from .project_history import install_history_routes
from .codex_auth import CodexAuth, install_codex_auth_routes
from .doctor import runtime_identity
from .http_learning import install_learning_routes
from .http_runs import install_run_routes
from .http_materials import install_material_routes
from .http_approvals import install_approval_routes
from .http_execution import install_execution_routes

WEB_DIR = Path(__file__).parent / "web"


def build_app(
    settings: Settings | None = None,
    client_factory: Callable[[], Any] | None = None,
    *,
    learning_repository_root: Path | None = None,
    learning_media_root: Path | None = None,
    production_image_factory: Callable[[], Any] | None = None,
    production_video_factory: Callable[[], Any] | None = None,
    production_poll_delay: float = 3,
    production_poll_count: int = 200,
) -> FastAPI:
    """Build the app.

    ``client_factory`` exists so tests can drive the whole HTTP surface with a
    scripted model and spend nothing. In normal use it is None and each turn
    freezes a role-connection snapshot for each job.
    """
    settings = settings or load_settings()
    store = Store(settings.data_root)
    pipeline = Pipeline(store)
    connections = ConnectionStore(settings.data_root)
    learning = LearningService(store, repository_root=learning_repository_root)
    learning_assets = LearningAssets(learning_repository_root or REPOSITORY, learning_media_root or US_MEDIA)
    creative_packages = CreativePackages(store, learning_repository_root or REPOSITORY)
    ledger = Ledger(store, settings if client_factory is not None else connections.settings(settings))

    def require_review_version(run_id, review):
        if any(s.open_attempt for s in pipeline.state(run_id).stages.values()):
            raise PipelineError('review', '有尚未结束的制作步骤，请先在运行状态中处理。')
        current = pipeline.stage_state(run_id, review.stage)
        if (not current.latest or current.latest.version != review.expected_version
                or current.latest.sha256 != review.expected_artifact_sha256):
            raise PipelineError('version_conflict', '方案已更新，请阅读当前版本后再审核。')
        if not current.can_review:
            raise PipelineError('review', '这一版当前不能发起审核，请刷新项目状态。')

    def prepare_job(run_id, review=None, reference_consent=False, feedback_target=None):
        if review is not None:
            require_review_version(run_id, review)
        if client_factory is not None:
            job_settings, client = settings, client_factory()
            summary = {'source': 'injected-test-client',
                       'roles': {role: {'model': settings.model_for(role)} for role in
                                 ('orchestrator', 'director', 'material', 'writer', 'reviewer')}}
        else:
            snapshot = connections.snapshot()
            job_settings = snapshot.settings(settings)
            summary = snapshot.public()

        from .agents.bailian_omni import connection_info
        reference_connection = connection_info() if reference_consent else None
        reference_revision = connections.public()['revision']
        if reference_consent:
            summary['reference_analysis'] = {'connection': reference_connection, 'revision': reference_revision,
                'notice': '仅本轮明确参考要求才按需调用；非图片/视频生成授权'}

        def run(text, emit, check_cancel):
            job_client = client if client_factory is not None else snapshot.client(
                check_cancel=check_cancel, max_tokens=job_settings.max_tokens,
                cancel_event=jobs.cancelled)
            def production_review(args):
                from .production import ActionIn
                service = app.state.production
                payload = ActionIn(request_id=uuid.uuid4().hex, expected_sha256=args['expected_sha256'], consent=True)
                runner, _ = service.prepare_review(run_id, args['draft_id'], payload,
                    frozen=(job_settings, job_client, summary))
                runner('检查制作输入', emit, check_cancel)
                return next(d for d in service.workspace(run_id, for_agent=True)['drafts'] if d['draft_id'] == args['draft_id'])
            if review is not None:
                # A typed command bypasses the producer loop, not review checks.
                require_review_version(run_id, review)
                check_cancel()
                store.append_chat(run_id, 'user', text)
                outcome = run_review(store=store, pipeline=pipeline, settings=job_settings,
                    client=job_client, ledger=Ledger(store, job_settings), run_id=run_id,
                    stage=review.stage, on_event=emit, check_cancel=check_cancel,
                    learning_service=learning, explicit_request=True)
                if outcome.verdict is None:
                    raise PipelineError('review', outcome.error or '审核未返回结论，原方案已保留。')
                label = vocabulary.stage_label(review.stage)
                reply = (f'「{label}」第 {review.expected_version} 版已通过审核。请在创作板阅读并决定是否采用。'
                         if outcome.verdict == 'pass' else
                         f'「{label}」第 {review.expected_version} 版审核提出了修改意见。请在创作板查看关键问题，再决定调整方向。')
                store.append_chat(run_id, 'assistant', reply)
                return OrchestratorTurn(reply, 'done', outcome.rounds)
            def observe_reference(source, instruction, reference_emit, reference_cancel):
                if not reference_connection:
                    raise PipelineError('reference_connection', '参考观察连接未就绪，请检查当前百炼连接。')
                return app.state.observe_reference(run_id, source, instruction, reference_emit, reference_cancel,
                    endpoint=reference_connection['base_url'], revision=reference_revision)
            def observe_images(items, image_emit, image_cancel):
                if not reference_connection:
                    raise PipelineError('image_connection', '图片观察连接未就绪，请检查当前百炼连接。')
                from .agent_images import observe
                return observe(store, job_settings, run_id, items, endpoint=reference_connection['base_url'],
                    emit=image_emit, check_cancel=image_cancel, client_factory=client_factory)
            return Orchestrator(store, pipeline, job_settings, job_client,
                Ledger(store, job_settings), run_id, on_event=emit, check_cancel=check_cancel,
                learning_service=learning, production_review=production_review,
                reference_observer=observe_reference, image_observer=observe_images).send(
                    text, feedback_context=feedback_target)
        return run, summary

    jobs = JobManager(store, pipeline, ledger, prepare_job)
    codex_auth = CodexAuth(jobs)
    jobs.before_submit = codex_auth.require_idle

    def continuation_request_id(approval):
        return f'decision:{approval.nonce_sha256}'

    def continuation_text(approval):
        if approval.decision == 'returned':
            return f'请按我的补充意见调整当前方案：{approval.note}。完成内部检查后让我看方案。'
        if approval.stage == 'direction':
            return (f'我已在界面选择方向 {approval.selected_option_id}。{approval.note} '
                    '请继续完成素材安排和完整方案，完成内部审核后展示给我确认。')
        return (f'我已在界面确认完整方案。{approval.note} '
                '请准备对应的分镜参考图制作方案，在工作区展示实际输入供我确认。'
                '先不提交图片或视频生成。')

    def continuation_job(run_id, approval):
        return jobs.submit(run_id, continuation_text(approval), continuation_request_id(approval))

    def pending_continuation(run_id):
        rows = store.read_events(run_id)
        created = {row.get('request_id') for row in rows if row.get('kind') == 'job_created'}
        for approval in reversed(store.list_approvals(run_id)):
            if not approval.continue_after or continuation_request_id(approval) in created:
                continue
            current = pipeline.stage_state(run_id, approval.stage).latest
            if current and current.version == approval.artifact_version and current.sha256 == approval.artifact_sha256:
                return {'stage': approval.stage, 'expected_version': approval.artifact_version,
                        'expected_artifact_sha256': approval.artifact_sha256,
                        'message': '决定已保存，下一步尚未启动。请检查连接后点击继续。'}
        return None

    @asynccontextmanager
    async def lifespan(app):
        jobs.initialize()
        try:
            yield
        finally:
            try:
                codex_auth.close()
            finally:
                jobs.close()

    app = FastAPI(title="US video studio", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.jobs = jobs
    app.state.codex_auth = codex_auth
    app.state.runtime_info = runtime_identity(settings.data_root)

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(request, exc):
        # FastAPI's default includes rejected input, which may contain an API key.
        return JSONResponse({'detail': [
            {key: error[key] for key in ('loc', 'msg', 'type') if key in error}
            for error in exc.errors()
        ]}, status_code=422)

    @app.middleware('http')
    async def local_requests_only(request: Request, call_next):
        host = request.headers.get('host', '')
        try:
            parsed = urlsplit('http://' + host)
            allowed = parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
            origin = request.headers.get('origin')
            if origin:
                source = urlsplit(origin)
                allowed = allowed and source.scheme == request.url.scheme and source.netloc.lower() == host.lower()
            allowed = allowed and request.headers.get('sec-fetch-site') != 'cross-site'
        except ValueError:
            allowed = False
        if not allowed:
            return JSONResponse({'detail': '只接受本机制片室同源请求。'}, status_code=403)
        if request.method in {'POST', 'PUT', 'PATCH'} and request.url.path.startswith('/api/'):
            content_type = request.headers.get('content-type', '').split(';')[0].lower()
            upload = request.url.path.endswith('/materials')
            empty = (request.url.path.endswith(('/nonce', '/cancel'))
                     and request.url.path != '/api/connections/codex/login/cancel')
            if ((upload and content_type != 'multipart/form-data') or
                    (not upload and not empty and content_type != 'application/json')):
                return JSONResponse({'detail': '此接口要求 application/json。'}, status_code=415)
        if request.url.path.startswith('/api/'):
            try:
                jobs.initialize()
            except PipelineError as exc:
                return JSONResponse({'detail': str(exc)}, status_code=409)
        # A learning run reuses the run store and JobManager but never enters the
        # production state machine. This boundary also covers future route
        # handlers that forget to check Run.kind themselves.
        match = re.fullmatch(r'/api/runs/([a-z0-9][a-z0-9-]*)(?:/(.*))?', request.url.path)
        if match and request.method in {'POST', 'PUT', 'PATCH'}:
            try:
                learning_run = store.read_run(match[1]).kind == 'learning'
            except PipelineError:
                learning_run = False
            suffix = match[2] or ''
            permitted = (suffix in {'restore', 'jobs', 'materials', 'material-observations',
                                    'reference-videos', 'message'}
                         or suffix.startswith('learning/')
                         or bool(re.fullmatch(r'jobs/[^/]+/(cancel|steer|resume-steer)', suffix))
                         or suffix.startswith('recovery/'))
            if learning_run and not permitted:
                return JSONResponse({'detail': '独立学习记录不能执行生产阶段、批准或媒体生成。'}, status_code=409)
        return await call_next(request)

    def require_run(run_id: str, *, include_deleted: bool = False) -> None:
        if not valid_run_id(run_id):
            raise HTTPException(400, "invalid run id")
        try:
            store.read_run(run_id)
        except PipelineError as exc:
            raise HTTPException(404, str(exc)) from exc
        if not include_deleted and store.deletion_state(run_id):
            raise HTTPException(410, '项目已移入回收站，请先恢复后再继续。')

    from .reference_video import install_reference_video_routes
    install_reference_video_routes(app, jobs, require_run, connections, settings, prepare_job, client_factory=client_factory)

    install_recovery_routes(app, jobs, require_run)
    install_codex_auth_routes(app, codex_auth)
    install_history_routes(app, store, require_run)
    install_image_analysis_routes(app, jobs, require_run, connections, settings, client_factory=client_factory)
    install_revision_routes(app, jobs, require_run, connections, settings, learning, client_factory=client_factory)
    install_product_card_routes(app, jobs, require_run)
    install_reference_routes(app, jobs, require_run, learning)
    install_outcome_routes(app, jobs, require_run)
    install_production_routes(app, jobs, require_run, connections, settings, learning,
        client_factory=client_factory, image_factory=production_image_factory,
        video_factory=production_video_factory, poll_delay=production_poll_delay, poll_count=production_poll_count)

    install_learning_routes(app, learning, learning_assets, creative_packages, store, pipeline, jobs, require_run)

    install_run_routes(app, store, pipeline, ledger, jobs, settings, require_run, pending_continuation)

    install_material_routes(app, store, jobs, require_run)

    install_approval_routes(app, jobs, pipeline, store, require_run, continuation_job)

    install_execution_routes(app, connections, settings, ledger, jobs, store, pipeline, require_run, prepare_job, client_factory)

    # ---------------------------------------------------------------- static --

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(str(WEB_DIR / "index.html"), headers={
                'X-Content-Type-Options': 'nosniff',
                'Content-Security-Policy': "frame-ancestors 'none'",
                'Cache-Control': 'no-store',
            })

    return app


app = None  # created in main() so importing this module has no side effects


def main() -> None:
    parser = argparse.ArgumentParser(description="US video studio (local only)")
    parser.add_argument("--port", type=int, default=4760)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("the studio must bind to loopback")

    import uvicorn

    settings = load_settings()
    print(f"data root : {settings.data_root}")
    print(f"stages    : {' -> '.join(STAGES)}")
    limits = []
    if settings.budget_limit_usd is not None:
        limits.append(f"{settings.budget_limit_usd:.2f} USD")
    if settings.budget_limit_tokens is not None:
        limits.append(f"{settings.budget_limit_tokens} tokens")
    print(f"budget cap: {', '.join(limits) if limits else 'none'} (all runs)")
    print(f"listening : http://{args.host}:{args.port}  (loopback only)")
    uvicorn.run(build_app(settings), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
