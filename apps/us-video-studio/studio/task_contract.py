"""Deterministic turn deliverable checks, separate from execution termination."""
from __future__ import annotations

from .models import STAGES

DELIVERABLES = ('direction', 'intake', 'writing', 'production_input', 'revision', 'analysis', 'follow_up')


def evaluate(store, pipeline, run_id, goal, *, revision_scope=None, analysis_completed=False,
             production_kind=None):
    if goal is None:
        return {'goal': None, 'status': 'unspecified', 'missing': []}
    if goal == 'follow_up':
        return {'goal': goal, 'status': 'informational', 'missing': []}
    if goal not in DELIVERABLES:
        raise ValueError('Unknown deliverable goal')

    missing = []
    if goal in ('intake', 'direction', 'writing'):
        state = pipeline.stage_state(run_id, goal)
        if not state.latest or state.stale or not state.integrity_ok or state.open_attempt:
            missing.append(f'{goal} 当前准确版本')
        elif goal != 'intake' and (not state.review or state.review.verdict != 'pass'
                                   or state.review.artifact_sha256 != state.latest.sha256):
            missing.append(f'{goal} 当前版本的独立审核通过记录')
    elif goal == 'production_input':
        from .production import draft_records, source_snapshot
        writing = pipeline.stage_state(run_id, 'writing')
        if (not writing.latest or writing.stale or not writing.integrity_ok
                or not pipeline.is_complete(run_id, 'writing')):
            missing.append('已确认的当前完整方案')
        else:
            baseline = source_snapshot(store, run_id)
            matching = [d for d in draft_records(store, run_id).values()
                        if d['source_sha256'] == writing.latest.sha256
                        and d['source_snapshot'] == baseline
                        and (production_kind is None or d['kind'] == production_kind)]
            if not matching:
                missing.append('绑定当前方案的制作输入')
            elif not any(d.get('review', {}).get('verdict') == 'ready' for d in matching if d.get('review')):
                missing.append('制作输入的独立文字检查通过记录')
    elif goal == 'revision':
        if not revision_scope:
            missing.append('绑定本轮原话与准确基线的修订计划')
        else:
            stage = revision_scope['stage']
            if stage not in STAGES:
                missing.append('有效修订阶段')
            else:
                applied = [e for e in store.read_events(run_id)
                           if e.get('kind') == 'agent_revision_applied'
                           and e.get('plan_id') == revision_scope['plan_id']]
                state = pipeline.stage_state(run_id, stage)
                if (not applied or not state.latest or state.stale or not state.integrity_ok
                        or state.latest.sha256 != applied[-1]['sha256']):
                    missing.append('按本轮计划提交的准确新版本')
                elif stage in ('direction', 'writing') and (not state.review or state.review.verdict != 'pass'
                                                             or state.review.artifact_sha256 != state.latest.sha256):
                    missing.append('修订新版的独立审核通过记录')
    elif goal == 'analysis' and not analysis_completed:
        missing.append('已保存的参考分析报告')
    return {'goal': goal, 'status': 'incomplete' if missing else 'complete', 'missing': missing}
