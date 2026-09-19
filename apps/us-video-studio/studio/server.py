"""Loopback HTTP surface for durable jobs, role connections and reviewed artifacts.

Host/origin and JSON checks protect local mutations from third-party pages.
Browser polling and legacy SSE observe the same execution journal. Approval
nonces remain outside logs and model context; only the decision route approves.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agents.orchestrator import Orchestrator
from .config import Settings, load_settings
from .ledger import Ledger
from .models import PipelineError, STAGES, STAGE_SPEC, valid_run_id
from .pipeline import Pipeline
from .store import Store
from . import materials
from . import vocabulary
from .structured import open_questions, view
from .timeline import build_timeline
from .jobs import JobManager
from .recovery import install_recovery_routes
from .connections import ConnectionStore, ConnectionConflict
from .connection_checks import ConnectionCheckError
from .learning import LearningService, LearningConflict
from .learning_assets import LearningAssets, REPOSITORY, US_MEDIA
from .creative_packages import CreativePackages
from .progress import project_progress
from .delivery import delivery_snapshot, delivery_zip
from .material_observations import material_workspace, save_human_observation, ObservationError, ObservationConflict
from .image_analysis import install_image_analysis_routes
from .revisions import install_revision_routes
from .product_cards import CardBinding, create_with_card, install_product_card_routes
from .references import install_reference_routes
from .outcomes import install_outcome_routes
from .production import install_production_routes
from .project_history import install_history_routes

WEB_DIR = Path(__file__).parent / "web"


class RunIn(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=1, max_length=20000)
    product_card: CardBinding | None = None


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class JobIn(MessageIn):
    request_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')


class DecisionIn(BaseModel):
    stage: str
    nonce: str
    decision: str
    note: str = ""
    override_reason: str = ""
    expected_artifact_sha256: str = Field(min_length=64, max_length=64)
    expected_version: int = Field(ge=1)
    selected_option_id: str | None = None


class LearningProposalIn(BaseModel):
    type: str
    payload: dict[str, Any]


class LearningApplyIn(BaseModel):
    expected_library_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class DeepSeekCheckIn(BaseModel):
    expected_revision: int = Field(ge=0, strict=True)
    connection_id: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9][a-z0-9-]*$')
    api_key: str | None = Field(default=None, min_length=1, max_length=8192)


class MaterialObservationIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    expected_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_manifest_sha256: str | None = None
    purpose: str
    level: str
    coverage: str = Field(min_length=1, max_length=2000)
    findings: list[str] = Field(min_length=1, max_length=30)
    limitations: list[str] = Field(min_length=1, max_length=30)


def _read_capped(file: Any, limit: int = materials.MAX_UPLOAD_BYTES) -> bytes:
    """Read an upload, giving up the moment it passes the limit.

    ``await file.read()`` buffers the whole thing and checks afterwards, so a
    mis-dropped 4 GB file is read into memory before being refused. Stopping at
    the limit is the difference between a refusal and a hang. Read synchronously
    because every other route here is a plain ``def`` and this one does not need
    to be async to be correct.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = file.file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise materials.MaterialError(
                f"文件太大：超过 {limit // 1048576} MB 上限。"
            )
        chunks.append(chunk)
    return b"".join(chunks)


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

    def prepare_job(run_id):
        if client_factory is not None:
            job_settings, client = settings, client_factory()
            summary = {'source': 'injected-test-client',
                       'roles': {role: {'model': settings.model_for(role)} for role in
                                 ('orchestrator', 'director', 'material', 'writer', 'reviewer')}}
        else:
            snapshot = connections.snapshot()
            job_settings = snapshot.settings(settings)
            summary = snapshot.public()

        def run(text, emit, check_cancel):
            job_client = client if client_factory is not None else snapshot.client(
                check_cancel=check_cancel, max_tokens=job_settings.max_tokens)
            return Orchestrator(store, pipeline, job_settings, job_client,
                Ledger(store, job_settings), run_id, on_event=emit, check_cancel=check_cancel,
                learning_service=learning).send(text)
        return run, summary

    jobs = JobManager(store, pipeline, ledger, prepare_job)

    @asynccontextmanager
    async def lifespan(app):
        jobs.initialize()
        try:
            yield
        finally:
            jobs.close()

    app = FastAPI(title="US video studio", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.jobs = jobs

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
            allowed = parsed.hostname in {'127.0.0.1', 'localhost', '::1', 'testserver'}
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
            empty = request.url.path.endswith(('/nonce', '/cancel'))
            if ((upload and content_type != 'multipart/form-data') or
                    (not upload and not empty and content_type != 'application/json')):
                return JSONResponse({'detail': '此接口要求 application/json。'}, status_code=415)
        if request.url.path.startswith('/api/'):
            try:
                jobs.initialize()
            except PipelineError as exc:
                return JSONResponse({'detail': str(exc)}, status_code=409)
        return await call_next(request)

    def require_run(run_id: str) -> None:
        if not valid_run_id(run_id):
            raise HTTPException(400, "invalid run id")
        try:
            store.read_run(run_id)
        except PipelineError as exc:
            raise HTTPException(404, str(exc)) from exc

    install_recovery_routes(app, jobs, require_run)
    install_history_routes(app, store, require_run)
    install_image_analysis_routes(app, jobs, require_run, connections, settings, client_factory=client_factory)
    install_revision_routes(app, jobs, require_run, connections, settings, learning, client_factory=client_factory)
    install_product_card_routes(app, jobs, require_run)
    install_reference_routes(app, jobs, require_run, learning)
    install_outcome_routes(app, jobs, require_run)
    install_production_routes(app, jobs, require_run, connections, settings, learning,
        client_factory=client_factory, image_factory=production_image_factory,
        video_factory=production_video_factory, poll_delay=production_poll_delay, poll_count=production_poll_count)

    # Read-only history and explicit human review share the canonical US library.
    @app.get('/api/learning')
    def learning_catalog():
        return {**learning.catalog(), 'archives': learning_assets.archives()}

    @app.get('/api/learning/documents/{document_id}')
    def learning_document(document_id: str):
        try:
            return learning.document(document_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    def learning_file(path):
        headers = {'X-Content-Type-Options': 'nosniff',
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
        return {'artifacts': [{'stage': a.stage, 'version': a.version,
                              'artifact_sha256': a.sha256, 'title': a.summary}
                             for a in store.list_artifacts(run_id)],
                'contexts': [{**event, 'snapshot': learning.context_record(run_id, event['snapshot_sha256'])}
                             for event in learning.contexts(run_id)],
                'proposals': learning.proposals(run_id)}

    @app.post('/api/runs/{run_id}/learning/proposals')
    def learning_propose(run_id: str, payload: LearningProposalIn):
        require_run(run_id)
        methods = {'feedback': learning.propose_feedback, 'source': learning.propose_source,
                   'method': learning.propose_method, 'observation': learning.propose_observation}
        if payload.type not in methods:
            raise HTTPException(400, '未知学习变更类型。')
        try:
            with jobs.lock:
                if jobs.active is not None:
                    raise HTTPException(409, '当前制作仍在运行，请结束后再整理学习记录。')
                return methods[payload.type](run_id, payload.payload, actor='user')
        except LearningConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, PipelineError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/api/runs/{run_id}/learning/proposals/{proposal_id}/apply')
    def learning_apply(run_id: str, proposal_id: str, payload: LearningApplyIn):
        require_run(run_id)
        try:
            with jobs.lock:
                if jobs.active is not None:
                    raise HTTPException(409, '当前制作仍在运行，请结束后再应用学习变更。')
                return learning.apply(run_id, proposal_id, payload.expected_library_sha256)
        except LearningConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, PipelineError) as exc:
            raise HTTPException(400, str(exc)) from exc

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

    @app.get('/api/runs/{run_id}/creative-packages/{package_id}/files/{filename:path}')
    def package_file(run_id: str, package_id: str, filename: str):
        require_run(run_id)
        try:
            return learning_file(creative_packages.file(run_id, package_id, filename))
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    # ------------------------------------------------------------------ runs --

    @app.get("/api/runs")
    def list_runs() -> list[dict[str, Any]]:
        out = []
        for run in store.list_runs():
            totals = ledger.totals(run.run_id)
            out.append({**run.to_dict(), "spend": totals.to_dict(),
                        "progress": project_progress(pipeline.state(run.run_id).to_dict())})
        return sorted(out, key=lambda r: r["created_at"], reverse=True)

    @app.post("/api/runs")
    def create_run(payload: RunIn) -> dict[str, Any]:
        try:
            with jobs.lock:
                run = (create_with_card(store, payload) if payload.product_card
                       else store.create_run(payload.run_id, payload.title, payload.brief))
        except (ValueError, PipelineError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return run.to_dict()

    @app.get("/api/runs/{run_id}/state")
    def state(run_id: str) -> dict[str, Any]:
        require_run(run_id)
        out = pipeline.state(run_id).to_dict()
        totals = ledger.totals(run_id)
        out["spend"] = totals.to_dict()
        out["budget"] = {
            "limit_usd": settings.budget_limit_usd,
            "remaining_usd": ledger.remaining_usd(),
            "limit_mode": "soft_threshold",
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
        return store.read_events(run_id)[-limit:]

    @app.get("/api/runs/{run_id}/timeline")
    def timeline(run_id: str) -> dict[str, Any]:
        """The conversation as the screen shows it: turns, cards, and what is pending.

        Read-only, and derived fresh from disk on every request — there is no
        cached view that could disagree with the run directory.
        """
        require_run(run_id)
        return build_timeline(store, pipeline, ledger, run_id)

    # -------------------------------------------------------------- materials --

    @app.get('/api/runs/{run_id}/material-observations')
    def read_material_observations(run_id: str):
        require_run(run_id)
        try:
            return material_workspace(store, run_id)
        except ObservationError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/material-observations')
    def write_material_observation(run_id: str, payload: MaterialObservationIn):
        require_run(run_id)
        with jobs.lock:
            if jobs.active is not None:
                raise HTTPException(409, '制作团队正在运行，请结束本轮后再更新素材观察。')
            try:
                return save_human_observation(store, run_id, **payload.model_dump())
            except ObservationConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except ObservationError as exc:
                raise HTTPException(400, str(exc)) from exc

    @app.get("/api/runs/{run_id}/materials")
    def materials_index(run_id: str) -> list[dict[str, Any]]:
        """What is in the run's materials directory, usable ones and all."""
        require_run(run_id)
        return materials.list_materials(store, run_id)

    @app.get("/api/runs/{run_id}/materials/{name}")
    def material_file(run_id: str, name: str) -> FileResponse:
        """Serve one material.

        The name is whatever the browser put in the URL, so it is handed to
        `materials.resolve` rather than joined to a directory here; that
        function is the one place that decides whether a name may name a file,
        and it returns None rather than raising so the refusal is a plain 404.
        """
        require_run(run_id)
        path = materials.resolve(store, run_id, name)
        if path is None:
            raise HTTPException(404, "no such material")
        # No `filename=`, so FileResponse does not add a content-disposition
        # header and the browser renders an image inline instead of downloading
        # it. The allowlist in `materials` is what makes the extension-inferred
        # content type safe to serve.
        return FileResponse(str(path))

    @app.post("/api/runs/{run_id}/materials")
    def material_upload(run_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
        """Put one file into `materials/`.

        The third write entry point, and the only one that touches no state. It
        appends no event, produces no artifact, and cannot approve anything — the
        two gates in `CLAUDE.md` red line 2 are exactly as they were. What it can
        do is bounded by `studio.materials`, and every bound is a test.
        """
        require_run(run_id)
        try:
            with jobs.lock:
                if jobs.busy(run_id):
                    raise HTTPException(409, '当前制作仍在运行，请停止或等待后再修改素材。')
                data = _read_capped(file)
                return materials.accept_upload(store, run_id, file.filename or "", data)
        except materials.MaterialError as exc:
            # The message is written for Jeffery, not for a log — it says which
            # rule the file broke and, where there is one, what to do instead.
            raise HTTPException(400, str(exc)) from exc

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
                )
        except PipelineError as exc:
            raise HTTPException(409, str(exc)) from exc
        return approval.to_dict()

    # ------------------------------------------------------------ conversation --

    def sse(events: Iterator[dict[str, Any] | None]) -> Iterator[str]:
        """Serialize each event, adding the plain-language sentence for it.

        The wording is added here rather than in JavaScript so the live stream and
        the reloaded timeline say the same thing — both go through
        ``vocabulary``. The raw fields travel alongside, because the
        "机器细节" panel needs them; ``say`` is what gets shown by default.
        """
        for event in events:
            if event is None:
                break
            payload = dict(event)
            payload["say"] = vocabulary.describe_event(event)
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    @app.get("/api/connections")
    def connection_settings():
        return connections.public()

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
                event["say"] = vocabulary.describe_event(event)
        return job

    @app.post("/api/runs/{run_id}/jobs")
    def create_job(run_id: str, payload: JobIn):
        require_run(run_id)
        try:
            return present_job(jobs.submit(run_id, payload.text, payload.request_id))
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

    # ---------------------------------------------------------------- static --

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(str(WEB_DIR / "index.html"))

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
    print(f"budget cap: {settings.budget_limit_usd:.2f} USD (estimated, all runs)")
    print(f"listening : http://{args.host}:{args.port}  (loopback only)")
    uvicorn.run(build_app(settings), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
