"""Deterministic turn deliverable checks, separate from execution termination.

Two things live here and they are different questions:

* Did the model stop? That is ``run_agent_loop``'s business.
* Did the user's request get delivered? That is this module's business.

A turn that ends politely with nothing delivered is not a finished turn, and
the difference between the two must not depend on the model's own summary.

``follow_up`` is the only goal that promises nothing, and it is limited to
read-only or open-ended requests. Every other goal names a concrete object that
must exist afterwards.
"""
from __future__ import annotations

from .models import STAGES

DELIVERABLES = ('direction', 'intake', 'writing', 'production_input', 'production_media',
                'revision', 'analysis', 'reference_prompt', 'follow_up')

#: Attempt states that mean a provider may still be working, or that the local
#: side has not yet established what happened. Neither is a finished result, and
#: neither is a licence to submit again.
IN_FLIGHT = frozenset({'queued', 'running', 'preparing', 'submitting', 'submitted',
                       'downloading', 'recovering', 'cancelling'})

#: Attempt states where the outcome is recorded but the result is not available.
UNSETTLED = frozenset({'unknown', 'submission_unknown', 'retrieve_failed',
                       'retry_authorized'})

#: Serialised as a JSON array so the value cannot be confused with a digest.
MEDIA_EXPECTATION = {
    'production_input': ('已准备的制作输入', '已保存'),
    'production_media': ('可阅读的生成结果', '已保存'),
}


def _repeat_missing(value, width):
    """A stable key for "the same thing is still missing"."""
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True)[:width]


def _shot_label(ordinals):
    return '第 ' + '、'.join(str(n) for n in ordinals) + ' 镜'


def _shot_coverage(store, pipeline, run_id, kind, require_media, *, state=None):
    """Per-shot readiness for the shots this project actually asks for.

    Returns ``(missing, note, target_count)``. The target list comes from the
    adopted script, so a six-segment script asks for six inputs and a
    three-segment one asks for three — nothing here counts to six.

    Read through the same projection the page uses. If this file derived its own
    answer, a shot could read as blocked on screen and ready in chat.
    """
    from .production import _read_state_without_service

    if state is None:
        state = _read_state_without_service(store, pipeline, run_id)
    targets = state['targets'][kind]
    if not targets['complete_list']:
        # A legacy prose script has no readable shot list. Say so instead of
        # inventing a number.
        return (['可核对的目标镜头清单'], targets['notice'], None)

    shots = state['shots'][kind]['shots']
    total = len(shots)
    if not require_media:
        unready = [row['ordinal'] for row in shots if row['stage'] == 'unprepared']
        if unready:
            return ([f'{_shot_label(unready)}还没有绑定当前方案的制作输入'],
                    f'{total} 个目标镜头中，已准备 {total - len(unready)} 个。', total)
        return ([], f'{total} 个目标镜头的制作输入已齐备。', total)

    failed = [row['ordinal'] for row in shots if row['stage'] == 'failed']
    if failed:
        return ([f'{_shot_label(failed)}生成失败，需要处理原因后再决定'],
                '每面镜头的失败原因与下一步已记录在对应镜头。', total)
    unsettled = [row['ordinal'] for row in shots if row['stage'] == 'unverified']
    if unsettled:
        return ([f'{_shot_label(unsettled)}结果待核实；先查询原任务，不要重复提交'],
                '结果不确定时只核实原任务，不会自动重发。', total)
    missing = [row['ordinal'] for row in shots if row['stage'] != 'completed']
    if missing:
        return ([f'{_shot_label(missing)}还没有可阅读的生成结果'],
                f'{total} 个目标镜头中，已完成 {total - len(missing)} 个。', total)
    return ([], f'{total} 个目标镜头的生成结果已齐备。', total)


def evaluate(store, pipeline, run_id, goal, *, revision_scope=None, analysis_completed=False,
             production_kind=None, media_kind=None, turn_id=None, reuse_artifact=None):
    if goal is None:
        return {'goal': None, 'status': 'unspecified', 'missing': [], 'note': '',
                'target_shots': None, 'repeat_key': '["unspecified"]'}
    if goal == 'follow_up':
        return {'goal': goal, 'status': 'informational', 'missing': [], 'note': '',
                'target_shots': None, 'repeat_key': '["informational"]'}
    if goal not in DELIVERABLES:
        raise ValueError('Unknown deliverable goal')

    missing: list[str] = []
    note = ''
    target_count = None
    delivery_status = None
    if goal in ('intake', 'direction', 'writing'):
        state = pipeline.stage_state(run_id, goal)
        if not state.latest or state.stale or not state.integrity_ok or state.open_attempt:
            missing.append(f'{goal} 当前准确版本')
        elif turn_id:
            scope = next((e for e in reversed(store.read_events(run_id))
                          if e.get('kind') == 'agent_turn_scope_started'
                          and e.get('turn_id') == turn_id), None)
            baseline = (scope or {}).get('artifact_baseline', {}).get(goal)
            if reuse_artifact:
                if (reuse_artifact.get('stage') != goal
                        or reuse_artifact.get('version') != state.latest.version
                        or reuse_artifact.get('sha256') != state.latest.sha256):
                    missing.append(f'{goal} 用户指定沿用的准确版本')
            elif not scope or 'artifact_baseline' not in scope:
                missing.append(f'{goal} 本轮可核对的交付基线')
            elif baseline and state.latest.version <= baseline['version']:
                missing.append(f'{goal} 本轮准确新版本（旧稿仅供参考）')
            elif (revision_scope and revision_scope.get('stage') == goal
                  and revision_scope.get('mode') == 'local'):
                missing.append(f'{goal} 完整新交付（当前仅完成局部补丁）')
    elif goal in MEDIA_EXPECTATION:
        writing = pipeline.stage_state(run_id, 'writing')
        if (not writing.latest or writing.stale or not writing.integrity_ok
                or not pipeline.is_complete(run_id, 'writing')):
            missing.append('已确认的当前完整方案')
            note = '脚本本身还没有被采用或是过期版本，制作输入没有可靠依据。'
            if goal == 'production_media':
                # The script being unadopted is not a media-delivery failure; the
                # first thing to fix is upstream, and saying otherwise would push
                # the controller at a generation it cannot justify.
                missing = ['当前脚本待用户采用']
        else:
            from .production import _read_state_without_service

            kind = media_kind or production_kind or 'image'
            production_state = _read_state_without_service(store, pipeline, run_id)
            missing, note, target_count = _shot_coverage(
                store, pipeline, run_id, kind, require_media=(goal == 'production_media'),
                state=production_state)
            if goal == 'production_media' and missing:
                # A read-only candidate is neither a displayed confirmation nor
                # a delivered result. Keep the missing-media evidence and expose
                # the real stopping condition so the controller never retries an
                # active/unknown job just to make the contract say complete.
                progress = production_state['shots'][kind]
                delivery_status = progress['state']
                if delivery_status == 'input_ready':
                    note = f'{target_count} 个镜头的输入已齐备，尚未提交生成；需要用户核对并授权。'
                elif delivery_status == 'awaiting_authorisation':
                    note = '准确生成确认已准备，等待用户授权；媒体尚未交付。'
                elif delivery_status == 'in_progress':
                    note = '原任务仍在执行，等待或查询原任务，不要重新提交。'
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
    elif goal == 'analysis' and not analysis_completed:
        missing.append('已保存的参考分析报告')
    elif goal == 'reference_prompt':
        saved = any(e.get('kind') == 'reference_prompt_saved' and e.get('turn_id') == turn_id
                    for e in store.read_events(run_id)) if turn_id else False
        if not saved:
            missing.append('本轮已保存的商品适配提示词')
    return {'goal': goal, 'status': delivery_status or ('incomplete' if missing else 'complete'),
            'missing': missing, 'note': note, 'target_shots': target_count,
            'repeat_key': _repeat_missing(missing, 600)}


def media_progress(store, run_id, kind):
    """How the target shots stand, for a turn that asked for media.

    Separated from ``evaluate`` because "still generating" must not be reported
    as "you have not delivered": the first means wait, the second means work.
    Telling the controller a turn is incomplete while a provider task is in
    flight is how the same generation gets submitted twice.
    """
    from .production import _read_state_without_service
    from .pipeline import Pipeline

    state = _read_state_without_service(store, Pipeline(store), run_id)
    targets = state['targets'][kind]
    if not targets['complete_list']:
        return {'status': 'unknown_targets', 'pending': False, 'notes': targets['notice']}
    result = state['shots'][kind]
    return {'status': result['state'], 'pending': bool(result['pending']),
            'failed': bool(result['failed']), 'delivered': bool(result['delivered']),
            'total': result.get('total', 0),
            'in_flight': [row['ordinal'] for row in result['shots']
                          if row['stage'] in {'awaiting_authorisation', 'submitted',
                                              'generating', 'retrieving'}]}
