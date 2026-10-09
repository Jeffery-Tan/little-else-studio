"""Actual approval HTTP and production handlers; synthetic models and temp runs."""
from copy import deepcopy
from dataclasses import replace
import time

import pytest

from fakes import ScriptedModel, final_turn, tool_turn
from local_http import TestClient
from studio.agents.orchestrator import Orchestrator
from studio.decision import continuation_request_id
from studio.decision_continuation import freeze, projection, validate
from studio.models import PipelineError
from studio.production import draft_records
from studio.server import build_app
from studio.timeline import build_timeline
from studio.tools.workspace import Workspace
from test_revision_scope import env, RUN, write


def source(env):
    store, pipe, _, _ = env
    data = deepcopy(store.latest_artifact(RUN, 'writing').structured)
    extra = deepcopy(data['segments'])
    for row in extra:
        row.pop('shot_id', None)
        row.pop('page_id', None)
    data['segments'] += extra
    return write(pipe, 'writing', data)


def approve_ui(env, stage='writing'):
    store, pipe, _, _ = env
    return pipe.record_approval(RUN, stage, nonce=pipe.issue_nonce(RUN, stage),
        decision='approved', source='ui', continue_after=True,
        selected_option_id='A' if stage == 'direction' else None)


def main(env, context):
    store, pipe, settings, ledger = env
    producer = Orchestrator(store, pipe, replace(settings, max_tool_rounds=1),
                            ScriptedModel([final_turn('Synthetic pending')]), ledger, RUN)
    producer.send('SERVER_DERIVED_PREPARATION_NOT_USER_WORDS', internal_instruction=True,
                  decision_context=context)
    return producer, Workspace(store, pipe, RUN, 'orchestrator')


def wait_job(app, job_id):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        row = app.state.jobs.get(RUN, job_id)
        if row['status'] not in {'queued', 'running', 'cancelling'}:
            return row
        time.sleep(.1)
    pytest.fail('Synthetic job did not terminate')


def test_actual_writing_button_prepares_four_inputs_without_new_user_or_media_authority(env):
    store, pipe, settings, ledger = env
    artifact = source(env)
    payload = {'kind': 'image', 'model': 'codex-image', 'prompt': 'Synthetic input only',
               'source_version': artifact.version, 'source_sha256': artifact.sha256}
    model = ScriptedModel([
        *[tool_turn(f'input-{i}', 'prepare_production', {**payload, 'request_id': f'offline-input-{i}',
                                                      'shot_id': f'shot-{i:02}'}) for i in range(1, 5)],
        final_turn('四镜输入已保存，尚未生成媒体。')])
    app = build_app(settings, client_factory=lambda: model)
    with TestClient(app) as http:
        shown = {'stage': 'writing', 'expected_version': artifact.version,
                 'expected_artifact_sha256': artifact.sha256}
        nonce = http.post(f'/api/runs/{RUN}/nonce', params=shown).json()['nonce']
        result = http.post(f'/api/runs/{RUN}/decision', json={**shown, 'nonce': nonce,
                          'decision': 'approved', 'continue_after': True})
        assert result.status_code == 200, result.text
        job = wait_job(app, result.json()['continuation']['job_id'])
        assert job['status'] == 'succeeded', job['reason']
        assert job['deliverable']['goal'] == 'production_input'
        assert job['deliverable']['status'] == 'complete'
        assert len(draft_records(store, RUN)) == 4
        events = store.read_events(RUN)
        assert not any(e['kind'] in {'user_message_received', 'execution_authorized', 'production_attempt'} for e in events)
        created = next(e for e in events if e['kind'] == 'job_created' and e['job_id'] == job['job_id'])
        assert created['user_input_id'] is None
        assert created['job_payload']['decision_context']['approval']['artifact_sha256'] == artifact.sha256
        assert store.latest_artifact(RUN, 'writing') == artifact
        assert len(store.list_approvals(RUN, 'writing')) == 2
        timeline = http.get(f'/api/runs/{RUN}/timeline').json()
        receipt = [i for t in timeline['turns'] for i in t['items'] if i['kind'] == 'approval'][-1]
        assert receipt['continuation']['status'] == 'succeeded'


@pytest.mark.parametrize('change', [
    {'intent': 'new'}, {'format': 'photo'}, {'deliverable': 'production_media'},
    {'production_kind': 'video'}, {'creation_mode': 'recreate'}, {'reference_scope': 'new'},
])
def test_frozen_scope_rejects_model_rerouting(env, change):
    source(env)
    approval = approve_ui(env)
    context = freeze(env[0], env[1], RUN, continuation_request_id(approval))
    producer, workspace = main(env, context)
    args = {'intent': 'continue', 'format': 'video', 'deliverable': 'production_input',
            'production_kind': 'image', **change}
    with pytest.raises(Exception, match='冻结'):
        producer._execute('prepare_creative_context', args, workspace)
    assert not draft_records(env[0], RUN)


def test_preselected_scope_does_not_accept_fabricated_interpretation(env):
    source(env)
    context = freeze(env[0], env[1], RUN, continuation_request_id(approve_ui(env)))
    producer, workspace = main(env, context)
    with pytest.raises(Exception, match='没有新的用户消息'):
        producer._execute('propose_task_interpretation', {}, workspace)
    producer._execute('prepare_creative_context', {'intent': 'continue', 'format': 'video',
                       'deliverable': 'production_input', 'production_kind': 'image'}, workspace)
    with pytest.raises(Exception, match='分镜制作'):
        producer._execute('dispatch', {'stage': 'writing', 'brief': 'Wrong rewrite'}, workspace)
    assert not any(e['kind'] == 'user_message_received' for e in env[0].read_events(RUN))


@pytest.mark.parametrize('mutation', ['writing', 'intake', 'format', 'reference', 'envelope'])
def test_scope_rechecks_accurate_sources_and_persisted_envelope(env, mutation):
    store, pipe, _, _ = env
    source(env)
    context = freeze(store, pipe, RUN, continuation_request_id(approve_ui(env)))
    if mutation in {'writing', 'intake'}:
        write(pipe, mutation, store.latest_artifact(RUN, mutation).structured)
    elif mutation == 'format':
        store.append_event(RUN, 'creative_context_selected', selection={'intent': 'continue', 'format': 'photo'})
    elif mutation == 'reference':
        store.append_event(RUN, 'creation_mode_selected', selection={'mode': 'inspiration', 'reference_id': 'foreign', 'scope': 'new'})
    else:
        context = deepcopy(context)
        context['scope']['production_kind'] = 'video'
    with pytest.raises(PipelineError):
        validate(store, pipe, RUN, context)
    assert not draft_records(store, RUN)


def test_direction_next_scope_is_only_material_and_writing_and_freeze_is_idempotent(env):
    store, pipe, _, _ = env
    write(pipe, 'direction', store.latest_artifact(RUN, 'direction').structured)
    approval = approve_ui(env, 'direction')
    request_id = continuation_request_id(approval)
    context = freeze(store, pipe, RUN, request_id)
    assert context['scope']['allowed_stages'] == ['material_plan', 'writing']
    assert context['scope']['deliverable'] == 'writing'
    assert freeze(store, pipe, RUN, request_id) == context
    assert sum(e['kind'] == 'decision_continuation_scope_frozen' for e in store.read_events(RUN)) == 1
    producer, workspace = main(env, context)
    with pytest.raises(Exception, match='阶段超出'):
        producer._execute('dispatch', {'stage': 'direction', 'brief': 'Wrong'}, workspace)


def failed_ui(env, status='blocked'):
    store, pipe, _, _ = env
    source(env)
    approval = approve_ui(env)
    store.append_event(RUN, 'job_created', job_id='original', request_id=continuation_request_id(approval),
                       job_kind='conversation', text='Synthetic original', user_input_id=None)
    store.append_event(RUN, 'job_status', job_id='original', status=status, reason='Exact preparation failure')
    return approval


@pytest.mark.parametrize('status', ['queued', 'running', 'succeeded', 'blocked', 'failed', 'cancelled', 'interrupted'])
def test_ui_receipt_projects_exact_job_without_borrowing_chat(env, status):
    store, pipe, _, ledger = env
    approval = failed_ui(env, status)
    store.append_event(RUN, 'job_created', job_id='foreign', request_id='another', text='Foreign')
    store.append_event(RUN, 'job_status', job_id='foreign', status='succeeded', reason='Foreign reason')
    value = projection(store, pipe, RUN, approval, store.read_events(RUN))
    assert value['job_id'] == 'original' and value['status'] == status
    assert value['reason'] == 'Exact preparation failure'
    assert ('retry' in value) == (status in {'blocked', 'failed'})
    tl = build_timeline(store, pipe, ledger, RUN)
    assert not any(t['user'] for t in tl['turns'])
    assert store.read_chat(RUN) == []


def test_explicit_fresh_preparation_reuses_approval_and_deduplicates_new_request(env):
    store, pipe, settings, _ = env
    approval = failed_ui(env)
    model = ScriptedModel([final_turn('Synthetic incomplete, no generation')])
    app = build_app(replace(settings, max_tool_rounds=1), client_factory=lambda: model)
    payload = {'stage': 'writing', 'expected_version': approval.artifact_version,
               'expected_artifact_sha256': approval.artifact_sha256,
               'failed_job_id': 'original', 'request_id': 'fresh-request-01'}
    with TestClient(app) as http:
        result = http.post(f'/api/runs/{RUN}/decision-preparation', json=payload)
        assert result.status_code == 200, result.text
        job_id = result.json()['job_id']
        wait_job(app, job_id)
        again = http.post(f'/api/runs/{RUN}/decision-preparation', json=payload)
        assert again.status_code == 200 and again.json()['job_id'] == job_id
        assert len([e for e in store.read_events(RUN) if e['kind'] == 'job_created']) == 2
        assert len(store.list_approvals(RUN, 'writing')) == 2
        assert app.state.jobs.get(RUN, 'original')['status'] == 'blocked'
        assert not any(e['kind'] == 'execution_authorized' for e in store.read_events(RUN))


@pytest.mark.parametrize('change', [{'failed_job_id': 'foreign'}, {'expected_version': 1},
                                  {'expected_artifact_sha256': 'f'*64}])
def test_fresh_preparation_rejects_wrong_failure_or_version(env, change):
    store, _, settings, _ = env
    approval = failed_ui(env)
    model = ScriptedModel([])
    app = build_app(settings, client_factory=lambda: model)
    payload = {'stage': 'writing', 'expected_version': approval.artifact_version,
               'expected_artifact_sha256': approval.artifact_sha256,
               'failed_job_id': 'original', 'request_id': 'fresh-request-01', **change}
    with TestClient(app) as http:
        result = http.post(f'/api/runs/{RUN}/decision-preparation', json=payload)
        assert result.status_code == 409
        assert not model.calls


@pytest.mark.parametrize('status', ['running', 'queued', 'succeeded', 'cancelled', 'interrupted'])
def test_fresh_preparation_does_not_replay_pending_success_unknown_or_stop(env, status):
    store, _, settings, _ = env
    approval = failed_ui(env, status)
    model = ScriptedModel([])
    app = build_app(settings, client_factory=lambda: model)
    payload = {'stage': 'writing', 'expected_version': approval.artifact_version,
               'expected_artifact_sha256': approval.artifact_sha256,
               'failed_job_id': 'original', 'request_id': 'fresh-request-01'}
    with TestClient(app) as http:
        result = http.post(f'/api/runs/{RUN}/decision-preparation', json=payload)
        assert result.status_code == 409
        assert not model.calls
        assert len([e for e in store.read_events(RUN) if e['kind'] == 'job_created']) == 1


def test_fresh_preparation_cannot_start_while_another_job_is_busy(env):
    store, _, settings, _ = env
    approval = failed_ui(env)
    model = ScriptedModel([])
    app = build_app(settings, client_factory=lambda: model)
    with TestClient(app) as http:
        app.state.jobs.active = (RUN, 'other-busy')
        result = http.post(f'/api/runs/{RUN}/decision-preparation', json={
            'stage': 'writing', 'expected_version': approval.artifact_version,
            'expected_artifact_sha256': approval.artifact_sha256,
            'failed_job_id': 'original', 'request_id': 'fresh-request-01'})
        app.state.jobs.active = None
        assert result.status_code == 409
        assert not model.calls
        assert len([e for e in store.read_events(RUN) if e['kind'] == 'job_created']) == 1


def test_returned_and_real_conversation_receipts_do_not_gain_ui_scope(env):
    store, pipe, _, _ = env
    source(env)
    returned = pipe.record_approval(RUN, 'writing', nonce=pipe.issue_nonce(RUN, 'writing'),
        decision='returned', note='Synthetic actual UI requested change', source='ui', continue_after=True)
    assert freeze(store, pipe, RUN, continuation_request_id(returned)) is None
    source(env)
    natural = pipe.record_approval(RUN, 'writing', nonce=pipe.issue_nonce(RUN, 'writing'),
        decision='approved', source='conversation', continue_after=True)
    assert freeze(store, pipe, RUN, continuation_request_id(natural)) is None
    assert not any(e['kind'] == 'decision_continuation_scope_frozen' for e in store.read_events(RUN))


def test_unknown_next_step_is_rejected_without_freezing_or_execution(env, monkeypatch):
    store, pipe, _, _ = env
    source(env)
    approval = replace(approve_ui(env), stage='material_plan')
    monkeypatch.setattr(store, 'list_approvals', lambda *a: [approval])
    with pytest.raises(PipelineError, match='唯一可用'):
        freeze(store, pipe, RUN, continuation_request_id(approval))
    assert not any(e['kind'] == 'decision_continuation_scope_frozen' for e in store.read_events(RUN))


def test_readonly_format_none_does_not_change_frozen_project_format(env):
    store, pipe, _, _ = env
    source(env)
    context = freeze(store, pipe, RUN, continuation_request_id(approve_ui(env)))
    store.append_event(RUN, 'creative_context_selected', selection={'intent': 'status', 'format': 'none'})
    assert validate(store, pipe, RUN, context)['format'] == 'video'


def test_delegated_submit_rechecks_ui_scope_after_model_returns(env):
    store, pipe, settings, ledger = env
    write(pipe, 'direction', store.latest_artifact(RUN, 'direction').structured)
    context = freeze(store, pipe, RUN, continuation_request_id(approve_ui(env, 'direction')))
    previous = store.latest_artifact(RUN, 'material_plan')
    class ChangedReference(ScriptedModel):
        def complete(self, **kwargs):
            store.append_event(RUN, 'creation_mode_selected', selection={
                'mode': 'inspiration', 'reference_id': 'foreign', 'scope': 'Changed during model'})
            return super().complete(**kwargs)
    model = ChangedReference([tool_turn('save', 'submit_stage', {
        'content': 'Synthetic material', 'summary': 'Synthetic material',
        'structured': previous.structured})])
    from studio.tools.dispatch import Dispatcher
    result = Dispatcher(store, pipe, replace(settings, max_tool_rounds=1), model, ledger, RUN).dispatch(
        'material_plan', 'Read the adopted direction, prepare material only',
        turn_scope={'decision_context': context})
    assert result.submitted is None
    assert store.latest_artifact(RUN, 'material_plan') == previous
    assert len(model.calls) == 1
