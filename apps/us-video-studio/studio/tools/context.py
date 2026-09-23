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
        "image_analysis": {"execution": "creative request via observe_images", "agent_can_execute": role == 'orchestrator',
                           "accepted_protocols": sorted(VISION_PROVIDERS), "max_bytes": MAX_IMAGE_BYTES,
                           "scope": "1–12 project images per conversation batch via Beijing Omni", "model_visual_quality_verified": False,
                           "ui_summary": "主制片可按任务观察项目图片；是否可用取决于本机连接和实际图片。模型观察会注明来源，不能代替人工确认。",
                           "notice": "主制片在创作范围内用observe_images实际看图，模型观察标明来源供团队策划，不冒充人工确认；仅列文件不算看图。原手动候选入口继续保留。"},
        "video_understanding": {"agent_can_execute": False, "user_command": "video-analysis",
                                "coverage": "up to 90 seconds; Beijing Omni receives a video file and available audio; other vision routes receive up to 12 frames with no audio input",
                                "ui_summary": "素材区可显式分析视频：百炼北京连接支持有限声画输入，其他视觉连接只分析抽帧。实际可用性与理解质量需逐次检查。",
                                "notice": "素材页百炼北京支持声画分析，沿用 Wan 凭据，需检查本机连接是否就绪；结果选择后进入证据。模型采样和时间估计不保证逐帧验收，角色自身仍只读取观察记录。",
                                "model_audiovisual_quality_verified": False},
        "reference_video": {"agent_can_execute": role == 'orchestrator', "user_command": "observe_reference",
                            "notice": "明确要求分析/复刻时主制片可调用受限参考观察，一轮最多一次，已有分析复用；只分析结束本轮。裸链接先问用途，其他角色只读指定观察。两种来源在对话中直接提供，不要求用途表；生成审批仍不开放。"},
        "production": {"image": "Codex image via user generation command",
                       "video": "wan3.0-video; single approved first frame; 2–30 seconds; 480P/720P/1080P",
                       "ui_summary": "图片与视频生成需在制作工作区分别核对准确输入并确认；主制片只准备方案。",
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
