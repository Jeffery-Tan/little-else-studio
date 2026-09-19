"""Opt-in local ContentInput import; no HTTP file-reader or implicit permissions.

Run with ``python -m studio.input_import --help``. The caller supplies the exact
package and roots it authorizes the importer to read. Shared contract validation
is loaded from the fixed sibling adapter in this repository, never a user path.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

from .pipeline import Pipeline
from .jobs import WriterLease
from .store import Store

MAX_INPUT_BYTES = 2 * 1024 * 1024


def _allowed(path: Path, roots: list[Path]) -> Path:
    resolved = path.resolve()
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ValueError(f"source outside explicitly allowed roots: {path.name}")
    if not resolved.is_file():
        raise ValueError(f"missing source: {path.name}")
    return resolved


def prepare_content_input(path: Path | str, repository_root: Path | str,
                          allowed_roots: list[Path | str]) -> dict[str, Any]:
    """Validate bytes and every source before letting the shared reader open it."""
    roots = [Path(root).resolve() for root in allowed_roots]
    if not roots or any(not root.is_dir() for root in roots):
        raise ValueError("at least one existing allowed root is required")
    repository_root = Path(repository_root).resolve()
    package = _allowed(Path(path), roots)
    if package.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("ContentInput exceeds 2 MiB")
    raw = package.read_bytes()
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("ContentInput must be an object")
    for collection in ("sources", "observations"):
        records = data.get(collection)
        if not isinstance(records, list):
            raise ValueError(f"{collection} must be an array")
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                raise ValueError(f"{collection} requires explicit local source paths")
            source = Path(record["path"])
            _allowed(source if source.is_absolute() else repository_root / source, roots)
    fixed_adapter = Path(__file__).resolve().parents[2] / "video-workbench" / "openmontage" / "lib" / "content_handoff.py"
    spec = importlib.util.spec_from_file_location("_studio_shared_content_contract", fixed_adapter)
    if spec is None or spec.loader is None:
        raise ValueError("shared ContentInput validator unavailable")
    contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contract)
    verified_raw, verified_data, dependencies = contract.input_package(package, repository_root)
    if verified_raw != raw:
        raise ValueError("input changed during validation; retry explicitly")
    for dependency in dependencies:
        _allowed(Path(dependency["path"]), roots)
    return {"raw": raw, "data": verified_data, "dependencies": dependencies,
            "allowed_roots": [str(root) for root in roots],
            "sha256": hashlib.sha256(raw).hexdigest()}


def import_content_input(store: Store, pipeline: Pipeline, run_id: str, *,
                         path: Path | str, repository_root: Path | str,
                         allowed_roots: list[Path | str]):
    """Initialize a new run's intake, preserving source bytes and unknown facts.

    Does not approve anything, import a ContentPlan, or dispatch a model. Refuses
    existing intake/attempts and refuses replacing any pre-existing input files.
    """
    store.read_run(run_id)
    if store.latest_artifact(run_id, "intake") is not None or pipeline.stage_state(run_id, "intake").open_attempt:
        raise ValueError("ContentInput import requires an empty intake; existing work is preserved")
    prepared = prepare_content_input(path, repository_root, allowed_roots)
    evidence_dir = store.run_dir(run_id) / "evidence"
    if evidence_dir.exists() and not evidence_dir.resolve().is_relative_to(store.run_dir(run_id).resolve()):
        raise ValueError("evidence directory escapes run")
    evidence_dir.mkdir(exist_ok=True)
    package = evidence_dir / "content-input-package.json"
    sources = evidence_dir / "content-input-sources.json"
    if package.exists() or sources.exists():
        raise ValueError("input import files already exist; refusing to replace them")
    manifest = {"schema_version": 1, "package_sha256": prepared["sha256"],
                "allowed_roots": prepared["allowed_roots"], "dependencies": prepared["dependencies"]}
    # Exclusive creation prevents overwriting another user's import.
    with package.open("xb") as target:
        target.write(prepared["raw"])
    with sources.open("x", encoding="utf-8", newline="\n") as target:
        target.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    data = prepared["data"]
    content = (
        "# 已校验的 ContentInput 文件快照\n\n"
        f"input_id: {data['input_id']}\n\nsha256: {prepared['sha256']}\n\n"
        "本次只校验文件与来源字节，保留所有事实层级、未知项、观察覆盖和局限。"
        "导入观察是来源报告，不是当前模型亲自看到媒体；授权字段不授予本应用图片、费用或发布权限。\n\n"
        "```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```\n"
    )
    attempt = pipeline.start_attempt(run_id, "intake", actor="local-content-input-import")
    return pipeline.submit(run_id, "intake", attempt.token, content=content,
                           summary=f"ContentInput {data['input_id']}（保留未知及来源）",
                           evidence=[f"input sha256:{prepared['sha256']}"],
                           limitations=["未调用视觉、参考搜索或生成服务；导入不代表任何批准。"],
                           actor="local-content-input-import")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--allow-root", type=Path, action="append", required=True)
    args = parser.parse_args()
    store = Store(args.data_root)
    with WriterLease(store.root):
        artifact = import_content_input(store, Pipeline(store), args.run_id, path=args.path,
                                        repository_root=args.repository_root, allowed_roots=args.allow_root)
    print(json.dumps({"artifact": artifact.key, "sha256": artifact.sha256}, ensure_ascii=False))


if __name__ == "__main__":
    main()
