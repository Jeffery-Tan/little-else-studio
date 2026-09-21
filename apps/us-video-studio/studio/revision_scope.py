"""Turn-bound revision scopes and deterministic, field-limited submissions.

Plans are execution records, never approvals. Pipeline remains the sole source
of dependency validity; unchanged cells do not carry an approval forward.
"""
from __future__ import annotations

import copy
import json
import uuid

from .models import PipelineError, STAGES
from .structured import MAX_CELL, is_canonical_document, normalize


def work_plan(pipeline, run_id, target=None):
    if target is not None and target not in STAGES:
        raise PipelineError('revision_scope', '未知目标阶段。')
    states = pipeline.state(run_id).stages
    responsible = {s: [] for s in STAGES}
    for source, state in states.items():
        if (not state.latest or state.stale or not state.integrity_ok or not state.review
                or state.review.verdict != 'return'
                or (state.approval and state.approval.decision.startswith('approved'))):
            continue
        for finding in state.review.findings:
            owner = finding.get('stage', source)
            if owner not in states or not states[owner].latest:
                continue
            if owner != source and state.latest.inputs.get(owner) != states[owner].latest.sha256:
                continue
            responsible[owner].append({'source_stage': source, 'source_version': state.latest.version,
                                       'source_sha256': state.latest.sha256, 'finding': finding})
    rows = []
    for stage, state in states.items():
        if state.open_attempt:
            action = 'recover'
        elif not state.integrity_ok:
            action = 'integrity_check'
        elif state.latest is None:
            action = 'create'
        elif state.stale:
            action = 'update'
        elif responsible[stage]:
            action = 'revise'
        elif state.can_review:
            action = 'review'
        elif state.can_approve:
            action = 'user_decision'
        else:
            action = 'preserve'
        rows.append({'stage': stage, 'action': action,
                     'version': state.latest.version if state.latest else None,
                     'sha256': state.latest.sha256 if state.latest else None,
                     'can_start': state.can_start, 'can_review': state.can_review,
                     'reason': state.stale_reason or state.blocked_reason,
                     'responsible_findings': responsible[stage],
                     'requires': list(pipeline.upstream_stages(stage))})
    relevant = rows[:STAGES.index(target) + 1] if target else rows
    next_step = next((r for r in relevant if r['action'] != 'preserve'), None)
    return {'stages': rows, 'next_step': next_step,
            'notice': '保留有效内容；按当前依赖逐步更新。审核/用户决定不等于重写；计划不批准或生成媒体。'}


def _path_key(path):
    if (not isinstance(path, list) or not 1 <= len(path) <= 4
            or any(type(p) not in (str, int) for p in path)):
        raise PipelineError('revision_scope', '字段路径必须是字符串/非负整数数组。')
    return json.dumps(path, ensure_ascii=False)


def _cell(data, path):
    _path_key(path)
    node = data
    for part in path:
        if isinstance(node, list) and type(part) is int and 0 <= part < len(node):
            node = node[part]
        elif isinstance(node, dict) and type(part) is str and part in node:
            node = node[part]
        else:
            raise PipelineError('revision_scope', '字段不存在；先读取准确版本的结构，不猜测路径。')
    if not isinstance(node, str) or path[-1] == 'key':
        raise PipelineError('revision_scope', '局部修改仅替换已有文字字段，不能改变方向身份或整个列表/对象。')
    return node


def check_base(store, pipeline, run_id, scope):
    stage = scope['stage']
    state = pipeline.stage_state(run_id, stage)
    base = state.latest
    if (not base or (base.version, base.sha256) != (scope['version'], scope['sha256'])
            or not state.integrity_ok or base.digest_algorithm != 'artifact-v2'):
        raise PipelineError('version_conflict', '修订基线已变化或不可信，请重新读取当前版本。')
    inputs = pipeline.require_prerequisites(run_id, stage)
    if scope.get('inputs') is not None and inputs != scope['inputs']:
        raise PipelineError('version_conflict', '修订计划的依据已变化，请重新读取并评估。')
    if scope['mode'] == 'local':
        if state.stale:
            raise PipelineError('revision_scope', '依据已变化，需先按新证据更新责任阶段，不能仅局部替换后视作依据有效。')
        original = store.read_artifact_text(run_id, stage, base.version)
        if not is_canonical_document(stage, base.structured, original):
            raise PipelineError('revision_scope', '旧稿的正文与卡片分别保存，不能保证只改指定字段；需明确整理完整版本。')
    return base, inputs


def plan_revision(store, pipeline, run_id, args, *, turn_id, user_message, previous=None):
    if (set(args) - {'stage', 'version', 'sha256', 'mode', 'quote', 'fields', 'reason'}
            or args.get('stage') not in STAGES or type(args.get('version')) is not int
            or args.get('mode') not in ('local', 'rework')):
        raise PipelineError('revision_scope', '修订计划的阶段、版本或范围无效。')
    quote, reason = args.get('quote'), args.get('reason')
    if (not isinstance(quote, str) or not quote.strip() or quote not in user_message
            or not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_CELL):
        raise PipelineError('revision_scope', '修订须绑定本轮用户原话，并说明本次修改原因。')
    fields = args.get('fields', [])
    if not isinstance(fields, list) or len(fields) > 200:
        raise PipelineError('revision_scope', '修改字段列表无效。')
    if (args['mode'] == 'local') != bool(fields):
        raise PipelineError('revision_scope', 'local 须列出字段；rework 不接受局部字段列表。')
    scope = {**copy.deepcopy(args), 'fields': fields, 'turn_id': turn_id}
    base, inputs = check_base(store, pipeline, run_id, scope)
    keys = []
    for entry in fields:
        if (not isinstance(entry, dict) or set(entry) != {'path', 'instruction'}
                or not isinstance(entry['instruction'], str) or not entry['instruction'].strip()
                or len(entry['instruction']) > MAX_CELL):
            raise PipelineError('revision_scope', '每个字段须有准确路径和修改要求。')
        keys.append(_path_key(entry['path']))
        _cell(base.structured, entry['path'])
    if len(keys) != len(set(keys)):
        raise PipelineError('revision_scope', '修改字段不能重复。')
    if previous and previous['mode'] == 'local':
        old_keys = {_path_key(f['path']) for f in previous['fields']}
        if (scope['mode'] != 'local' or scope['stage'] != previous['stage']
                or not set(keys) <= old_keys):
            raise PipelineError('revision_scope', '本轮已限定局部修改，不能扩大范围或改写其他阶段。')
    scope.update(plan_id=uuid.uuid4().hex, inputs=inputs)
    scope['input_changes'] = [{'source': key, 'before': base.inputs.get(key), 'current': inputs.get(key)}
                             for key in sorted(base.inputs.keys() | inputs.keys())
                             if base.inputs.get(key) != inputs.get(key)]
    scope['cells'] = [{'path': f['path'], 'value': _cell(base.structured, f['path'])} for f in fields]
    scope['affected_stages'] = [s for s in STAGES if s != base.stage
        and base.stage in pipeline.upstream_stages(s) and store.latest_artifact(run_id, s)]
    store.append_event(run_id, 'agent_revision_planned', scope=scope)
    return scope


def patch_payload(store, pipeline, run_id, scope, args):
    """Validate everything before attempt submission; preserve untouched values."""
    if scope['mode'] != 'local' or set(args) - {'changes', 'summary'}:
        raise PipelineError('revision_scope', '局部提交只能提供 changes 与 summary，不能提交全文。')
    changes, summary = args.get('changes'), args.get('summary')
    if (not isinstance(changes, list) or not 1 <= len(changes) <= 200
            or not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_CELL):
        raise PipelineError('revision_scope', '请提交非空字段补丁及简短摘要。')
    base, _ = check_base(store, pipeline, run_id, scope)
    data = copy.deepcopy(base.structured)
    allowed = {_path_key(f['path']) for f in scope['fields']}
    seen, actual = set(), []
    for change in changes:
        if not isinstance(change, dict) or set(change) != {'path', 'value'}:
            raise PipelineError('revision_scope', '补丁只能包含 path 和 value。')
        path, value = change['path'], change['value']
        key = _path_key(path)
        if key not in allowed or key in seen or not isinstance(value, str) or len(value) > MAX_CELL:
            raise PipelineError('revision_scope', '补丁越出本次字段范围、重复或内容过长，原稿未修改。')
        seen.add(key)
        before = _cell(data, path)
        if before == value.strip():
            continue
        node = data
        for part in path[:-1]:
            node = node[part]
        node[path[-1]] = value.strip()
        actual.append({'path': path, 'before': before, 'after': value.strip()})
    if not actual:
        raise PipelineError('revision_scope', '补丁没有实际变化，未创建重复版本。')
    if base.stage == 'writing' and any(c['path'][-1] in ('tc', 'duration') for c in actual):
        data['total_duration'] = '待复核（镜头时码或时长已修改）'
        note = '局部修改了镜头时码或时长；需重新核对全片时长、衔接与节奏。'
        data['assumptions'] = list(dict.fromkeys([*data.get('assumptions', []), note]))
    normalized = normalize(base.stage, data)
    if normalized.dropped or normalized.data != data:
        raise PipelineError('revision_scope', '补丁不能无损保存，请保留合法字段内容。')
    from .materials import resolve
    for change in actual:
        if change['path'][-1] == 'file' and change['after'] and resolve(store, run_id, change['after']) is None:
            raise PipelineError('revision_scope', '素材文件不在当前项目中，原稿未修改。')
    return {'content': '', 'summary': summary.strip(), 'structured': data,
            'evidence': list(base.evidence), 'limitations': list(base.limitations)}, actual


def record_applied(store, pipeline, run_id, scope, artifact, changes):
    impact = work_plan(pipeline, run_id)
    store.append_event(run_id, 'agent_revision_applied', plan_id=scope['plan_id'],
                       stage=artifact.stage, base_version=scope['version'], base_sha256=scope['sha256'],
                       version=artifact.version, sha256=artifact.sha256, changes=changes,
                       affected_stages=scope['affected_stages'])
    return impact
