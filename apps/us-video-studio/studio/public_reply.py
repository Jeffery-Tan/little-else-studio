"""Project a model turn into a truthful creator-facing reply."""
from __future__ import annotations

import re


STAGE_NAMES = {'direction': '创意方向', 'writing': '完整脚本'}
INTERNAL_NAMES = {'intake': '需求记录', 'direction': '创意方向',
                  'material_plan': '素材计划', 'writing': '脚本'}
INTERNAL_REF = re.compile(
    r'(?:artifact:)?(intake|direction|material_plan|writing)-v\d+'
    r'(?:\#[0-9a-f]{64})?', re.I)


def creator_reply(text: str, delivery: dict, state, *, technical_requested=False,
                  review_timed_out=False, preserve_text=False) -> str:
    """Use the actual gate when an unfinished draft makes model advice unsafe.

    The model can describe its own work, but cannot make an unavailable approval
    button appear. An incomplete gated deliverable gets a small factual status
    instead of a model-authored next-action list. The saved artifact remains the
    place to read the model's substantive proposal.
    """
    goal = delivery.get('goal')
    if delivery.get('blocker'):
        return delivery['blocker']['note']
    if delivery.get('status') == 'incomplete' and goal in STAGE_NAMES:
        stage = state.stages[goal]
        name = STAGE_NAMES[goal]
        if stage.latest and not stage.stale and stage.integrity_ok and stage.can_approve:
            return f'{name}已保存，等你阅读后决定采用或退回。'
        return f'{name}本轮尚未完成。已有内容已保存，请查看本轮记录后继续。'

    if technical_requested or preserve_text:
        return text
    return INTERNAL_REF.sub(lambda match: INTERNAL_NAMES[match.group(1).lower()], text)
