"""Material upload, read and human observation HTTP routes."""
from __future__ import annotations
import hashlib
from typing import Any
from fastapi import File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from .http_contracts import MaterialObservationIn
from .material_observations import material_workspace, save_human_observation, ObservationError, ObservationConflict
from . import materials


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



def install_material_routes(app, store, jobs, require_run):
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
        if path is None or materials.kind_of(name) == 'other':
            raise HTTPException(404, "no such material")
        # No `filename=`, so FileResponse does not add a content-disposition
        # header and the browser renders an image inline instead of downloading
        # it. The allowlist in `materials` is what makes the extension-inferred
        # content type safe to serve.
        return FileResponse(str(path), headers={'X-Content-Type-Options': 'nosniff'})

    @app.post("/api/runs/{run_id}/materials")
    def material_upload(run_id: str, file: UploadFile = File(...), retry_sha256: str | None = None) -> dict[str, Any]:
        """Put one file into `materials/`.

        The third write entry point. It appends a ``material_uploaded`` event
        (so the evidence generation advances) but still produces no artifact
        and cannot approve anything — the two gates in `CLAUDE.md` red line 2
        are exactly as they were. What it can do is bounded by
        `studio.materials`, and every bound is a test.
        """
        require_run(run_id)
        try:
            with jobs.lock:
                if jobs.busy(run_id):
                    raise HTTPException(409, '当前制作仍在运行，请停止或等待后再修改素材。')
                data = _read_capped(file)
                if retry_sha256 is not None:
                    if hashlib.sha256(data).hexdigest() != retry_sha256:
                        raise HTTPException(409, '附件与原发送内容不一致，请重新选择原文件。')
                    existing = materials.resolve(store, run_id, file.filename or '')
                    if existing is not None and hashlib.sha256(existing.read_bytes()).hexdigest() == retry_sha256:
                        return {'name': file.filename, 'already_saved': True}
                return materials.accept_upload(store, run_id, file.filename or "", data)
        except materials.MaterialError as exc:
            # The message is written for Jeffery, not for a log — it says which
            # rule the file broke and, where there is one, what to do instead.
            raise HTTPException(400, str(exc)) from exc
