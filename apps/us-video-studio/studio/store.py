"""On-disk store: atomic writes, immutable artifacts, append-only logs.

Layout under ``<data_root>/``::

    config.json                     settings (outside the model's reach)
    runs/<run-id>/
        run.json                    created once, never rewritten
        events.jsonl                append-only; the pipeline state is derived from this
        artifacts/<stage>-v<n>.md   immutable
        artifacts/<stage>-v<n>.json sidecar: fingerprint, evidence, limitations
        reviews/<stage>-v<n>-r<k>.json
        approvals.jsonl             append-only; written only by the server
        ledger.jsonl                append-only; one row per model call

Nothing here talks to the network. Every operation is testable offline.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from functools import wraps
from pathlib import Path
from typing import Any, Iterable

from .models import (
    Artifact,
    Approval,
    LedgerEntry,
    PipelineError,
    Run,
    Review,
    STAGE_SPEC,
    STAGES,
    MARKET,
    utcnow,
    valid_run_id,
)

_RUN_LOCKS: dict[str, Any] = {}
_RUN_LOCKS_GUARD = threading.Lock()


def serialized_run(method):
    """Serialize read/check/write transitions in the local single-process service.

    Shared by Store and Pipeline instances pointing at the same run. This is
    intentionally not a claim of cross-process or distributed coordination.
    """
    @wraps(method)
    def guarded(self, run_id, *args, **kwargs):
        store = getattr(self, "store", self)
        with store.run_lock(run_id):
            return method(self, run_id, *args, **kwargs)
    return guarded


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def artifact_digest(content: str, *, stage: str, version: int, summary: str,
                    evidence: list[str], limitations: list[str],
                    inputs: dict[str, str], structured: dict[str, Any]) -> str:
    """Bind everything reviewed or displayed, including the exact version.

    Metadata such as creation time and author does not affect creative content.
    Existing body-v1 records are never rewritten or silently upgraded.
    """
    return sha256_text(json.dumps({
        "algorithm": "artifact-v2", "stage": stage, "version": version,
        "content": content, "summary": summary, "evidence": evidence,
        "limitations": limitations, "inputs": inputs, "structured": structured,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then ``os.replace``.

    Same directory matters: ``os.replace`` is atomic within a volume, and a
    half-written artifact must never be observable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                # A corrupt line is a real problem; do not skip it silently.
                raise PipelineError(
                    "store", f"{path.name} line {lineno} is not valid JSON: {exc}"
                ) from exc
    return out


class Store:
    def __init__(self, data_root: Path | str):
        self.root = Path(data_root)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)

    def run_lock(self, run_id: str):
        key = os.path.normcase(str(self.run_dir(run_id).resolve()))
        with _RUN_LOCKS_GUARD:
            return _RUN_LOCKS.setdefault(key, threading.RLock())

    # ---------------------------------------------------------------- runs --

    def run_dir(self, run_id: str) -> Path:
        if not valid_run_id(run_id):
            raise PipelineError("store", f"invalid run id: {run_id!r}")
        return self.root / "runs" / run_id

    def create_run(self, run_id: str, title: str, brief: str, market: str = MARKET) -> Run:
        if market != MARKET:
            raise PipelineError(
                "market",
                f"this studio is US-only; refusing to open a run for market {market!r}",
            )
        run_dir = self.run_dir(run_id)
        run_file = run_dir / "run.json"
        if run_file.exists():
            raise PipelineError("store", f"run already exists: {run_id}")
        run = Run(
            run_id=run_id,
            title=title.strip(),
            brief=brief.strip(),
            market=market,
            created_at=utcnow(),
        )
        _atomic_write_text(
            run_file, json.dumps(run.to_dict(), ensure_ascii=False, indent=2) + "\n"
        )
        return run

    def read_run(self, run_id: str) -> Run:
        path = self.run_dir(run_id) / "run.json"
        if not path.is_file():
            raise PipelineError("store", f"no such run: {run_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return Run(**data)

    def list_runs(self) -> list[Run]:
        out: list[Run] = []
        for entry in sorted((self.root / "runs").iterdir()):
            run_file = entry / "run.json"
            if run_file.is_file():
                out.append(Run(**json.loads(run_file.read_text(encoding="utf-8"))))
        return out

    # -------------------------------------------------------------- events --

    def append_event(self, run_id: str, kind: str, **fields: Any) -> dict[str, Any]:
        path = self.run_dir(run_id) / "events.jsonl"
        existing = _read_jsonl(path)
        record = {"seq": len(existing) + 1, "at": utcnow(), "kind": kind, **fields}
        _append_jsonl(path, record)
        return record

    def read_events(self, run_id: str) -> list[dict[str, Any]]:
        return _read_jsonl(self.run_dir(run_id) / "events.jsonl")

    # ----------------------------------------------------------- artifacts --

    def _artifact_paths(self, run_id: str, stage: str, version: int) -> tuple[Path, Path]:
        base = self.run_dir(run_id) / "artifacts"
        return base / f"{stage}-v{version}.md", base / f"{stage}-v{version}.json"

    @serialized_run
    def write_artifact(
        self,
        run_id: str,
        stage: str,
        version: int,
        content: str,
        *,
        summary: str,
        author_role: str,
        attempt: str,
        evidence: Iterable[str] = (),
        limitations: Iterable[str] = (),
        inputs: dict[str, str] | None = None,
        structured: dict[str, Any] | None = None,
    ) -> Artifact:
        """Write a new artifact. Refuses to touch an existing version."""
        if stage not in STAGE_SPEC:
            raise PipelineError("store", f"unknown stage: {stage!r}")
        if version < 1:
            raise PipelineError("store", f"version must be >= 1, got {version}")
        if not content.strip():
            raise PipelineError("store", "refusing to write an empty artifact")

        md_path, meta_path = self._artifact_paths(run_id, stage, version)
        if md_path.exists() or meta_path.exists():
            raise PipelineError(
                "immutable",
                f"{stage}-v{version} already exists; artifacts are immutable, "
                f"write the next version instead",
            )

        evidence = [str(e) for e in evidence]
        limitations = [str(x) for x in limitations]
        artifact = Artifact(
            stage=stage,
            version=version,
            filename=md_path.name,
            sha256=artifact_digest(content, stage=stage, version=version,
                                   summary=summary.strip(), evidence=evidence,
                                   limitations=limitations, inputs=dict(inputs or {}),
                                   structured=dict(structured or {})),
            digest_algorithm="artifact-v2",
            summary=summary.strip(),
            evidence=[str(e) for e in evidence],
            limitations=[str(x) for x in limitations],
            author_role=author_role,
            attempt=attempt,
            created_at=utcnow(),
            inputs=dict(inputs or {}),
            # Stored as given. Validating the shape is the pipeline's job — this
            # layer persists, and a store that second-guessed its callers would
            # make the stage semantics live in two places.
            structured=dict(structured or {}),
        )
        _atomic_write_text(md_path, content)
        try:
            _atomic_write_text(
                meta_path,
                json.dumps(artifact.to_dict(), ensure_ascii=False, indent=2) + "\n",
            )
        except Exception:
            # Never leave a content file without its fingerprint sidecar.
            md_path.unlink(missing_ok=True)
            raise
        return artifact

    def read_artifact_text(self, run_id: str, stage: str, version: int) -> str:
        md_path, _ = self._artifact_paths(run_id, stage, version)
        if not md_path.is_file():
            raise PipelineError("store", f"no such artifact: {stage}-v{version}")
        # Digest the stored text exactly; universal-newline translation would
        # make a legitimate CRLF artifact fail its own fingerprint.
        return md_path.read_bytes().decode("utf-8")

    def read_artifact(self, run_id: str, stage: str, version: int) -> Artifact:
        _, meta_path = self._artifact_paths(run_id, stage, version)
        if not meta_path.is_file():
            raise PipelineError("store", f"no such artifact record: {stage}-v{version}")
        return Artifact(**json.loads(meta_path.read_text(encoding="utf-8")))

    def verify_artifact(self, run_id: str, stage: str, version: int) -> bool:
        """Recompute the fingerprint from the file on disk.

        A tampered or truncated artifact fails here, which invalidates every
        review and approval that pointed at the recorded fingerprint.
        """
        artifact = self.read_artifact(run_id, stage, version)
        content = self.read_artifact_text(run_id, stage, version)
        if artifact.digest_algorithm == "body-v1":
            current = sha256_text(content)
        elif artifact.digest_algorithm == "artifact-v2":
            current = artifact_digest(content, stage=artifact.stage, version=artifact.version,
                                      summary=artifact.summary, evidence=artifact.evidence,
                                      limitations=artifact.limitations, inputs=artifact.inputs,
                                      structured=artifact.structured)
        else:
            return False
        return current == artifact.sha256

    def list_artifacts(self, run_id: str, stage: str | None = None) -> list[Artifact]:
        base = self.run_dir(run_id) / "artifacts"
        if not base.is_dir():
            return []
        out: list[Artifact] = []
        for meta_path in sorted(base.glob("*.json")):
            artifact = Artifact(**json.loads(meta_path.read_text(encoding="utf-8")))
            if stage is None or artifact.stage == stage:
                out.append(artifact)
        # Pipeline order, not alphabetical: 'direction' must not sort after 'writing'.
        out.sort(key=lambda a: (STAGES.index(a.stage), a.version))
        return out

    def latest_artifact(self, run_id: str, stage: str) -> Artifact | None:
        versions = self.list_artifacts(run_id, stage)
        return versions[-1] if versions else None

    def next_version(self, run_id: str, stage: str) -> int:
        versions = self.list_artifacts(run_id, stage)
        return (versions[-1].version + 1) if versions else 1

    # ------------------------------------------------------------- reviews --

    def write_review(self, run_id: str, review: Review) -> Path:
        base = self.run_dir(run_id) / "reviews"
        base.mkdir(parents=True, exist_ok=True)
        n = len(list(base.glob(f"{review.stage}-*.json"))) + 1
        path = base / f"{review.stage}-r{n}.json"
        if path.exists():
            raise PipelineError("immutable", f"review record already exists: {path.name}")
        _atomic_write_text(
            path, json.dumps(review.to_dict(), ensure_ascii=False, indent=2) + "\n"
        )
        return path

    def list_reviews(self, run_id: str, stage: str | None = None) -> list[Review]:
        base = self.run_dir(run_id) / "reviews"
        if not base.is_dir():
            return []
        out = [
            Review(**json.loads(p.read_text(encoding="utf-8")))
            for p in sorted(base.glob("*.json"))
        ]
        return [r for r in out if stage is None or r.stage == stage]

    # ----------------------------------------------------------- approvals --

    def append_approval(self, run_id: str, approval: Approval) -> None:
        """Only the server calls this, and only after a UI click with a valid nonce."""
        _append_jsonl(
            self.run_dir(run_id) / "approvals.jsonl", approval.to_dict()
        )

    def list_approvals(self, run_id: str, stage: str | None = None) -> list[Approval]:
        rows = _read_jsonl(self.run_dir(run_id) / "approvals.jsonl")
        out = [Approval(**r) for r in rows]
        return [a for a in out if stage is None or a.stage == stage]

    # -------------------------------------------------------------- ledger --

    def append_ledger(self, run_id: str, entry: LedgerEntry) -> None:
        _append_jsonl(self.run_dir(run_id) / "ledger.jsonl", entry.to_dict())

    def read_ledger(self, run_id: str) -> list[LedgerEntry]:
        return [
            LedgerEntry(**r) for r in _read_jsonl(self.run_dir(run_id) / "ledger.jsonl")
        ]

    # ---------------------------------------------------------------- chat --

    def append_chat(self, run_id: str, role: str, text: str) -> None:
        """Append a turn to the run's conversation log.

        Display history, not model context. The orchestrator rebuilds a short
        window from this each turn and re-reads the live state, so a long tool
        history never accumulates into the prompt.
        """
        _append_jsonl(
            self.run_dir(run_id) / "chat.jsonl",
            {"at": utcnow(), "role": role, "text": text},
        )

    def read_chat(self, run_id: str) -> list[dict[str, Any]]:
        return _read_jsonl(self.run_dir(run_id) / "chat.jsonl")

    # ------------------------------------------------------------ lifecycle --

    def archive_run(self, run_id: str) -> Path:
        """Mark a run out of the way. Moves files; deletes nothing."""
        src = self.run_dir(run_id)
        if not src.is_dir():
            raise PipelineError("store", f"no such run: {run_id}")
        dest = self.root / "archive" / run_id
        if dest.exists():
            raise PipelineError("store", f"archive target already exists: {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return dest
