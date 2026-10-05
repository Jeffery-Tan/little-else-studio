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
from .active_brief import load_amendments

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
    learning_assets = LearningAssets(learning_repository_root or REPOSITORY, learning_media_root or US_MEDIA,
                                     store=store)
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

    def prepare_job(run_id, review=None, reference_consent=False, feedback_target=None,
                    writing_revision=None, direction_resume=None, user_input_id=None,
                    source_user_input_id=None, role_resume=None, reference_recovery_context=None):
        if review is not None:
            require_review_version(run_id, review)
        if writing_revision is not None:
            from .revision_scope import validate_writing_target
            validate_writing_target(store, pipeline, run_id,
                                    writing_revision.feedback_target.model_dump())
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
            if role_resume is not None:
                from .conversation_input import message as user_message, accept, start_execution
                from .tools.dispatch import Dispatcher
                from .task_contract import evaluate
                check_cancel()
                receipt = user_message(store, run_id, user_input_id)
                accept(store, run_id, receipt)
                start_execution(store, run_id, receipt)
                outcome = Dispatcher(store, pipeline, job_settings, job_client,
                    Ledger(store, job_settings), run_id, on_event=emit,
                    check_cancel=check_cancel, learning_service=learning).resume_session(
                        role_resume['session_id'], brief=receipt['text'],
                        user_input_id=receipt['message_id'])
                if outcome.stopped_because == 'cancelled':
                    reply = '这项角色任务已停止，已有内容与记录保留；不会自动重试。'
                    store.append_chat(run_id, 'assistant', reply)
                    return OrchestratorTurn(reply, 'done', outcome.rounds,
                                            deliverable=evaluate(store, pipeline, run_id, 'follow_up'))
                if not outcome.submitted:
                    raise PipelineError('role_resume', outcome.error or '角色尚未交付；会话与旧内容保留。')
                delivery = evaluate(store, pipeline, run_id,
                    'direction' if outcome.stage == 'direction' else 'writing' if outcome.stage == 'writing' else 'follow_up')
                reply = f'这项创作任务已交回：{outcome.submitted}。请阅读当前版本，原批准不会自动继承。'
                store.append_chat(run_id, 'assistant', reply)
                return OrchestratorTurn(reply, 'done', outcome.rounds, deliverable=delivery)
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
            if writing_revision is not None:
                from .agents.roles import creative_selection
                from .revision_scope import plan_revision, validate_writing_target
                from .task_contract import evaluate as evaluate_deliverable
                from .tools.dispatch import Dispatcher

                check_cancel()
                from .conversation_input import message as user_message, accept, start_execution
                receipt = user_message(store, run_id, user_input_id)
                accept(store, run_id, receipt)
                start_execution(store, run_id, receipt)
                target = validate_writing_target(store, pipeline, run_id,
                                                 writing_revision.feedback_target.model_dump())
                previous = creative_selection(store, run_id)
                selection = {**previous, 'intent': 'edit'}
                scope = plan_revision(store, pipeline, run_id, {
                    'stage': 'writing', 'version': target['version'], 'sha256': target['sha256'],
                    'mode': 'local', 'quote': text, 'reason': '用户明确要求修改当前脚本的选中字段',
                    'fields': [{'path': target['path'], 'instruction': text}]},
                    turn_id=writing_revision.request_id, user_message=text)
                store.append_event(run_id, 'creative_context_selected', selection=selection,
                                   turn_id=writing_revision.request_id, deliverable='revision',
                                   source='explicit_writing_field')
                ledger_for_task = Ledger(store, job_settings)
                outcome = Dispatcher(store, pipeline, job_settings, job_client, ledger_for_task,
                                     run_id, on_event=emit, check_cancel=check_cancel,
                                     learning_service=learning).dispatch('writing', text,
                                                                          revision_scope=scope,
                                                                          user_input_id=user_input_id)
                if not outcome.submitted:
                    raise PipelineError('revision', outcome.error or '编剧未提交新版本，原稿已保留。')
                revised = store.latest_artifact(run_id, 'writing')
                if revised is None or revised.key != outcome.submitted:
                    raise PipelineError('revision', '编剧提交的新版本与当前脚本不一致，请检查任务记录。')
                revision_result = {'stage': 'writing', 'version': revised.version,
                                   'artifact_sha256': revised.sha256,
                                   'change_status': 'saved', 'review_status': 'not_requested'}
                emit({'type': 'writing_revision_result', 'result': revision_result})
                delivery = evaluate_deliverable(store, pipeline, run_id, 'revision', revision_scope=scope)
                reply = f'脚本新版 {outcome.submitted} 已保存。请阅读准确版本后决定是否采用。'
                store.append_chat(run_id, 'assistant', reply)
                return OrchestratorTurn(reply, 'done', outcome.rounds,
                                        deliverable=delivery)
            if direction_resume is not None:
                from .agents.roles import creative_selection
                from .direction_command import require_current, task_brief
                from .task_contract import evaluate as evaluate_deliverable
                from .tools.dispatch import Dispatcher

                # The button authorizes exactly this stage. The producer does
                # not get another chance to select a source, revision tool or
                # destination; Pipeline still freezes all current inputs.
                check_cancel()
                require_current(store, pipeline, run_id,
                                direction_resume.expected_context_sha256)
                store.append_chat(run_id, 'user', text)
                previous = creative_selection(store, run_id)
                selection = {**previous, 'intent': 'continue'}
                store.append_event(run_id, 'creative_context_selected', selection=selection,
                                   turn_id=direction_resume.request_id, deliverable='direction',
                                   source='explicit_direction_resume')
                brief = task_brief(store, pipeline, run_id)
                outcome = Dispatcher(store, pipeline, job_settings, job_client,
                    Ledger(store, job_settings), run_id, on_event=emit,
                    check_cancel=check_cancel, learning_service=learning).dispatch(
                        'direction', brief)
                if not outcome.submitted:
                    raise PipelineError('direction_resume',
                        outcome.error or '导演没有保存新版方向；旧稿已保留。')
                delivery = evaluate_deliverable(store, pipeline, run_id, 'direction')
                reply = ('新版创意方向已保存。请阅读 A/B/C 的差异，在对话区点选或提出修改意见。'
                         if delivery['status'] == 'complete' else
                         '新版方向已保存，但当前依据已变化；请查看项目状态后再决定。')
                store.append_chat(run_id, 'assistant', reply)
                return OrchestratorTurn(reply, 'done', outcome.rounds,
                                        tool_log=[{'tool': 'dispatch', 'ok': True}],
                                        deliverable=delivery)
            def observe_reference(source, instruction, reference_emit, reference_cancel):
                if not reference_connection:
                    raise PipelineError('reference_connection', '参考观察连接未就绪，请检查当前百炼连接。')
                return app.state.observe_reference(run_id, source, instruction, reference_emit, reference_cancel,
                    endpoint=reference_connection['base_url'], revision=reference_revision)
            def observe_product_video(source, product_emit, product_cancel):
                if not reference_connection:
                    raise PipelineError('product_video_connection', '商品视频观察连接未就绪，请检查当前百炼连接。')
                from .product_video import observe
                return observe(store, pipeline, connections, settings, run_id, source,
                    product_emit, product_cancel, endpoint=reference_connection['base_url'],
                    revision=reference_revision, client_factory=client_factory)
            def observe_images(items, image_emit, image_cancel):
                if not reference_connection:
                    raise PipelineError('image_connection', '图片观察连接未就绪，请检查当前百炼连接。')
                from .agent_images import observe
                return observe(store, job_settings, run_id, items, endpoint=reference_connection['base_url'],
                    emit=image_emit, check_cancel=image_cancel, client_factory=client_factory)
            return Orchestrator(store, pipeline, job_settings, job_client,
                Ledger(store, job_settings), run_id, on_event=emit, check_cancel=check_cancel,
                learning_service=learning,
                reference_observer=observe_reference, product_video_observer=observe_product_video,
                image_observer=observe_images).send(
                    text, feedback_context=feedback_target, user_input_id=user_input_id,
                    source_user_input_id=source_user_input_id,
                    internal_instruction=user_input_id is None,
                    **({'reference_recovery_context': reference_recovery_context}
                       if reference_recovery_context is not None else {}))
        return run, summary

    jobs = JobManager(store, pipeline, ledger, prepare_job)
    codex_auth = CodexAuth(jobs)
    jobs.before_submit = codex_auth.require_idle

    # One identity and one text for a decision's follow-up work, shared by the
    # browser's click and a decision the user stated in conversation. Two copies
    # is how the two channels would start behaving differently for one decision.
    from .decision import continuation_request_id, continuation_text, amendment_redo_request_id, amendment_redo_text

    def continuation_job(run_id, approval):
        return jobs.submit_decision_followup(run_id, continuation_text(approval, store=store, run_id=run_id),
                                            continuation_request_id(approval))

    # ---- brief amendment → team redo (F5) --------------------------------- #
    #
    # Approving a brief amendment invalidates the current direction version, so
    # the click that approves it is also the click that has to restart the work.
    # The request identity is derived from the amendment id, never from a
    # timestamp: retrying a lost response, or pressing the explicit "继续重做"
    # button, resolves the *same* job instead of buying a second one.
    def amendment_redo_job(run_id, amendment_id):
        return jobs.submit_decision_followup(run_id, amendment_redo_text(),
                                            amendment_redo_request_id(amendment_id))

    def pending_amendment_redo(run_id):
        """An approved amendment whose redo never started, as a recovery entry.

        It cannot reuse the approval-shaped entry: that one resolves a decision
        strictly by version + sha, and an approved amendment deliberately
        invalidates the very direction version it was decided against.

        Three ways an amendment stops being pending work, and all three have to
        be checked or the entry outlives its reason (R4):

        * the policy reads the newest approved amendment only — a later
          amendment replaces this one as the brief's current intent, and its own
          redo (started or not) is what remains;
        * the redo job exists, so the work is under way; or
        * a direction draft already reflects the *current* brief (no mapping
          blockers and no unresolved amendment), whoever produced it — an
          ordinary conversation turn can have done this work already, and
          offering it again would spend a model call on a finished job.
        """
        from .active_brief import compile_active_brief, fingerprint as brief_fp

        approved = [a for a in load_amendments(store, run_id)
                    if a.get('status') == 'approved']
        if not approved:
            return None
        newest = approved[-1]
        if any(e['kind'] == 'user_message_consumed'
               and (e.get('result') or {}).get('amendment', {}).get('id') == newest.get('id')
               and e['result'].get('continuation_status') == 'paused'
               for e in store.read_events(run_id)):
            return None
        created = {row.get('request_id') for row in store.read_events(run_id)
                   if row.get('kind') in {'job_created', 'decision_followup_suppressed'}}
        if amendment_redo_request_id(newest.get('id')) in created:
            return None

        from .active_brief import gate_admissible

        fingerprint = brief_fp(compile_active_brief(store, run_id))
        direction = pipeline.stage_state(run_id, 'direction')
        latest = direction.latest
        if latest is not None and latest.inputs.get('brief:snapshot') == fingerprint:
            fresh = gate_admissible(direction.brief_gate)
            if fresh and not (direction.brief_gate or {}).get('conflicts'):
                return None

        return {'amendment_id': newest.get('id'),
                'message': '简报修订已保存，团队重做还没有启动。请检查连接后点击继续。'}

    def pending_continuation(run_id, *, job_busy=None):
        # A running job is itself the evidence that the next step started. Without
        # this the card reappeared for a moment after the user clicked it: the
        # check below only asked whether the continuation job had ever been
        # *created*, and between the approval being written and the job row
        # landing, "not created yet" was still true. The user saw a card flash
        # under the cursor for a step that was already under way.
        #
        # ``job_busy`` is passed in by callers that already hold this run's lock,
        # because asking the job manager here would invert the lock order and
        # deadlock against the worker (`jobs._work` holds `jobs.lock` and then
        # takes the run lock). Callers without the lock may let it default.
        if job_busy is None:
            job_busy = jobs.busy(run_id)
        if job_busy:
            return None
        from .decision import continuation_intent
        pending = continuation_intent(store, pipeline, run_id)
        if pending:
            approval = pending['approval']
            return {'stage': pending['stage'], 'expected_version': approval['artifact_version'],
                    'expected_artifact_sha256': approval['artifact_sha256'],
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

    install_run_routes(app, store, pipeline, ledger, jobs, settings, require_run, pending_continuation,
                       pending_amendment_redo)

    install_material_routes(app, store, jobs, require_run)

    install_approval_routes(app, jobs, pipeline, store, require_run, continuation_job,
                            amendment_redo_job)

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
