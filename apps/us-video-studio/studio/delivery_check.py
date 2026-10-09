"""One producer check, at most one scoped repair, then a read-only conclusion.

The check is an observed model assessment, not a user approval or quality fact.
Its findings are bound to exact saved cells and cannot create new authority.
"""
from __future__ import annotations

from dataclasses import replace
import copy
import hashlib
import json
import uuid

from .agents.client import ModelError, NonRetryableStageError
from .agents.roles import DELIVERY_CHECK
from .models import PipelineError
from .revision_scope import _cell, _path_key, check_base


CHECK_TOOL = {'name': 'submit_delivery_check', 'description': '记录本次准确作品的主制片自检；不审批，不授权媒体。',
    'input_schema': {'type': 'object', 'additionalProperties': False,
        'properties': {'verdict': {'type': 'string', 'enum': ['ready', 'repair', 'blocked']},
            'summary': {'type': 'string'},
            'findings': {'type': 'array', 'maxItems': 8, 'items': {'type': 'object',
                'additionalProperties': False, 'properties': {
                    'path': {'type': 'array', 'minItems': 1, 'maxItems': 4,
                             'items': {'anyOf': [{'type': 'string'}, {'type': 'integer'}]}},
                    'quote': {'type': 'string'}, 'issue': {'type': 'string'},
                    'instruction': {'type': 'string'}},
                'required': ['path', 'quote', 'issue', 'instruction']}}},
        'required': ['verdict', 'summary', 'findings']}}


def _sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def _repair_already_planned(orch):
    from .task_execution import root_task
    job_id = root_task(orch.store, orch.run_id)
    source_id = (orch._task_user_input or {}).get('message_id')
    decision_id = (orch._decision_context or {}).get('request_id')
    return any(row['kind'] == 'delivery_repair_planned' and (
        bool(job_id and row.get('job_id') == job_id) or
        bool((source_id or decision_id) and row.get('source_message_id') == source_id
             and row.get('decision_request_id') == decision_id))
        for row in orch.store.read_events(orch.run_id))


def _check(orch, client, context, index):
    context.check_current()
    data = json.loads(context.messages[0]['content'])
    saved = data['current_script' if context.stage == 'writing' else 'current_direction']
    orch._emit({'type': 'agent_activity', 'role': 'orchestrator', 'stage': context.stage,
                'phase': 'delivery_check', 'check_index': index})
    previous_stage = client.stage
    try:
        client.stage = context.stage
        turn = client.complete(model=orch.settings.model_for('orchestrator'), system=DELIVERY_CHECK,
            messages=[{'role': 'user', 'content': json.dumps({**data, 'phase': 'delivery_check',
                'check_index': index, 'repair_remaining': index == 1 and not _repair_already_planned(orch)},
                ensure_ascii=False)}],
            tools=[copy.deepcopy(CHECK_TOOL)])
    finally:
        client.stage = previous_stage
    context.check_current()
    orch._emit({'type': 'agent_turn', 'role': 'orchestrator', 'stage': context.stage,
                'phase': 'delivery_check', 'stop_reason': turn.stop_reason,
                'tools': [call.name for call in turn.tool_calls]})
    if (len(turn.tool_calls) != 1 or turn.tool_calls[0].name != CHECK_TOOL['name']
            or turn.stop_reason != 'tool_use'):
        raise ModelError(200, 'invalid producer delivery check', usage=turn.usage, usage_final=True,
            retryable=False, public_message='作品已保存，但交付前自检没有返回可核对的结论；本轮停止，不自动重试。')
    result = copy.deepcopy(turn.tool_calls[0].input)
    from .agents.deepseek_client import _strict_schema_issue
    if _strict_schema_issue(result, CHECK_TOOL['input_schema']):
        raise NonRetryableStageError('自检没有返回完整且合法的问题记录，已保留作品，未开始修复。')
    if not result['summary'].strip() or len(result['summary']) > 2000:
        raise NonRetryableStageError('自检缺少明确结论，已保留作品，未进入自动修复。')
    if ((result['verdict'] == 'ready' and result['findings'])
            or (result['verdict'] == 'repair' and not result['findings'])):
        raise NonRetryableStageError('自检结论与问题列表不一致，已保留作品，未进入自动修复。')
    if len({_path_key(finding['path']) for finding in result['findings']}) != len(result['findings']):
        raise NonRetryableStageError('自检重复列出同一字段，未进入自动修复。')
    for finding in result['findings']:
        cell = (saved['body'] if finding['path'] == ['body'] and isinstance(saved.get('body'), str)
                else _cell(saved['structured'], finding['path']))
        if (not finding['quote'].strip() or finding['quote'] not in cell
                or any(not finding[key].strip() or len(finding[key]) > 3000
                       for key in ('quote', 'issue', 'instruction'))):
            raise NonRetryableStageError('自检问题没有绑定作品中的准确文字，不能据此自动改稿。')
    event = orch.store.append_event(orch.run_id, 'delivery_self_check',
        source_message_id=data['source_message_id'], turn_id=orch._turn_id,
        decision_request_id=(data.get('decision_context') or {}).get('request_id'),
        stage=context.stage, artifact={'version': saved['version'], 'sha256': saved['sha256']},
        task_sha256=_sha(orch._task_constraints), check_index=index, assessment=result,
        quality_verified=False, user_approval=False)
    return {**result, 'event_seq': event['seq'], 'stage': context.stage,
            'artifact': event['artifact'], 'check_index': index}


def _repair_paths(stage, findings):
    """Only existing creative prose; identities, facts and user decisions stay out."""
    allowed = []
    for finding in findings:
        path = finding['path']
        valid = (stage == 'writing' and (path == ['caption'] or len(path) == 3
            and path[0] == 'segments' and type(path[1]) is int and path[2] in
            {'line', 'picture', 'sound', 'on_screen_text', 'text', 'headline', 'body', 'cta'})) or (
            stage == 'direction' and len(path) == 3 and path[0] == 'options'
            and type(path[1]) is int and path[2] in {'title', 'claim', 'hook', 'story', 'gives_up'})
        if not valid:
            raise PipelineError('delivery_check_scope', '自检发现的问题涉及事实、结构或决定，超出本次定向修复范围。')
        if path not in allowed:
            allowed.append(path)
    return allowed


def repair_scope(orch, check):
    from .conversation_input import message, interpretation_for, interpreted_task_constraints
    from .task_execution import root_task
    source = orch._task_user_input
    decision = orch._decision_context
    if not source and not decision:
        raise PipelineError('delivery_check_scope', '自检修复缺少原任务来源。')
    source_id = source['message_id'] if source else None
    interpreted = None
    if decision:
        from .decision_continuation import validate
        actual = {**validate(orch.store, orch.pipeline, orch.run_id, decision), 'task_mode': 'create'}
        if source or actual != orch._task_constraints:
            raise PipelineError('delivery_check_scope', '原决定范围已变化，未开始修复。')
    else:
        receipt = message(orch.store, orch.run_id, source_id)
        interpreted = interpretation_for(orch.store, orch.run_id, source_id)
        actual = interpreted_task_constraints(receipt, interpreted)
        if receipt != source or not interpreted or actual != orch._task_constraints:
            raise PipelineError('delivery_check_scope', '原任务范围已变化，未开始修复。')
    stage = check['stage']
    if (actual.get('readonly') or stage not in actual.get('allowed_stages', [])
            or actual.get('task_mode') not in {'produce', 'revise', 'create'}):
        raise PipelineError('delivery_check_scope', '本轮原范围不允许修改这个阶段。')
    paths = _repair_paths(stage, check['findings'])
    original = orch._revision_scope
    if actual.get('task_mode') == 'revise' and not original:
        raise PipelineError('delivery_check_scope', '原修订范围缺失，不能替用户扩大修改范围。')
    if original and (original['stage'] != stage or original['mode'] == 'local'
            and any(path not in [field['path'] for field in original['fields']] for path in paths)):
        raise PipelineError('delivery_check_scope', '问题超出用户本轮已绑定的局部字段，未自动扩大修改。')
    job_id = root_task(orch.store, orch.run_id)
    scope = {'stage': stage, **check['artifact'], 'mode': 'local',
        'origin': 'delivery_self_check', 'source_message_id': source_id,
        **({'root_job_id': job_id} if job_id else {}),
        'interpretation_id': interpreted['id'] if interpreted else None, 'task_sha256': _sha(actual),
        **({'decision_context': copy.deepcopy(decision)} if decision else {}),
        'check_seq': check['event_seq'], 'turn_id': orch._turn_id, 'plan_id': uuid.uuid4().hex,
        'reason': check['summary'], 'fields': [{'path': finding['path'], 'instruction': finding['instruction']}
            for finding in check['findings']],
        'original_plan_id': original['plan_id'] if original else None,
        'inputs': orch.pipeline.require_prerequisites(orch.run_id, stage),
        'affected_stages': [], 'input_changes': []}
    # Validate before recording. The persisted plan becomes the trusted proof
    # used by the specialist; model-supplied origin flags cannot manufacture it.
    state = orch.pipeline.stage_state(orch.run_id, stage)
    if state.approval:
        raise PipelineError('delivery_check_scope', '该准确版本已有用户决定，不自动改写。')
    plain = {key: value for key, value in scope.items() if key != 'origin'}
    base, _ = check_base(orch.store, orch.pipeline, orch.run_id, plain)
    scope['cells'] = [{'path': path, 'value': _cell(base.structured, path)} for path in paths]
    with orch.store.run_lock(orch.run_id):
        if _repair_already_planned(orch):
            raise PipelineError('delivery_check_scope', '本任务已用过一次自检修复，不再自动返工。')
        orch.store.append_event(orch.run_id, 'delivery_repair_planned',
            job_id=job_id, source_message_id=source_id,
            decision_request_id=decision['request_id'] if decision else None, scope=scope)
    return scope


def validate_repair_scope(store, run_id, scope, source_message_id):
    from .conversation_input import message, interpretation_for, interpreted_task_constraints
    from .task_execution import root_task
    if scope.get('origin') != 'delivery_self_check':
        return False
    saved = [event for event in store.read_events(run_id) if event['kind'] == 'delivery_repair_planned'
             and (event.get('scope') or {}).get('plan_id') == scope.get('plan_id')]
    if (len(saved) != 1 or saved[0]['scope'] != scope or scope['source_message_id'] != source_message_id):
        raise PipelineError('delivery_check_scope', '自检修复计划不是本任务已保存的准确范围。')
    job_id = root_task(store, run_id)
    if scope.get('root_job_id') and job_id and scope['root_job_id'] != job_id:
        raise PipelineError('delivery_check_scope', '自检修复计划不属于当前父任务。')
    if scope.get('decision_context'):
        from .decision_continuation import validate
        from .pipeline import Pipeline
        actual = {**validate(store, Pipeline(store), run_id, scope['decision_context']), 'task_mode': 'create'}
        if source_message_id or _sha(actual) != scope['task_sha256']:
            raise PipelineError('delivery_check_scope', '原UI决定范围已变化，自检修复停止。')
        return True
    receipt = message(store, run_id, source_message_id)
    interpreted = interpretation_for(store, run_id, source_message_id)
    actual = interpreted_task_constraints(receipt, interpreted)
    if (not interpreted or interpreted['id'] != scope['interpretation_id']
            or _sha(actual) != scope['task_sha256']):
        raise PipelineError('delivery_check_scope', '原任务解释已变化，自检修复停止。')
    return True


def prepare(orch, client, context):
    """Check once; an eligible repair is followed by one recheck, never a loop."""
    context.check_current()
    checked = _check(orch, client, context, 1)
    rounds = 1
    orch._delivery_check_result = checked
    if checked['verdict'] == 'repair':
        try:
            scope = repair_scope(orch, checked)
        except PipelineError as exc:
            checked = {**checked, 'verdict': 'blocked', 'repair_blocker': str(exc)}
            orch.store.append_event(orch.run_id, 'delivery_repair_blocked',
                source_message_id=(orch._task_user_input or {}).get('message_id'),
                check_seq=checked['event_seq'], reason=str(exc))
        else:
            context.check_current()
            orch._finishing_guard = None
            outcome = orch.dispatcher.dispatch(context.stage,
                '主制片在原任务交付前发现以下准确内容问题；这不是新用户要求。只修复已绑定字段，再复查整份表达。\n'
                + json.dumps(checked['findings'], ensure_ascii=False), revision_scope=scope,
                user_input_id=(orch._task_user_input or {}).get('message_id'), turn_scope=orch._role_turn_scope())
            if not outcome.submitted:
                checked = {**checked, 'verdict': 'blocked', 'repair_blocker': outcome.error or '责任角色未提交修复。'}
            else:
                orch._delivery_repair_scope = scope
                fresh = orch._finishing_context()
                if fresh is None:
                    raise NonRetryableStageError('修复版本已保存，但无法核对完整交付来源，未发布旧稿结论。')
                context = fresh
                checked = _check(orch, client, context, 2)
                rounds += 1
                if checked['verdict'] == 'repair':
                    checked = {**checked, 'verdict': 'blocked', 'repair_blocker': '一次修复后仍有问题；已停止自动返工。'}
    orch._delivery_check_result = checked
    data = json.loads(context.messages[0]['content'])
    data['producer_check'] = checked
    return replace(context, prepare=None, preparation_rounds=rounds,
        system=context.system + '\n交付前自检及修复记录在 producer_check；这是主制片的内容判断，不是用户采用或外部效果验证。'
        'blocked 表示本轮仍未完成，应明确尚存问题和范围/预算边界；不要求用户逐项验收。',
        messages=[{'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}])
