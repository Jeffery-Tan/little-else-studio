"""Durable task/inbox behavior in disposable stores, without model calls."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from studio.agent_runtime import AgentRuntime, bind_task, current_task_id, reset_task
from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.store import Store


@pytest.fixture
def project(tmp_path):
    store = Store(tmp_path)
    store.create_run('runtime-test', 'Offline project', 'Synthetic brief')
    pipeline = Pipeline(store)
    intake = pipeline.start_attempt('runtime-test', 'intake')
    pipeline.submit('runtime-test', 'intake', intake.token,
                    content='Known synthetic facts', summary='facts')
    runtime = AgentRuntime(store, 'runtime-test')
    runtime.create_task('job-1', role='orchestrator', parent_job_id='job-1',
                        source_message_id='message-1', allowed_tools=['dispatch'])
    runtime.transition('job-1', 'running')
    return store, pipeline, runtime


def child(project):
    _, pipeline, runtime = project
    attempt = pipeline.start_attempt('runtime-test', 'direction')
    runtime.create_task(attempt.key, role='director', stage='direction',
                        parent_task_id='job-1', parent_job_id='job-1',
                        source_message_id='message-1', allowed_tools=['submit_stage'],
                        expected_result={'artifact_stage': 'direction', 'user_decision_required': True})
    return attempt


def test_contract_creation_is_idempotent_and_read_projection_never_mutates(project):
    store, _, runtime = project
    before = store.read_events('runtime-test')
    same = runtime.create_task('job-1', role='orchestrator', parent_job_id='job-1',
                               source_message_id='message-1', allowed_tools=['dispatch'])
    assert same['status'] == 'running'
    restored = AgentRuntime(store, 'runtime-test')
    assert restored.snapshot() == runtime.snapshot()
    assert store.read_events('runtime-test') == before
    with pytest.raises(PipelineError, match='another contract'):
        runtime.create_task('job-1', role='orchestrator', source_message_id='different')


def test_child_reuses_real_attempt_and_role_tools_cannot_expand(project):
    _, _, runtime = project
    attempt = child(project)
    task = runtime.task(attempt.key)
    assert task['parent_task_id'] == 'job-1'
    assert task['source_message_id'] == 'message-1'
    with pytest.raises(PipelineError, match='existing Pipeline attempt'):
        runtime.create_task('direction#999', role='director', stage='direction')
    with pytest.raises(PipelineError, match='does not own'):
        runtime.create_task('wrong-role', role='writer', stage='direction')
    with pytest.raises(PipelineError, match='exceed'):
        runtime.create_task('wrong-tools', role='director', stage='direction', allowed_tools=['dispatch'])
    with pytest.raises(PipelineError, match='user-decision requirement'):
        runtime.create_task('wrong-gate', role='director', stage='direction',
                            expected_result={'user_decision_required': False})


def test_child_inherits_original_job_and_message_and_recovery_can_scope_the_job(project):
    _, pipeline, runtime = project
    attempt = pipeline.start_attempt('runtime-test', 'direction')
    task = runtime.create_task(attempt.key, role='director', stage='direction',
                               parent_task_id='job-1', context_refs={'inputs': {
                                   'intake': 'a' * 64, 'evidence:generation': '3',
                                   'token': 'do-not-project'}})
    assert task['parent_job_id'] == 'job-1'
    assert task['source_message_id'] == 'message-1'
    assert task['context_refs']['inputs'] == {'intake': 'a' * 64, 'evidence:generation': '3'}
    runtime.create_task('other-job', role='orchestrator', parent_job_id='other-job')
    runtime.interrupt_open_tasks(parent_job_id='job-1')
    assert runtime.task(attempt.key)['status'] == 'interrupted'
    assert runtime.task('other-job')['status'] == 'queued'


def test_role_completion_requires_its_real_verified_artifact(project):
    store, pipeline, runtime = project
    attempt = child(project)
    runtime.transition(attempt.key, 'running')
    with pytest.raises(PipelineError, match='requires an exact saved artifact'):
        runtime.transition(attempt.key, 'completed')
    with pytest.raises(PipelineError, match='owned child'):
        runtime.transition('job-1', 'completed')
    artifact = pipeline.submit('runtime-test', 'direction', attempt.token,
                               content='Synthetic options', summary='options')
    receipt = {'stage': 'direction', 'version': artifact.version, 'sha256': artifact.sha256}
    complete = runtime.transition(attempt.key, 'completed', artifact_receipt=receipt)
    assert complete['artifact_receipt']['key'] == 'direction-v1'
    before = store.read_events('runtime-test')
    assert runtime.transition(attempt.key, 'completed', artifact_receipt=receipt) == complete
    assert store.read_events('runtime-test') == before
    runtime.transition('job-1', 'completed')
    with pytest.raises(PipelineError, match='terminal tasks never restart'):
        runtime.transition(attempt.key, 'running')


def test_another_attempt_artifact_or_changed_bytes_cannot_complete_task(project):
    store, pipeline, runtime = project
    first = child(project)
    artifact = pipeline.submit('runtime-test', 'direction', first.token,
                               content='First saved direction', summary='first')
    runtime.transition(first.key, 'running')
    runtime.transition(first.key, 'failed', reason='Synthetic failure')
    second = child(project)
    runtime.transition(second.key, 'running')
    receipt = {'stage': 'direction', 'version': artifact.version, 'sha256': artifact.sha256}
    with pytest.raises(PipelineError, match='does not match'):
        runtime.transition(second.key, 'completed', artifact_receipt=receipt)
    fresh = pipeline.submit('runtime-test', 'direction', second.token,
                            content='Second saved direction', summary='second')
    source = store.run_dir('runtime-test') / 'artifacts' / fresh.filename
    source.write_text('tampered fixture bytes', encoding='utf-8')
    with pytest.raises(PipelineError, match='does not match'):
        runtime.transition(second.key, 'completed', artifact_receipt={
            'stage': 'direction', 'version': fresh.version, 'sha256': fresh.sha256})


def test_stop_is_owned_by_parent_and_does_not_falsely_report_finished_work(project):
    _, _, runtime = project
    attempt = child(project)
    runtime.transition(attempt.key, 'running')
    runtime.request_stop('job-1', reason='Explicit user stop')
    assert runtime.task('job-1')['status'] == 'running'
    assert runtime.task(attempt.key)['stop_requested'] is True
    with pytest.raises(PipelineError, match='stopped task'):
        runtime.transition(attempt.key, 'waiting')
    runtime.transition(attempt.key, 'cancelled', reason='Executor observed stop')
    runtime.transition('job-1', 'cancelled', reason='Executor observed stop')
    with pytest.raises(PipelineError, match='terminal tasks never restart'):
        runtime.transition('job-1', 'running')


def test_restart_marks_open_tasks_interrupted_without_consuming_inbox_or_replaying(project):
    store, _, runtime = project
    attempt = child(project)
    runtime.post_message(attempt.key, 'inbox-1', 'Saved correction', source_message_id='user-2')
    recreated = AgentRuntime(store, 'runtime-test')
    assert recreated.task(attempt.key)['status'] == 'queued'
    recreated.interrupt_open_tasks()
    assert {task['status'] for task in recreated.tasks()} == {'interrupted'}
    assert recreated.pending_messages(attempt.key)[0]['text'] == 'Saved correction'
    before = store.read_events('runtime-test')
    recreated.interrupt_open_tasks()
    assert store.read_events('runtime-test') == before
    with pytest.raises(PipelineError, match='terminal tasks never restart'):
        recreated.transition(attempt.key, 'running')
    with pytest.raises(PipelineError, match='stopped tasks cannot consume'):
        recreated.ack_message(attempt.key, 'inbox-1')


def test_inbox_exact_identity_acknowledgement_and_parent_child_permissions(project):
    store, _, runtime = project
    attempt = child(project)
    runtime.post_message(attempt.key, 'parent-note', 'Read these facts',
                         kind='parent', source_task_id='job-1')
    before = store.read_events('runtime-test')
    runtime.post_message(attempt.key, 'parent-note', 'Read these facts',
                         kind='parent', source_task_id='job-1')
    assert store.read_events('runtime-test') == before
    with pytest.raises(PipelineError, match='different input'):
        runtime.post_message(attempt.key, 'parent-note', 'Other facts',
                             kind='parent', source_task_id='job-1')
    with pytest.raises(PipelineError, match='ownership'):
        runtime.post_message(attempt.key, 'wrong-direction', 'Wrong sender kind',
                             kind='child', source_task_id='job-1')
    with pytest.raises(PipelineError, match='does not belong'):
        runtime.ack_message('job-1', 'parent-note')
    runtime.ack_message(attempt.key, 'parent-note')
    assert runtime.pending_messages(attempt.key) == []
    runtime.post_message('job-1', 'child-note', 'Artifact prepared',
                         kind='child', source_task_id=attempt.key)
    assert runtime.pending_messages('job-1')[0]['source_task_id'] == attempt.key


def test_public_projection_keeps_context_identity_and_hides_prompt_tokens_and_inbox(project):
    _, _, runtime = project
    runtime.create_task('job-2', role='orchestrator', context_refs={
        'system_sha256': 'a' * 64, 'token': 'private-token', 'system': 'private prompt',
        'sources': [{'stage': 'writing', 'version': 3, 'sha256': 'b' * 64,
                     'tool_args': {'token': 'secret'}}]})
    runtime.post_message('job-2', 'user-note', 'Private message body', source_message_id='user-3')
    task = next(row for row in runtime.snapshot()['tasks'] if row['task_id'] == 'job-2')
    assert task['context_refs']['system_sha256'] == 'a' * 64
    assert task['context_refs']['sources'] == [{'stage': 'writing', 'version': 3, 'sha256': 'b' * 64}]
    assert 'private' not in str(task).lower() and 'token' not in str(task)
    assert task['pending_message_count'] == 1
    assert task['messages'][0]['source_message_id'] == 'user-3'


def test_active_task_scope_is_nested_and_not_shared_with_other_threads():
    assert current_task_id() is None
    parent = bind_task('parent-job')
    try:
        child_token = bind_task('direction#1')
        try:
            assert current_task_id() == 'direction#1'
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(current_task_id).result() is None
        finally:
            reset_task(child_token)
        assert current_task_id() == 'parent-job'
    finally:
        reset_task(parent)
    assert current_task_id() is None
