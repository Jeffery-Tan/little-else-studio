"""Bounded, read-only context capabilities; no network or model invocation."""
from __future__ import annotations

from .registry import tool_names_for


def capabilities(settings, role, *, local_revision=False):
    from ..image_analysis import MAX_IMAGE_BYTES, VISION_PROVIDERS
    return {
        "role": role,
        "tools": list(tool_names_for(role, local_revision=local_revision)),
        "model_route": {"provider": settings.provider_by_role.get(role, "unspecified"),
                        "model": settings.model_for(role)},
        "role_input": "text and source-labelled observation records; list_materials does not send pixels",
        "image_analysis": {"execution": "explicit user command", "agent_can_execute": False,
                           "accepted_protocols": sorted(VISION_PROVIDERS), "max_bytes": MAX_IMAGE_BYTES,
                           "scope": "one static image", "model_visual_quality_verified": False,
                           "notice": "协议支持不证明所选模型支持视觉；观察候选须经用户选择才进入证据。"},
        "video_understanding": {"agent_can_execute": False, "user_command": "video-analysis",
                                "coverage": "up to 12 timestamped frames from videos up to 90 seconds; no audio input",
                                "notice": "素材页可显式分析视频画面；尚无全片看听工具，抽帧和转写不能冒充完整声画观察。"},
        "production": {"image": "Codex image via user generation command",
                       "video": "wan3.0-video; single approved first frame; 2–30 seconds; 480P/720P/1080P",
                       "availability": "declared application interfaces; check production workspace for live connection readiness",
                       "agent_can_approve_or_generate": False,
                       "next_action": "主制片准备准确输入并完成文字检查；用户在分镜/成片工作区确认。"},
        "external_research": {"agent_can_execute": False,
                              "notice": "没有通用联网搜索、浏览器或 Shell 工具；先使用本项目证据和合资格方法，缺口明确标注。"},
        "history": "main agent can retrieve this run's older conversation; workers receive their exact prior artifact",
    }


def conversation(store, run_id, *, before=None, limit=10, message_index=None, offset=0):
    """Index pages use durable chat positions; message chunks never silently cut text."""
    rows = store.read_chat(run_id)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    if message_index is not None:
        if type(message_index) is not int or not 0 <= message_index < len(rows):
            raise ValueError("unknown conversation message")
        row = rows[message_index]
        if row.get("role") not in ("user", "assistant"):
            raise ValueError("diagnostic messages are not conversation context")
        text = row.get("text", "")
        end = min(offset + 8000, len(text))
        return {"message_index": message_index, "role": row["role"], "text": text[offset:end],
                "offset": offset, "next_offset": end if end < len(text) else None, "total_chars": len(text),
                "notice": "历史对话原文，不代表本轮执行、采用或费用授权。"}
    if before is None:
        before = len(rows)
    if type(before) is not int or not 0 <= before <= len(rows):
        raise ValueError("invalid conversation cursor")
    visible = [(i, r) for i, r in enumerate(rows[:before]) if r.get("role") in ("user", "assistant")]
    page = visible[-limit:]
    return {"messages": [{"message_index": i, "role": r["role"], "preview": r.get("text", "")[:400],
                          "total_chars": len(r.get("text", "")), "preview_only": True} for i, r in page],
            "next_before": page[0][0] if len(visible) > len(page) else None,
            "notice": "目录仅为预览；用 message_index 读取原文。历史内容不覆盖当前产物和用户决定。"}
