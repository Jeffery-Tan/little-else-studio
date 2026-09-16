"""Local HTTP server: REST for state, SSE for the conversation.

Binds to loopback only. There is no authentication because there is no network
exposure — the whole point is that it runs on Jeffery's machine and nowhere else.

The one security-relevant thing here is the approval nonce. The server mints it,
returns it to the requesting page, and validates it on the way back. It is never
logged, never written to the run directory, and never included in a model
request. ``POST /decision`` is the only route that can write an approval, and it
requires a nonce that the caller could only have got from ``POST /nonce``.
"""
from __future__ import annotations

import argparse
import json
import queue
import threading
from pathlib import Path
from typing import Any, Callable, Iterator

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agents.client import AnthropicCompatClient
from .agents.orchestrator import Orchestrator
from .config import Settings, credentials, load_settings
from .ledger import Ledger
from .models import BudgetExceeded, PipelineError, STAGES, STAGE_SPEC, valid_run_id
from .pipeline import Pipeline
from .store import Store
from . import materials
from . import vocabulary
from .structured import open_questions, view
from .timeline import build_timeline

WEB_DIR = Path(__file__).parent / "web"


class RunIn(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=1, max_length=20000)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class DecisionIn(BaseModel):
    stage: str
    nonce: str
    decision: str
    note: str = ""
    override_reason: str = ""


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
) -> FastAPI:
    """Build the app.

    ``client_factory`` exists so tests can drive the whole HTTP surface with a
    scripted model and spend nothing. In normal use it is None and each turn
    builds a real client from the environment.
    """
    settings = settings or load_settings()
    store = Store(settings.data_root)
    pipeline = Pipeline(store)
    ledger = Ledger(store, settings)

    app = FastAPI(title="US video studio", docs_url=None, redoc_url=None)

    def make_client() -> Any:
        if client_factory is not None:
            return client_factory()
        # Read at call time so a rotated credential is picked up on restart
        # without the value ever passing through persisted state.
        base_url, token = credentials()
        return AnthropicCompatClient(base_url, token, max_tokens=settings.max_tokens)

    def require_run(run_id: str) -> None:
        if not valid_run_id(run_id):
            raise HTTPException(400, "invalid run id")
        try:
            store.read_run(run_id)
        except PipelineError as exc:
            raise HTTPException(404, str(exc)) from exc

    #: Run ids with a turn in flight. One turn at a time per run.
    #:
    #: ``POST /message`` spawns a daemon thread, and ``Store.append_event``
    #: numbers a record by reading the whole log and adding one. Two turns
    #: interleaving there write duplicate ``seq`` values, and
    #: ``Pipeline._attempts`` reconstructs attempts by scanning that log for
    #: ``{stage}#{number}`` — so duplicates corrupt attempt state rather than
    #: merely looking untidy. Refusing the second message is the smallest fix
    #: that cannot be wrong; serialising the writes would be the other, and the
    #: turn is long enough (minutes) that queueing it would just hide the
    #: problem from whoever is clicking.
    _in_flight: set[str] = set()
    _in_flight_lock = threading.Lock()

    # ------------------------------------------------------------------ runs --

    @app.get("/api/runs")
    def list_runs() -> list[dict[str, Any]]:
        out = []
        for run in store.list_runs():
            totals = ledger.totals(run.run_id)
            out.append({**run.to_dict(), "spend": totals.to_dict()})
        return sorted(out, key=lambda r: r["created_at"], reverse=True)

    @app.post("/api/runs")
    def create_run(payload: RunIn) -> dict[str, Any]:
        try:
            run = store.create_run(payload.run_id, payload.title, payload.brief)
        except PipelineError as exc:
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
            "remaining_usd": round(ledger.remaining_usd(), 4),
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
            "rows": [r.to_dict() for r in store.read_ledger(run_id)],
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
            data = _read_capped(file)
            return materials.accept_upload(store, run_id, file.filename or "", data)
        except materials.MaterialError as exc:
            # The message is written for Jeffery, not for a log — it says which
            # rule the file broke and, where there is one, what to do instead.
            raise HTTPException(400, str(exc)) from exc

    # -------------------------------------------------------------- approvals --

    @app.post("/api/runs/{run_id}/nonce")
    def issue_nonce(run_id: str, stage: str) -> dict[str, str]:
        """Mint the nonce that makes a click a click.

        Returned to this caller and nowhere else. Nothing writes it to disk.
        """
        require_run(run_id)
        if stage not in STAGE_SPEC or not STAGE_SPEC[stage].gated:
            raise HTTPException(400, f"{stage} is not a gated stage")
        try:
            nonce = pipeline.issue_nonce(run_id, stage)
        except PipelineError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"stage": stage, "nonce": nonce}

    @app.post("/api/runs/{run_id}/decision")
    def decide(run_id: str, payload: DecisionIn) -> dict[str, Any]:
        require_run(run_id)
        try:
            approval = pipeline.record_approval(
                run_id,
                payload.stage,
                nonce=payload.nonce,
                decision=payload.decision,
                note=payload.note,
                override_reason=payload.override_reason,
            )
        except PipelineError as exc:
            raise HTTPException(400, str(exc)) from exc
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

    @app.post("/api/runs/{run_id}/message")
    def send_message(run_id: str, payload: MessageIn) -> StreamingResponse:
        require_run(run_id)
        with _in_flight_lock:
            if run_id in _in_flight:
                raise HTTPException(409, "这一轮还没跑完，等它结束再发下一条。")
            _in_flight.add(run_id)
        events: queue.Queue[dict[str, Any] | None] = queue.Queue()

        def worker() -> None:
            try:
                orchestrator = Orchestrator(
                    store, pipeline, settings, make_client(), ledger, run_id,
                    on_event=events.put,
                )
                turn = orchestrator.send(payload.text)
                events.put(
                    {
                        "type": "done",
                        "text": turn.text,
                        "rounds": turn.rounds,
                        "stopped_because": turn.stopped_because,
                        "tools": turn.used_tools,
                    }
                )
            except BudgetExceeded as exc:
                events.put({"type": "blocked", "message": str(exc)})
            except PipelineError as exc:
                events.put({"type": "error", "message": str(exc)})
            except Exception as exc:  # the UI must show a failure, not hang
                events.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                with _in_flight_lock:
                    _in_flight.discard(run_id)
                events.put(None)

        threading.Thread(target=worker, daemon=True).start()
        return StreamingResponse(
            sse(iter(events.get, None)),
            media_type="text/event-stream",
            headers={"cache-control": "no-store", "x-accel-buffering": "no"},
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

    import uvicorn

    settings = load_settings()
    print(f"data root : {settings.data_root}")
    print(f"stages    : {' -> '.join(STAGES)}")
    print(f"budget cap: {settings.budget_limit_usd:.2f} USD (estimated, all runs)")
    print(f"listening : http://{args.host}:{args.port}  (loopback only)")
    uvicorn.run(build_app(settings), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
