"""Read-only projection of an independent learning run.

The event log and existing observation records remain the sources of truth. A
learning run never creates a production stage merely to display progress.
"""
from __future__ import annotations

import hashlib
import json
import copy

from .image_analysis import candidates
from .learning_assets import run_material_resource_id
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


def model_finding_observation(candidate: dict, index: int) -> dict:
    """Project observed modalities from saved input, never from an agent claim."""
    audiovisual = candidate.get('audiovisual')
    if isinstance(audiovisual, dict):
        coverage = copy.deepcopy(audiovisual)
        heard_audio = bool(coverage.get('audio_sent') and coverage.get('audio_ranges'))
    else:
        sampling = candidate.get('sampling') or candidate.get('video_input')
        coverage = {'mode': 'visual_sample', 'sampling': copy.deepcopy(sampling),
                    'audio_sent': False, 'audio_ranges': []}
        heard_audio = False
    timeline = next((row for row in candidate.get('timeline', [])
                     if row.get('finding_index') == index), None)
    return {
        'id': f"{candidate['analysis_id']}/{index}",
        'source_ref': f"analysis:{candidate['analysis_id']}",
        'media_sha256': candidate.get('sha256'),
        'time_range': ([timeline['start_seconds'], timeline['end_seconds']]
                       if timeline and 'start_seconds' in timeline and 'end_seconds' in timeline else None),
        'observer_type': 'model',
        'observation_kind': 'model_audiovisual' if heard_audio else 'model_visual_sample',
        'content': candidate['findings'][index], 'coverage': coverage,
        'limitations': copy.deepcopy(candidate.get('limitations') or []),
        'review_state': 'model_unreviewed',
    }


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
        "resource_id": run_material_resource_id(run_id, row["name"]),
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
            "coverage": (("模型声画观察，仅在登记音轨范围内；逐帧准确性与市场效果未经核验。"
                          if model_finding_observation(candidate, 0)['observation_kind'] == 'model_audiovisual'
                          else "模型只观察画面采样，未听音轨；完整动作与市场效果未经核验。")
                         if candidate and candidate.get('findings') else "尚无可读取的分析观察。"),
            "limitations": candidate.get("limitations", []) if candidate else [],
        })
        if not candidate or task.get("status") == "stale":
            continue
        for index, finding in enumerate(candidate.get("findings") or []):
            observations.append(model_finding_observation(candidate, index))
    phase = session_phase(store, learning, run_id, events=events, chat=chat)
    return {
        "run": run.to_dict(), **phase,
        "sources": sources, "observations": observations,
        "discussion": chat,
    }


def agent_session_context(store, learning, run_id: str, *, cursor=0, limit=40,
                          analysis_id=None, observation_ids=None) -> dict:
    """Bounded model view; human proposal payloads and full-library diffs stay private."""
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 40:
        raise ValueError('Invalid learning observation page')
    if analysis_id is not None and (not isinstance(analysis_id, str) or not analysis_id):
        raise ValueError('Invalid analysis ID')
    if observation_ids is not None and (not isinstance(observation_ids, list) or
            not 1 <= len(observation_ids) <= 40 or
            any(not isinstance(value, str) or not value for value in observation_ids)):
        raise ValueError('Observation IDs must be a nonempty list of at most 40')
    if analysis_id is not None and observation_ids is not None:
        raise ValueError('Choose analysis ID or observation IDs, not both')
    human = session_snapshot(store, learning, run_id)
    observations = human['observations']
    if analysis_id is not None:
        observations = [row for row in observations if row['source_ref'] == f'analysis:{analysis_id}']
        if not observations:
            raise ValueError('Unknown analysis ID in this learning Run')
    if observation_ids is not None:
        by_id = {row['id']: row for row in observations}
        if any(ident not in by_id for ident in observation_ids):
            raise ValueError('Unknown observation ID in this learning Run')
        observations = [by_id[ident] for ident in observation_ids]
    total = len(observations)
    if cursor > total:
        raise ValueError('Learning observation cursor is out of range')
    page = observations[cursor:cursor + limit]
    next_cursor = cursor + len(page) if cursor + len(page) < total else None
    proposals = [{'id': row['id'], 'type': row['type'], 'status': row['status'],
                  'no_change': row.get('no_change', False)} for row in human['proposals'][-10:]]
    return {'run': human['run'], 'status': human['status'], 'discussion_sha256': human['discussion_sha256'],
            'sources': human['sources'], 'observations': page, 'observation_total': total,
            'truncated': total > len(page), 'next_cursor': next_cursor,
            'proposals': proposals, 'discussion': human['discussion'][-20:],
            'discussion_truncated': len(human['discussion']) > 20}
