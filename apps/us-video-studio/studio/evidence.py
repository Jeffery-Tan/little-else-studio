"""Read-only local evidence, with explicit observation coverage.

Listing or hashing a file never observes its visual/audio contents. Optional
observations are externally supplied records, not claims this model may write.
This module verifies their byte binding, not the truth of the observer's report.
No network provider or paid vision service is silently selected.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .materials import kind_of, materials_dir, resolve
from .store import Store

LEVELS = {"image_observed", "frames_observed", "transcript_only", "audiovisual_review"}
MAX_MANIFEST_BYTES = 2 * 1024 * 1024


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evidence_snapshot(store: Store, run_id: str) -> dict[str, Any]:
    """Stable snapshot used both in context and dependency fingerprints.

    Optional ``evidence/observations.json`` shape::

        {"schema_version":1,"observations":[{"name":"shot.jpg","sha256":"…",
         "level":"image_observed","observer":"human:Jeffery","method":"…",
         "coverage":"whole still","observed_at":"…","findings":["…"],
         "limitations":["audio not applicable"]}]}

    Invalid/stale records are retained as errors and never labelled observed.
    Unsupported files and paths outside the material directory are rejected.
    """
    run = store.run_dir(run_id).resolve()
    base = materials_dir(store, run_id)
    result: dict[str, Any] = {
        "materials": [], "manifest_sha256": None, "errors": [],
        "imported_input": None,
        "reference_discovery": {
            "status": "unavailable",
            "reason": "未配置并授权参考发现或视觉服务；本次上下文只含本地记录，未访问外部参考。",
        },
    }
    imported_sources = run / "evidence" / "content-input-sources.json"
    if imported_sources.exists():
        result["imported_input"] = _input_sources(run, imported_sources, result["errors"])
    if base.exists() and base.resolve().parent != run:
        result["errors"].append("materials directory is outside this run")
        return result
    if base.is_dir():
        for entry in sorted(base.iterdir(), key=lambda p: p.name):
            if not entry.is_file():
                continue
            path = resolve(store, run_id, entry.name)
            kind = kind_of(entry.name)
            if path is None or kind == "other":
                result["errors"].append(f"unsupported or uncontained material: {entry.name}")
                continue
            result["materials"].append({
                "name": entry.name, "kind": kind, "bytes": path.stat().st_size,
                "sha256": _sha(path), "observation_level": "existence_only", "observations": [],
            })
    manifest = run / "evidence" / "observations.json"
    if not manifest.exists():
        return result
    if not manifest.resolve().is_relative_to(run) or not manifest.is_file():
        result["errors"].append("observation manifest is outside this run or is not a file")
        return result
    result["manifest_sha256"] = _sha(manifest)
    if manifest.stat().st_size > MAX_MANIFEST_BYTES:
        result["errors"].append("observation manifest exceeds 2 MiB")
        return result
    try:
        document = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise ValueError("unsupported observation manifest version")
        records = document.get("observations")
        if not isinstance(records, list):
            raise ValueError("observations must be an array")
    except (ValueError, UnicodeError) as exc:
        result["errors"].append(f"invalid observation manifest: {exc}")
        return result
    materials = {item["name"]: item for item in result["materials"]}
    for index, record in enumerate(records):
        try:
            if not isinstance(record, dict):
                raise ValueError("record must be an object")
            material = materials.get(record.get("name"))
            if material is None or record.get("sha256") != material["sha256"]:
                raise ValueError("material missing or bytes changed")
            level = record.get("level")
            if level not in LEVELS:
                raise ValueError("unsupported observation level")
            if (level == "image_observed") != (material["kind"] == "image"):
                raise ValueError("observation level does not match media kind")
            for key in ("observer", "method", "coverage", "observed_at"):
                if not isinstance(record.get(key), str) or not record[key].strip():
                    raise ValueError(f"{key} must describe the external observation")
            for key in ("findings", "limitations"):
                if not isinstance(record.get(key), list) or not all(isinstance(x, str) for x in record[key]):
                    raise ValueError(f"{key} must be an array of strings")
            material["observations"].append({
                key: record[key] for key in ("level", "observer", "method", "coverage", "observed_at", "findings", "limitations")
            })
            # Multiple modes are preserved independently; no numeric rank can
            # promote a transcript to a full audiovisual observation.
            material["observation_level"] = "imported_observations"
        except (ValueError, TypeError) as exc:
            result["errors"].append(f"observation {index + 1}: {exc}")
    return result


def _input_sources(run: Path, manifest: Path, errors: list[str]) -> dict[str, Any]:
    """Re-check explicitly imported source bindings without rewriting history."""
    result: dict[str, Any] = {"manifest_sha256": None, "dependencies": []}
    if not manifest.resolve().is_relative_to(run) or not manifest.is_file():
        errors.append("input source manifest escapes run")
        return result
    result["manifest_sha256"] = _sha(manifest)
    try:
        if manifest.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError("manifest exceeds 2 MiB")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("unsupported source manifest version")
        if not isinstance(data.get("allowed_roots"), list) or not isinstance(data.get("dependencies"), list):
            raise ValueError("allowed roots and dependencies must be arrays")
        roots = [Path(value).resolve() for value in data["allowed_roots"]]
        package = run / "evidence" / "content-input-package.json"
        if not package.resolve().is_relative_to(run) or not package.is_file():
            raise ValueError("imported package missing or escapes run")
        result["package_sha256"] = _sha(package)
        if result["package_sha256"] != data["package_sha256"]:
            errors.append("imported ContentInput bytes changed")
        for dependency in data["dependencies"]:
            if not isinstance(dependency, dict):
                raise ValueError("source dependency must be an object")
            source = Path(dependency["path"]).resolve()
            record = {"path": str(source), "expected_sha256": dependency["sha256"], "actual_sha256": None}
            if not any(source.is_relative_to(root) for root in roots):
                errors.append(f"input source escapes allowed roots: {source.name}")
            elif not source.is_file():
                errors.append(f"input source missing: {source.name}")
            else:
                record["actual_sha256"] = _sha(source)
                if record["actual_sha256"] != dependency["sha256"]:
                    errors.append(f"input source changed: {source.name}")
            result["dependencies"].append(record)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        errors.append(f"invalid imported input sources: {exc}")
    return result


def evidence_fingerprint(store: Store, run_id: str) -> str:
    payload = json.dumps(evidence_snapshot(store, run_id), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
