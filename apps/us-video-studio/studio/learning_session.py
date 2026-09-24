"""Read-only projection of an independent learning run.

The event log and existing observation records remain the sources of truth. A
learning run never creates a production stage merely to display progress.
"""
from __future__ import annotations

import hashlib
import json

from .image_analysis import candidates
from .material_observations import material_workspace
from .reference_video import task_state


def discussion_sha256(chat: list[dict]) -> str:
    """Only user turns invalidate a pending interpretation or delta.

    The agent's reply is saved after its proposal in the same job, so including
    assistant turns would make a freshly proposed delta stale immediately.
    """
    rows = [row for row in chat if row.get("role") == "user"]
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def human_finding_ref(name: str, media_sha256: str, record: dict, index: int) -> str:
    """Bind one reported finding to exact current media and observation bytes.

    The record is the validated projection from ``material_workspace``. Its
    order among other records is deliberately excluded: later append-only
    observations must not change an earlier finding's identity.
    """
    raw = json.dumps(
        {"name": name, "media_sha256": media_sha256, "observation": record},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    record_sha256 = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"material-observation:{record_sha256}#{index}"


def session_phase(store, learning, run_id: str, *, events=None, chat=None, proposals=None) -> dict:
    """Project status without hashing every uploaded video for the run list."""
    events = store.read_events(run_id) if events is None else events
    chat = store.read_chat(run_id) if chat is None else chat
    proposals = learning.proposals(run_id) if proposals is None else proposals
    current_discussion = discussion_sha256(chat)
    projected = []
    for proposal in proposals:
        item = dict(proposal)
        if (item.get("status") == "pending" and item.get("discussion_sha256")
                and item["discussion_sha256"] != current_discussion):
            item["status"] = "stale"
        projected.append(item)
    last_close = next((row for row in reversed(events) if row['kind'] == 'learning_closed'), None)
    resumed = last_close and any(row['seq'] > last_close['seq'] and row['kind'] in {
        'material_uploaded', 'reference_video_started', 'learning_proposed',
        'learning_applied', 'learning_rejected', 'learning_discussion_checkpointed',
        'job_created', 'observation_saved',
    } for row in events)
    if last_close and not resumed:
        status = "closed"
    elif any(p["status"] == "pending" for p in projected):
        status = "pending_review"
    elif applied := next((row for row in reversed(events)
                         if row["kind"] == "learning_applied"), None):
        status = ("no_change" if applied.get("proposal_type") == "delta"
                  and applied.get("before_sha256") == applied.get("after_sha256")
                  else "saved")
    elif chat:
        status = "discussing"
    else:
        status = "collecting"
    labels = {
        "closed": "本次学习已结束", "pending_review": "知识变更待确认",
        "saved": "已保存，待迁移验证", "no_change": "已记录：暂无知识变更",
        "discussing": "讨论中", "collecting": "整理材料",
    }
    return {"status": status, "summary": labels[status],
            "discussion_sha256": current_discussion, "proposals": projected}


def session_snapshot(store, learning, run_id: str) -> dict:
    run = store.read_run(run_id)
    if run.kind != "learning":
        raise ValueError("This run is not an independent learning session")
    events = store.read_events(run_id)
    chat = store.read_chat(run_id)
    material_rows = material_workspace(store, run_id)["materials"]
    tasks = task_state(store, run_id)
    by_analysis = {row.get("analysis_id"): row for row in candidates(store, run_id)}
    sources = [{
        "kind": "uploaded_media", "name": row["name"],
        "media_sha256": row["sha256"], "media_url": row["url"],
        "coverage": "已保存原件；观察范围以逐条记录为准。",
    } for row in material_rows]
    observations = []
    for material in material_rows:
        for record in material["observations"]:
            # Other records may be model-authored, imported, or unclassified.
            # Only the workspace's own user-authored report is shown here as
            # direct user evidence; its contents remain a report, not a
            # verified property or proof of full audiovisual review.
            if (record.get("source_kind") != "human_observation" or
                    record.get("observer") != "human:workspace-user"):
                continue
            for index, finding in enumerate(record["findings"]):
                source_ref = human_finding_ref(
                    material["name"], material["sha256"], record, index,
                )
                observations.append({
                    "id": source_ref, "source_ref": source_ref,
                    "source_name": material["name"],
                    "media_sha256": material["sha256"],
                    "content": finding, "time_range": None,
                    "observer_type": "user", "observer": record["observer"],
                    "observation_kind": "human_material_observation",
                    "level": record["level"], "coverage": record["coverage"],
                    "method": record["method"],
                    "limitations": record["limitations"],
                    "purpose": record.get("purpose"),
                    "observed_at": record["observed_at"],
                    "review_state": "user_reported",
                })
    for task in tasks:
        candidate = by_analysis.get(task.get("analysis_id"))
        name = task.get("name")
        matching = next((m for m in material_rows if m.get("name") == name), None)
        sources.append({
            "kind": "reference_analysis", "reference_id": task["reference_id"],
            "name": name, "source_url": task.get("source_url"),
            "media_sha256": task.get("sha256"),
            "media_url": matching.get("url") if matching else None,
            "analysis_id": task.get("analysis_id"), "status": task["status"],
            "coverage": ("模型声画观察，按提供方采样；逐帧准确性与市场效果未经核验。"
                         if candidate else "尚无可读取的分析观察。"),
            "limitations": candidate.get("limitations", []) if candidate else [],
        })
        if not candidate or task.get("status") == "stale":
            continue
        timeline = candidate.get("timeline") or []
        for index, finding in enumerate(candidate.get("findings") or []):
            span = next((row for row in timeline if row.get("finding_index") == index), None)
            observations.append({
                "id": f"{candidate['analysis_id']}/{index}",
                "source_ref": f"analysis:{candidate['analysis_id']}",
                "media_sha256": candidate.get("sha256"),
                "time_range": ([span["start_seconds"], span["end_seconds"]]
                               if span and "start_seconds" in span and "end_seconds" in span else None),
                "observer_type": "model", "observation_kind": (
                    "model_audiovisual" if candidate.get("audiovisual") else "model_visual_sample"),
                "content": finding,
                "coverage": candidate.get("audiovisual") or candidate.get("video_input") or {},
                "review_state": "model_unreviewed",
            })
    phase = session_phase(store, learning, run_id, events=events, chat=chat)
    return {
        "run": run.to_dict(), **phase,
        "sources": sources, "observations": observations,
        "discussion": chat,
    }
