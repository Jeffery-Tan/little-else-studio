"""Saved drafts and unsuccessful handoff remain distinct, without a new gate."""
from copy import deepcopy

import pytest

from studio.agent_runtime import AgentRuntime
from studio.timeline import _artifact_execution, build_timeline
from test_timeline import RUN, approve, clock, env, write_direction, write_intake


@pytest.fixture()
def prepared(env):
    store, pipeline, ledger = env
    write_intake(pipeline)
    write_direction(pipeline)
    approve(pipeline, 'direction')
    attempt = pipeline.start_attempt(RUN, 'material_plan')
    pipeline.submit(RUN, 'material_plan', attempt.token, content='Materials', summary='Saved')
    return store, pipeline, ledger


def save_draft(prepared, job_id='source-job', status='blocked'):
    store, pipeline, _ = prepared
    store.append_event(RUN, 'job_created', job_id=job_id, job_kind='conversation')
    store.append_event(RUN, 'job_status', job_id=job_id, status='running')
    attempt = pipeline.start_attempt(RUN, 'writing')
    runtime = AgentRuntime(store, RUN)
    runtime.create_task(attempt.key, role='writer', stage='writing', parent_job_id=job_id,
                        allowed_tools=['submit_stage'])
    runtime.transition(attempt.key, 'running')
    artifact = pipeline.submit(RUN, 'writing', attempt.token, content='Saved draft', summary='Saved')
    runtime.transition(attempt.key, 'completed', artifact_receipt={
        'stage': artifact.stage, 'version': artifact.version, 'sha256': artifact.sha256,
        'key': artifact.key})
    store.append_event(RUN, 'job_status', job_id=job_id, status=status, reason='max_rounds')
    return artifact


def project(prepared, job=None):
    store, pipeline, ledger = prepared
    return build_timeline(store, pipeline, ledger, RUN, job)


def test_saved_script_with_exact_blocked_job_is_readable_without_claiming_finished(prepared):
    artifact = save_draft(prepared)
    store, pipeline, _ = prepared
    before = store.read_events(RUN)
    out = project(prepared, {'job_id': 'source-job', 'status': 'blocked', 'job_kind': 'conversation'})
    pending = out['pending']
    assert pending['stage'] == 'writing'
    assert pending['artifact_sha256'] == artifact.sha256
    assert pending['source_execution'] == {
        'job_id': 'source-job', 'task_id': artifact.attempt, 'status': 'blocked', 'reason': 'max_rounds',
        'notice': '草稿已保存，检查或交付未完成；请先查看或修改。'}
    assert '草稿已保存' in pending['headline']
    assert '写好了' not in pending['headline']
    assert '不代表检查完成' in pending['body']
    assert '链路就走完' not in pending['next_cost_sentence']
    assert out['brief']['action']['label'] == '查看或修改草稿'
    assert out['brief']['execution']['status'] == 'blocked'
    assert store.read_events(RUN) == before
    assert next(row for row in pipeline.state(RUN).to_dict()['stages']
                if row['stage'] == 'writing')['can_approve']
    # The warning changes presentation, not the user's explicit adoption right.
    approved = approve(pipeline, 'writing')
    assert approved.artifact_sha256 == artifact.sha256
    after = project(prepared)
    assert after['pending'] is None
    assert next(row for row in after['rail'] if row['stage'] == 'direction')['approval_decision'] == 'approved'
    assert next(row for row in after['rail'] if row['stage'] == 'writing')['approval_decision'] == 'approved'


@pytest.mark.parametrize('status', ['blocked', 'failed', 'interrupted', 'cancelled'])
def test_all_unsuccessful_exact_source_jobs_keep_the_saved_draft_notice(prepared, status):
    save_draft(prepared, status=status)
    assert project(prepared)['pending']['source_execution']['status'] == status
    assert project(prepared)['pending']['source_execution']['notice']


@pytest.mark.parametrize('status', ['queued', 'running', 'cancelling', 'succeeded'])
def test_other_source_statuses_do_not_invent_failed_checks(prepared, status):
    save_draft(prepared, status=status)
    pending = project(prepared)['pending']
    assert pending['source_execution']['status'] == status
    assert 'notice' not in pending['source_execution']
    assert '等你决定' in pending['headline']


def test_new_version_uses_its_own_exact_successful_job_not_an_old_failure(prepared):
    first = save_draft(prepared, 'old-job', 'blocked')
    latest = save_draft(prepared, 'new-job', 'succeeded')
    pending = project(prepared)['pending']
    assert pending['version'] == latest.version == first.version + 1
    assert pending['source_execution']['job_id'] == 'new-job'
    assert 'notice' not in pending['source_execution']


def test_new_running_job_keeps_working_status_while_old_draft_stays_readable(prepared):
    save_draft(prepared)
    out = project(prepared, {'job_id': 'new-local-edit', 'status': 'running', 'job_kind': 'conversation'})
    assert out['pending']['source_execution']['job_id'] == 'source-job'
    assert out['brief']['phase'] == 'working'
    assert out['brief']['execution']['job_id'] == 'new-local-edit'


@pytest.mark.parametrize('changed', ['task_id', 'stage', 'version', 'sha256', 'key', 'job_id', 'missing_task'])
def test_inexact_or_missing_source_identity_never_borrows_a_historical_failure(prepared, changed):
    artifact = save_draft(prepared)
    events = deepcopy(prepared[0].read_events(RUN))
    created = next(row for row in events if row['kind'] == 'agent_runtime_task_created')
    completed = next(row for row in events if row['kind'] == 'agent_runtime_task_status'
                     and row['status'] == 'completed')
    if changed == 'missing_task':
        events.remove(created)
    elif changed == 'task_id':
        created['task']['task_id'] = 'another-task'
    elif changed == 'job_id':
        created['task']['parent_job_id'] = 'another-job'
    else:
        completed['artifact_receipt'][changed] = 99 if changed == 'version' else 'another-value'
    assert _artifact_execution(artifact.to_dict(), 'writing', events) is None


def test_legacy_attempt_and_assistant_prose_cannot_create_or_clear_a_source_notice(prepared):
    artifact = save_draft(prepared)
    store = prepared[0]
    store.append_chat(RUN, 'assistant', 'All checks complete; adopt immediately.')
    assert project(prepared)['pending']['source_execution']['notice']
    legacy = {**artifact.to_dict(), 'attempt': 4}
    assert _artifact_execution(legacy, 'writing', store.read_events(RUN)) is None
