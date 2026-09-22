"""User-authored, byte-bound material notes in the existing evidence manifest.

No vision request is made. A human report is neither verified product truth nor
an approval. Existing records remain append-only; the manifest's byte revision
guards concurrent editors and is already part of pipeline input fingerprints.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .evidence import MAX_MANIFEST_BYTES, evidence_snapshot
from .materials import material_url, resolve, kind_of
from .models import utcnow
from .store import Store, _atomic_write_text


class ObservationError(ValueError):
    pass


class ObservationConflict(ObservationError):
    pass


def _manifest(store: Store, run_id: str):
    run = store.run_dir(run_id).resolve()
    path = run / "evidence" / "observations.json"
    if not path.resolve().is_relative_to(run):
        raise ObservationError("素材记录位置无效，未保存。")
    if not path.exists():
        return path, None, {"schema_version": 1, "observations": []}
    if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ObservationError("素材记录无法读取，原记录已保留。")
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("observations"), list):
            raise ValueError()
    except (ValueError, UnicodeError) as exc:
        raise ObservationError("素材记录格式异常，原记录已保留。") from exc
    return path, hashlib.sha256(raw).hexdigest(), data


def material_workspace(store: Store, run_id: str) -> dict[str, Any]:
    with store.run_lock(run_id):
        store.read_run(run_id)
        snapshot = evidence_snapshot(store, run_id)
        items = []
        for material in snapshot["materials"]:
            observations = material["observations"]
            items.append({**material, "url": material_url(run_id, material["name"]),
                          "status": "has_observations" if observations else "uploaded",
                          "status_label": "已记录观察" if observations else
                              "生成素材 · 待验收" if material.get('origin') == 'generated' else "已上传 · 待补充观察"})
        return {"materials": items, "expected_manifest_sha256": snapshot["manifest_sha256"],
                "errors": snapshot["errors"], "automatic_analysis_available": False,
                "notice": ""}


def _text(value: Any, label: str, limit: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ObservationError(f"请填写{label}，最多 {limit} 个字符。")
    return value.strip()


def _lines(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 30:
        raise ObservationError(f"{label}需填写 1 至 30 条。")
    return [_text(line, label) for line in value]


def save_human_observation(store: Store, run_id: str, *, name: str,
                           expected_sha256: str, expected_manifest_sha256: str | None,
                           purpose: str, level: str, coverage: str,
                           findings: list[str], limitations: list[str]) -> dict[str, Any]:
    """Call inside the server job lock, refusing edits during active jobs.

    No caller-controlled observer or approval fields are accepted. All fields
    are untrusted evidence data when later included in role context.
    """
    with store.run_lock(run_id):
        store.read_run(run_id)
        run = store.run_dir(run_id).resolve()
        path = resolve(store, run_id, name)
        if path is None or not path.is_relative_to(run) or kind_of(name) == "other":
            raise ObservationError("找不到这个项目的素材，请重新打开素材面板。")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if not isinstance(expected_sha256, str) or actual != expected_sha256:
            raise ObservationConflict("素材已变化，请重新查看后再保存观察。")
        manifest, revision, document = _manifest(store, run_id)
        if revision != expected_manifest_sha256:
            raise ObservationConflict("素材记录已更新，请重新打开后补充，原记录不会被覆盖。")
        if purpose not in {"product", "reference"}:
            raise ObservationError("请选择商品素材或创意参考。")
        allowed = {"image_observed"} if kind_of(name) == "image" else {"frames_observed", "transcript_only", "audiovisual_review"}
        if level not in allowed:
            raise ObservationError("观察方式与素材类型不匹配。")
        record = {"name": name, "sha256": actual, "level": level,
                  "observer": "human:workspace-user", "source_kind": "human_observation",
                  "method": "工作台用户查看后手动填写；未经模型视觉验证",
                  "purpose": purpose, "coverage": _text(coverage, "观察范围", 500),
                  "observed_at": utcnow(), "findings": _lines(findings, "观察内容"),
                  "limitations": _lines(limitations, "未知或限制")}
        document["observations"].append(record)
        encoded = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        if len(encoded.encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise ObservationError("素材记录达到容量上限，原记录已保留。")
        _atomic_write_text(manifest, encoded)
        # Advances evidence generation so A→B→A on the manifest cannot revive
        # an old approval that only compared content hashes (CLAUDE.md red line 7).
        store.append_event(
            run_id, "observation_saved",
            name=name, sha256=actual, purpose=purpose, level=level,
        )
        return material_workspace(store, run_id)
