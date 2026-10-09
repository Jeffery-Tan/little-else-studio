"""Bounded preparation authority from an actual saved UI approval, never prose."""
from __future__ import annotations

from .models import PipelineError


def _identity(artifact):
    return {'version': artifact.version, 'sha256': artifact.sha256} if artifact else None


def _format(store, run_id):
    from .project_brief import content_format
    # The established project default is video. A read-only format=none turn
    # cannot erase the most recent explicit creative format.
    return content_format(store.read_events(run_id)) or 'video'


def _approval(store, run_id, request_id):
    if store.read_run(run_id).kind != 'creative':
        raise PipelineError('decision_scope', '这项决定不属于创作项目的准备流程。')
    from .decision import continuation_request_id
    base = request_id.split(':prepare:', 1)[0]
    matches = [a for a in store.list_approvals(run_id)
               if continuation_request_id(a) == base]
    if len(matches) != 1:
        raise PipelineError('decision_scope', '无法唯一核对这次准备所依据的原决定。')
    approval = matches[0]
    if (approval.source != 'ui' or not approval.continue_after
            or approval.decision not in {'approved', 'approved_override'}):
        raise PipelineError('decision_scope', '这项原决定没有授予此 UI 准备范围。')
    if approval.stage not in {'direction', 'writing'}:
        raise PipelineError('decision_scope', '这项决定没有唯一可用的可逆准备下一步。')
    if ':prepare:' in request_id:
        import re
        token = request_id.split(':prepare:', 1)[1]
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', token):
            raise PipelineError('decision_scope', '重新准备请求没有准确的决定或请求身份。')
    return approval


def _current(store, pipeline, run_id, approval):
    stage = pipeline.stage_state(run_id, approval.stage)
    if (not stage.latest or stage.stale
            or _identity(stage.latest) != {'version': approval.artifact_version,
                                           'sha256': approval.artifact_sha256}
            or stage.approval != approval):
        raise PipelineError('decision_scope', '原采用版本已变化或失效，请先核对当前作品。')
    if not store.verify_artifact(run_id, approval.stage, approval.artifact_version):
        raise PipelineError('decision_scope', '原采用版本的文件指纹无法核对。')


def _preparation_ready(store, pipeline, run_id, approval):
    if approval.stage == 'writing':
        from .production import _read_state_without_service, continuation_blocker
        blocker = continuation_blocker(_read_state_without_service(store, pipeline, run_id), 'production_input')
        if blocker:
            raise PipelineError('decision_scope', blocker['note'])


def freeze(store, pipeline, run_id, request_id):
    """Only the UI adoption path obtains this envelope; real receipts stay intact."""
    from .decision import continuation_request_id
    base = request_id.split(':prepare:', 1)[0]
    approvals = [a for a in store.list_approvals(run_id)
                 if continuation_request_id(a) == base]
    if len(approvals) > 1:
        raise PipelineError('decision_scope', '原决定身份不唯一。')
    if not approvals or approvals[0].source != 'ui' or approvals[0].decision == 'returned':
        return None
    with store.run_lock(run_id):
        approval = _approval(store, run_id, request_id)
        _current(store, pipeline, run_id, approval)
        _preparation_ready(store, pipeline, run_id, approval)
        saved = [e for e in store.read_events(run_id)
                 if e['kind'] == 'decision_continuation_scope_frozen'
                 and e.get('request_id') == request_id]
        if saved:
            if len(saved) != 1:
                raise PipelineError('decision_scope', '准备范围的持久身份不唯一。')
            context = saved[0]['context']
            validate(store, pipeline, run_id, context)
            return context
        from .creation_mode import current
        content_format = _format(store, run_id)
        if content_format not in {'video', 'photo'}:
            raise PipelineError('decision_scope', '当前交付格式不明确，不能派生准备范围。')
        production = approval.stage == 'writing'
        scope = {'intent': 'continue', 'format': content_format,
                 'deliverable': 'production_input' if production else 'writing',
                 'allowed_stages': [] if production else ['material_plan', 'writing']}
        if production:
            scope['production_kind'] = 'image'
        context = {'request_id': request_id, 'approval': approval.to_dict(), 'scope': scope,
                   'creation_mode': current(store, run_id) or {
                       'mode': 'autonomous', 'reference_id': None, 'scope': ''},
                   'intake': _identity(store.latest_artifact(run_id, 'intake'))}
        if ':prepare:' in request_id:
            requests = [e for e in store.read_events(run_id)
                        if e['kind'] == 'decision_preparation_requested' and e.get('request_id') == request_id]
            if len(requests) != 1:
                raise PipelineError('decision_scope', '缺少这次明确重新准备的请求。')
            context['failed_job_id'] = requests[0]['failed_job_id']
        if production:
            from .production import source_snapshot
            context['production_snapshot'] = source_snapshot(store, run_id)
        store.append_event(run_id, 'decision_continuation_scope_frozen',
                           request_id=request_id, context=context)
        return context


def validate(store, pipeline, run_id, context):
    """Recheck the original persisted envelope before scope selection and writes."""
    with store.run_lock(run_id):
        if not isinstance(context, dict) or not isinstance(context.get('request_id'), str):
            raise PipelineError('decision_scope', '缺少可信的决定准备范围。')
        saved = [e for e in store.read_events(run_id)
                 if e['kind'] == 'decision_continuation_scope_frozen'
                 and e.get('request_id') == context['request_id']]
        if len(saved) != 1 or saved[0].get('context') != context:
            raise PipelineError('decision_scope', '准备范围与原决定记录不一致。')
        approval = _approval(store, run_id, context['request_id'])
        if approval.to_dict() != context.get('approval'):
            raise PipelineError('decision_scope', '原决定身份已变化。')
        _current(store, pipeline, run_id, approval)
        from .creation_mode import current
        mode = current(store, run_id) or {'mode': 'autonomous', 'reference_id': None, 'scope': ''}
        if (mode != context.get('creation_mode')
                or _format(store, run_id) != context['scope']['format']
                or _identity(store.latest_artifact(run_id, 'intake')) != context.get('intake')):
            raise PipelineError('decision_scope', '准备所依据的格式、参考或需求已变化。')
        if context.get('production_snapshot'):
            from .production import source_snapshot
            if source_snapshot(store, run_id) != context['production_snapshot']:
                raise PipelineError('decision_scope', '准备所依据的脚本或素材已变化。')
        return context['scope']


def related_jobs(events, approval):
    """Only this approval's exact initial request or its persisted fresh scopes."""
    from .decision import continuation_request_id
    base = continuation_request_id(approval)
    return [e for e in events if e['kind'] == 'job_created'
            and (e.get('request_id') == base or
                 (str(e.get('request_id', '')).startswith(base + ':prepare:')
                  and (e.get('job_payload') or {}).get('decision_context', {}).get('approval') == approval.to_dict()
                  and any(r['kind'] == 'decision_continuation_scope_frozen'
                          and r.get('request_id') == e.get('request_id')
                          and r.get('context') == (e.get('job_payload') or {}).get('decision_context')
                          for r in events)))]


def projection(store, pipeline, run_id, approval, events):
    if approval.source != 'ui' or not approval.continue_after:
        return None
    from .decision import continuation_request_id
    jobs = related_jobs(events, approval)
    request_id = continuation_request_id(approval)
    if jobs:
        latest = jobs[-1]
        if sum(e.get('request_id') == latest.get('request_id') for e in jobs) != 1:
            return None
        request_id, job_id = latest['request_id'], latest['job_id']
        status, reason = 'queued', ''
        for e in events:
            if e['kind'] == 'job_status' and e.get('job_id') == job_id:
                status, reason = e.get('status'), e.get('reason', '')
    else:
        job_id, status, reason = None, 'pending', ''
    copy = {'pending': '后续准备尚未启动。', 'queued': '后续准备已排队。',
            'running': '团队正在准备后续内容。', 'cancelling': '准备任务正在停止。',
            'succeeded': '本轮后续准备已结束，请查看保存的内容。',
            'blocked': '后续准备未完成。', 'failed': '后续准备执行失败。',
            'cancelled': '后续准备已停止，不会自动重跑。',
            'interrupted': '后续准备被中断，不会自动重跑。'}
    result = {'request_id': request_id, 'job_id': job_id, 'status': status,
              'text': copy.get(status, '后续准备状态待核实。'), 'reason': reason}
    if status not in {'blocked', 'failed'} or approval.decision not in {'approved', 'approved_override'}:
        return result
    states = {e['job_id']: 'queued' for e in events if e['kind'] == 'job_created'}
    for e in events:
        if e['kind'] == 'job_status':
            states[e['job_id']] = e.get('status')
    if any(s in {'queued', 'running', 'cancelling'} for s in states.values()):
        return result
    try:
        _current(store, pipeline, run_id, approval)
        _preparation_ready(store, pipeline, run_id, approval)
        if approval.stage not in {'direction', 'writing'}:
            return result
    except (PipelineError, ValueError):
        return result
    result['retry'] = {'stage': approval.stage, 'expected_version': approval.artifact_version,
                       'expected_artifact_sha256': approval.artifact_sha256, 'failed_job_id': job_id}
    return result
