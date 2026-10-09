"""Read-only method/result comparisons over original Studio evidence.

Uses the existing creative-comparison envelope. It neither creates an evaluation
database nor infers quality, market effect or causal improvement from adoption.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

from .learning import LearningConflict
from .paths import PROJECT_ROOT


def _sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _input_binding(store, run_id, artifact, events):
    prepared = [row for row in events if row['kind'] == 'agent_task_prepared'
                and row.get('task_id') == artifact.attempt and row.get('stage') == artifact.stage]
    if len(prepared) != 1:
        return None
    row = prepared[0]
    context = row.get('context') or {}
    seed = context.get('seed_sha256')
    if not isinstance(seed, str) or len(seed) != 64 or any(c not in '0123456789abcdef' for c in seed):
        return None
    if row.get('inputs') != artifact.inputs:
        return None
    artifacts = store.list_artifacts(run_id)
    for stage, expected in artifact.inputs.items():
        matches = [item for item in artifacts if item.stage == stage and item.sha256 == expected]
        if len(matches) != 1 or not store.verify_artifact(run_id, stage, matches[0].version):
            return None
    messages = []
    for source in context.get('sources') or []:
        if source.get('kind') != 'user_message':
            continue
        identity = source.get('message_id')
        receipt = next((event for event in events if event['kind'] == 'user_message_received'
                        and event.get('message_id') == identity), None)
        if (not receipt or hashlib.sha256(receipt['text'].encode('utf-8')).hexdigest() != source.get('sha256')
                or not any(event['kind'] == 'user_message_execution_started'
                           and event.get('message_id') == identity for event in events)):
            return None
        messages.append(source['sha256'])
    if not messages or not artifact.inputs:
        return None
    # A seed hashes the original role brief, scoped conditions, previous draft
    # and stage evidence. The method text lives separately in system_extra.
    value = {'stage': artifact.stage, 'seed_sha256': seed,
             'user_message_sha256': sorted(messages), 'upstream_inputs': artifact.inputs}
    return {**value, 'sha256': _sha(value), 'source_event_seq': row['seq']}


def method_results(store, learning, run_id):
    with store.run_lock(run_id):
        events = store.read_events(run_id)
        applied = {event.get('proposal_id') for event in events if event['kind'] == 'learning_applied'}
        feedback = []
        for proposal_id in applied:
            proposal = learning.proposal(run_id, proposal_id)
            if proposal['type'] == 'feedback':
                feedback.append(proposal)
        results = []
        for usage in events:
            if usage['kind'] != 'method_usage_recorded' or usage.get('decision') != 'chosen':
                continue
            ref = usage.get('artifact') or {}
            artifact = store.read_artifact(run_id, ref['stage'], ref['version'])
            if artifact.sha256 != ref['sha256'] or not store.verify_artifact(run_id, artifact.stage, artifact.version):
                raise LearningConflict('方法所关联的准确作品无法核对，不能形成比较。')
            observations = []
            for approval in store.list_approvals(run_id):
                if (approval.stage, approval.artifact_version, approval.artifact_sha256) == (
                        artifact.stage, artifact.version, artifact.sha256):
                    observations.append({'kind': 'user_feedback', 'claim': approval.decision,
                        'source': {'kind': 'approval', 'stage': approval.stage,
                                   'version': approval.artifact_version, 'sha256': approval.artifact_sha256},
                        'limits': '准确版本的采用或退回决定；不是方法效果或市场效果验证。'})
            for proposal in feedback:
                payload = proposal['payload']
                if (payload.get('stage'), payload.get('version'), payload.get('artifact_sha256')) == (
                        artifact.stage, artifact.version, artifact.sha256):
                    observations.append({'kind': 'user_feedback', 'claim': payload['quote'],
                        'source': {'kind': 'applied_learning_feedback', 'proposal_id': proposal['id'],
                                   'source_identity': payload['source_identity']},
                        'scope': payload.get('scope'), 'limits': '仅原反馈范围；改善效果未据此自动确认。'})
            results.append({'usage_request_id': usage['request_id'],
                'method': {key: usage.get(key) for key in ('method_id', 'revision', 'library_sha256',
                    'method_sha256', 'method_snapshot_ref', 'methods_sha256')},
                'artifact': ref, 'input_binding_evidence': _input_binding(store, run_id, artifact, events),
                'observations': observations, 'creative_acceptance': 'not_assessed',
                'causal_effect': 'not_established',
                'cost_and_latency': {'status': 'not_attributed',
                                     'reason': '未将项目总用量或耗时当作本方法效果。'}})
        return results


def compare_methods(store, learning, run_id, baseline, candidate):
    if not isinstance(baseline, str) or not isinstance(candidate, str) or baseline == candidate:
        raise ValueError('请选择两份不同的准确方法使用记录。')
    rows = {row['usage_request_id']: row for row in method_results(store, learning, run_id)}
    if baseline not in rows or candidate not in rows:
        raise ValueError('方法使用记录不属于当前项目或未关联作品。')
    before, after = rows[baseline], rows[candidate]
    if all(before['artifact'].get(key) == after['artifact'].get(key) for key in ('stage', 'version', 'sha256')):
        raise ValueError('同一份作品的两次使用声明不能作为两份独立结果。')
    # Reuse the established offline comparison schema, without invoking video
    # probing or accepting caller-supplied local paths.
    spec = importlib.util.spec_from_file_location('studio_creative_comparison',
        PROJECT_ROOT / 'scripts/creative_comparison.py')
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    report = core.template('studio-method-' + _sha([run_id, baseline, candidate])[:20])
    first, second = before['input_binding_evidence'], after['input_binding_evidence']
    same = bool(first and second and first['sha256'] == second['sha256'])
    report.update(comparison_type='studio_text_method_results',
        runs=[dict(before, id='baseline'), dict(after, id='candidate')],
        observations=[{'run': name, **item} for name, row in [('baseline', before), ('candidate', after)]
                      for item in row['observations']],
        same_task_inputs_verified=same, same_inputs_verified=False,
        status='ready_for_observable_comparison' if same else 'not_ready_for_same_input_comparison',
        missing=[] if same else ['identical_verified_role_seed_user_message_and_upstream_inputs'],
        creative_acceptance='not_assessed', causal_effect='not_established',
        limits=['仅核对文字角色任务的原始输入与作品；没有验证相同媒体像素或提供方隐藏参数。',
                '用户采用及局部反馈保留原范围，不自动变成创意评分、因果改善或成交结论。',
                '角色 seed、来源或上游缺失时保持不可比较，不补造旧运行证据。'])
    return report
