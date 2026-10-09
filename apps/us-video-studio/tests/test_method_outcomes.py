"""Exact same-task result links; no inferred scores, causal effect or calls."""
import hashlib

import pytest

from studio.conversation_input import accept, receive, start_execution
from studio.learning import LearningConflict
from studio.method_outcomes import compare_methods, method_results
from studio.method_selection import record_method_usage
from test_learning import env, feedback


def usage(env, version, *, seed='a' * 64, with_binding=True, text='Same original task'):
    learning, store, direction = env
    artifact = store.write_artifact('run-one', 'writing', version, f'Exact result {version}',
        summary='Output', author_role='writer', attempt=f'writing-task-{version}',
        inputs={'direction': direction.sha256})
    if with_binding:
        receipt = receive(store, 'run-one', text, f'user-source-{version}')
        accept(store, 'run-one', receipt)
        start_execution(store, 'run-one', receipt)
        store.append_event('run-one', 'agent_task_prepared', task_id=artifact.attempt,
            stage='writing', inputs=artifact.inputs,
            context={'seed_sha256': seed, 'sources': [{'kind': 'user_message',
                'message_id': receipt['message_id'], 'sha256': hashlib.sha256(text.encode()).hexdigest()}]})
    directory = learning.method_summaries(run_id='run-one')
    method = directory['methods'][0]
    event = record_method_usage(store, learning, 'run-one', {
        'request_id': f'usage-{version}', 'method_id': method['id'], 'revision': method['revision'],
        'library_sha256': directory['library_sha256'], 'purpose': 'message',
        'decision': 'chosen', 'reason': 'Synthetic test selection', 'stage': 'writing',
        'version': version, 'artifact_sha256': artifact.sha256, 'passage': f'Exact result {version}'})
    return artifact, event


def test_comparison_requires_real_executed_sources_and_exact_same_seed(env):
    _, store, _ = env
    usage(env, 1)
    usage(env, 2)
    before = store.read_events('run-one')
    result = compare_methods(store, env[0], 'run-one', 'usage-1', 'usage-2')
    assert result['same_task_inputs_verified'] is True
    assert result['same_inputs_verified'] is False  # No full provider/media equivalence claim.
    assert result['causal_effect'] == 'not_established'
    assert result['creative_acceptance'] == 'not_assessed'
    assert all(row['method']['method_snapshot_ref'] for row in result['runs'])
    assert store.read_events('run-one') == before


@pytest.mark.parametrize('kwargs', [{'seed': 'b' * 64}, {'text': 'A changed request'}, {'with_binding': False}])
def test_changed_or_missing_task_inputs_never_become_same_question(env, kwargs):
    usage(env, 1)
    usage(env, 2, **kwargs)
    result = compare_methods(env[1], env[0], 'run-one', 'usage-1', 'usage-2')
    assert not result['same_task_inputs_verified']
    assert result['status'] == 'not_ready_for_same_input_comparison'
    assert result['missing']


def test_only_applied_exact_version_feedback_is_linked_without_effect_claim(env):
    learning, store, _ = env
    first, _ = usage(env, 1)
    second, _ = usage(env, 2)
    proposal = learning.propose_feedback('run-one', feedback(first), actor='user')
    assert method_results(store, learning, 'run-one')[0]['observations'] == []
    learning.apply('run-one', proposal['id'], proposal['base_library_sha256'])
    results = method_results(store, learning, 'run-one')
    assert results[0]['observations'][0]['claim'] == '人物说话太像念说明'
    assert results[1]['observations'] == []
    assert all(row['causal_effect'] == 'not_established' for row in results)


def test_tampered_output_blocks_comparison_and_never_rewrites_evidence(env):
    learning, store, _ = env
    first, _ = usage(env, 1)
    usage(env, 2)
    path, _ = store._artifact_paths('run-one', first.stage, first.version)
    path.write_text('Tampered', encoding='utf-8')
    with pytest.raises(LearningConflict, match='准确作品无法核对'):
        compare_methods(store, learning, 'run-one', 'usage-1', 'usage-2')
    assert path.read_text(encoding='utf-8') == 'Tampered'


def test_usage_identity_is_project_scoped_and_same_artifact_not_two_results(env):
    learning, store, _ = env
    artifact, original = usage(env, 1)
    payload = {key: original[key] for key in ('method_id', 'revision', 'library_sha256', 'purpose', 'decision', 'reason')}
    payload.update(request_id='usage-other', stage=artifact.stage, version=artifact.version,
                   artifact_sha256=artifact.sha256, passage='Exact result')
    record_method_usage(store, learning, 'run-one', payload)
    with pytest.raises(ValueError, match='同一份作品'):
        compare_methods(store, learning, 'run-one', 'usage-1', 'usage-other')
    with pytest.raises(ValueError, match='不属于当前项目'):
        compare_methods(store, learning, 'run-one', 'usage-1', 'unknown')
