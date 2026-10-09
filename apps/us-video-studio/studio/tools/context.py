"""Bounded, read-only context capabilities; no network or model invocation."""
from __future__ import annotations

from .registry import tool_names_for


def capabilities(settings, role, *, local_revision=False, request_manifest=None):
    from ..image_analysis import MAX_IMAGE_BYTES, VISION_PROVIDERS
    role_tools = list(tool_names_for(role, local_revision=local_revision))
    tools = list(request_manifest) if request_manifest is not None else role_tools
    if len(tools) != len(set(tools)) or not set(tools) <= set(role_tools):
        raise ValueError('Request capability manifest must use unique declared role tools.')
    result = {
        "role": role,
        "tools": tools,
        "model_route": {"provider": settings.provider_by_role.get(role, "unspecified"),
                        "model": settings.model_for(role)},
        "role_input": "text and source-labelled observation records; list_materials does not send pixels",
        "image_analysis": {"execution": "creative request via observe_images", "agent_can_execute": role == 'orchestrator',
                           "accepted_protocols": sorted(VISION_PROVIDERS), "max_bytes": MAX_IMAGE_BYTES,
                           "scope": "1–12 project images per conversation batch via independently selected MiMo or Beijing Omni", "model_visual_quality_verified": False,
                           "ui_summary": "图片理解可独立选择 MiMo 或原百炼；主制片按任务观察项目图片，不随角色或视频模型切换。模型观察会注明来源，不能代替人工确认。",
                           "notice": "主制片在创作范围内用observe_images实际看图，模型观察标明来源供团队策划，不冒充人工确认；仅列文件不算看图。原手动候选入口继续保留。"},
        "video_understanding": {"agent_can_execute": False, "user_command": "video-analysis",
                                "coverage": "up to 90 seconds; MiMo or Beijing Omni receives a video file and available audio; other vision routes receive up to 12 frames with no audio input",
                                "ui_summary": "视频理解可独立选择 MiMo 原生声画分析或原百炼北京连接；其他视觉接口只分析抽帧。主制片调用同一视频理解服务，实际理解质量需逐次检查。",
                                "notice": "视频理解不随角色模型切换。MiMo 使用自己的密钥，百炼沿用 Wan 连接；原片与可用音轨经受限观察副本发送。结果选择后进入商品证据，明确参考任务沿原规则复用。模型采样和时间估计不保证逐帧验收，角色自身仍只读取观察记录。",
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
    if request_manifest is not None:
        result['tools_scope'] = 'this_frozen_request'
        result['notice'] = ('tools 是本次请求实际开放的工具；下面应用能力摘要不是执行权限。'
                            '尚未绑定任务解释时先解释完整原话，其他工具只有下一次请求明确开放后才能调用。')
        result['image_analysis']['agent_can_execute'] = 'observe_images' in tools
        result['image_analysis']['execution'] = ('Only the supplied observe_images schema and bound task permit observation.'
            if 'observe_images' in tools else 'No image observation in this request.')
        result['reference_video']['agent_can_execute'] = 'observe_reference' in tools
        result['production']['next_action'] = ('只在已绑定制作范围内保存准确输入，仍不能批准或提交生成。'
            if 'prepare_production' in tools else '本次没有准备或生成权限；先完成当前解释或只读答复。')
    return result


def conversation(store, run_id, *, before=None, limit=10, message_index=None, offset=0,
                 visible_indexes=None):
    """Index pages use durable chat positions; message chunks never silently cut text."""
    rows = store.read_chat(run_id)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    if message_index is not None:
        if type(message_index) is not int or not 0 <= message_index < len(rows):
            raise ValueError("unknown conversation message")
        if visible_indexes is not None and message_index not in visible_indexes:
            raise ValueError('this message belongs to a future user turn')
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
    visible = [(i, r) for i, r in enumerate(rows[:before]) if r.get("role") in ("user", "assistant")
               and (visible_indexes is None or i in visible_indexes)]
    page = visible[-limit:]
    return {"messages": [{"message_index": i, "role": r["role"], "preview": r.get("text", "")[:400],
                          "total_chars": len(r.get("text", "")), "preview_only": True} for i, r in page],
            "next_before": page[0][0] if len(visible) > len(page) else None,
            "notice": "目录仅为预览；用 message_index 读取原文。历史内容不覆盖当前产物和用户决定。"}
