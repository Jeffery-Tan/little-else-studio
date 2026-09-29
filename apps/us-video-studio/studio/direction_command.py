"""One explicit, version-bound command for continuing the direction stage.

The model may write the direction, but it does not decide which stage to run or
whether a stale page still refers to the same sources. The existing Pipeline
remains the authority for prerequisites and immutable artifact versions.
"""
from __future__ import annotations

import hashlib
import json

from .models import PipelineError


def available(store, pipeline, run_id):
    """Return the current command identity, or None when a choice is pending."""
    if store.read_run(run_id).kind != 'creative':
        return None
    state = pipeline.stage_state(run_id, 'direction')
    if (state.status not in {'empty', 'stale', 'returned'} or not state.can_start
            or (state.latest is not None and not state.integrity_ok)):
        return None
    try:
        inputs = pipeline.require_prerequisites(run_id, 'direction')
    except PipelineError:
        return None
    baseline = ({'version': state.latest.version, 'sha256': state.latest.sha256}
                if state.latest else None)
    identity = {'stage': 'direction', 'status': state.status,
                'inputs': inputs, 'baseline': baseline}
    fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    return {'kind': 'resume_direction', 'stage': 'direction',
            'label': '生成创意方向' if state.latest is None else '重做创意方向',
            'expected_context_sha256': fingerprint}


def require_current(store, pipeline, run_id, expected_context_sha256):
    command = available(store, pipeline, run_id)
    if command is None:
        raise PipelineError('direction_resume',
            '当前没有可直接重做的创意方向；请先处理上游资料或阅读已有方向。')
    if command['expected_context_sha256'] != expected_context_sha256:
        raise PipelineError('direction_conflict',
            '创意方向所依据的资料或版本已变化，请刷新项目后重新发起。')
    return command


def task_brief(store, pipeline, run_id):
    """Give the director a fixed deliverable, with a returned decision if any."""
    state = pipeline.stage_state(run_id, 'direction')
    lines = [
        '按当前有效简报、已选择的参考和本款商品观察，交付 A/B/C 三个真正不同的创意方向。',
        '每个方向只写主张、开场识别点、两三句话的推进与结尾回报、证据和取舍。',
        '指出推荐项及最不确定的一点；停在方向选择，不写逐镜头脚本、不提交媒体生成。',
    ]
    if state.status == 'returned' and state.approval and state.approval.note:
        lines.append('Jeffery 退回上一版时的意见：' + state.approval.note[:2000])
    return '\n'.join(lines)
