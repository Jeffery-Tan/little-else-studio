"""New direction references fail before persistence; historical gates remain."""
from __future__ import annotations

import copy
from threading import Event

import pytest
from local_http import TestClient

from studio import active_brief as ab
from studio.agents.deepseek_client import DeepSeekClient
from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.server import build_app
from studio.store import Store
from test_direction_command import RUN as COMMAND_RUN, options, seed, settings_for, wait_job
from test_finishing_phase import mock_http, reply


RUN = 'direction-reference-submission'
PRIVATE_BAD_VALUE = 'SYNTHETIC_PRIVATE_BAD_REFERENCE'


def fixture(tmp_path, *, constraints=()):
    store = Store(tmp_path)
    store.create_run(RUN, 'Synthetic candidates', 'Create three distinct photo directions.')
    pipe = Pipeline(store)
    initial = pipe.start_attempt(RUN, 'intake')
    pipe.submit(RUN, 'intake', initial.token, content='', summary='Known synthetic constraints',
                structured={'facts': [{'label': 'Product', 'value': 'Grey lunch bag', 'source': 'fixture'}],
                            'constraints': list(constraints)})
    attempt = pipe.start_attempt(RUN, 'direction')
    return store, pipe, attempt


def payload():
    return {'content': '', 'summary': 'Three synthetic candidates', 'structured': options()}


@pytest.mark.parametrize('field', ['satisfies', 'violates', 'amend_clause'])
@pytest.mark.parametrize('row_index', [0, 1, 2])
def test_unknown_reference_never_saves_and_same_attempt_can_correct(tmp_path, field, row_index):
    store, pipe, attempt = fixture(tmp_path)
    value = payload()
    value['structured']['options'][row_index][field] = PRIVATE_BAD_VALUE
    original = copy.deepcopy(value)
    before = store.read_events(RUN)
    with pytest.raises(PipelineError) as caught:
        pipe.submit(RUN, 'direction', attempt.token, **value)
    assert caught.value.rule == 'brief_reference'
    assert f'structured.options[{row_index}].{field}' in str(caught.value)
    assert 'goal.primary' in str(caught.value)
    assert PRIVATE_BAD_VALUE not in str(caught.value)
    assert value == original, 'Never remove, remap or edit the submitted cell'
    assert store.read_events(RUN) == before
    assert store.latest_artifact(RUN, 'direction') is None
    assert pipe.stage_state(RUN, 'direction').open_attempt.token == attempt.token
    fixed = payload()
    fixed['structured']['options'][row_index][field] = 'goal.primary' if field == 'satisfies' else ''
    saved = pipe.submit(RUN, 'direction', attempt.token, **fixed)
    assert saved.version == 1
    assert all(row['admissible'] for row in pipe.stage_state(RUN, 'direction').brief_gate['options'])
    assert store.list_approvals(RUN) == []


@pytest.mark.parametrize('wrong_namespace', ['content.format', '3a9c237994025af5ca2f3b49'])
def test_user_condition_key_or_record_id_is_not_a_brief_id(tmp_path, wrong_namespace):
    store, pipe, attempt = fixture(tmp_path)
    value = payload()
    for row in value['structured']['options']:
        row['satisfies'] = f'goal.primary,{wrong_namespace}'
    with pytest.raises(PipelineError) as caught:
        pipe.submit(RUN, 'direction', attempt.token, **value)
    assert caught.value.rule == 'brief_reference'
    assert wrong_namespace not in str(caught.value)
    assert store.latest_artifact(RUN, 'direction') is None


@pytest.mark.parametrize('field', ['violates', 'amend_clause'])
def test_known_conflict_is_saved_but_stays_blocked_for_adoption(tmp_path, field):
    store, pipe, attempt = fixture(tmp_path)
    value = payload()
    row = value['structured']['options'][1]
    row[field] = 'goal.primary'
    if field == 'amend_clause':
        row['amend_proposal'] = 'Propose an explicitly different creative goal.'
    saved = pipe.submit(RUN, 'direction', attempt.token, **value)
    assert saved.structured['options'][1][field] == 'goal.primary'
    gate = pipe.stage_state(RUN, 'direction').brief_gate
    assert {r['key']: r['admissible'] for r in gate['options']} == {'A': True, 'B': False, 'C': True}
    with pytest.raises(PipelineError) as caught:
        pipe.record_approval(RUN, 'direction', nonce=pipe.issue_nonce(RUN, 'direction'),
                             decision='approved', selected_option_id='B')
    assert caught.value.rule == 'brief'
    assert store.list_approvals(RUN) == []


def test_current_clause_ids_and_original_separators_remain_valid(tmp_path):
    store, pipe, attempt = fixture(tmp_path, constraints=['Keep supplied shape', 'Do not claim material composition'])
    brief = ab.compile_active_brief(store, RUN)
    ids = [row['id'] for row in brief['clauses']]
    value = payload()
    separator_examples = [',', '；', ' ']
    for row, separator in zip(value['structured']['options'], separator_examples):
        row['satisfies'] = separator.join(ids)
    saved = pipe.submit(RUN, 'direction', attempt.token, **value)
    assert [r['satisfies'] for r in saved.structured['options']] == [
        row['satisfies'] for row in value['structured']['options']]
    assert all(r['admissible'] for r in pipe.stage_state(RUN, 'direction').brief_gate['options'])


def test_amendment_field_does_not_silently_pick_one_of_multiple_ids(tmp_path):
    store, pipe, attempt = fixture(tmp_path, constraints=['Keep supplied shape'])
    ids = [row['id'] for row in ab.compile_active_brief(store, RUN)['clauses']]
    value = payload()
    value['structured']['options'][0]['amend_clause'] = ','.join(ids)
    with pytest.raises(PipelineError) as caught:
        pipe.submit(RUN, 'direction', attempt.token, **value)
    assert caught.value.rule == 'brief_reference'
    assert 'structured.options[0].amend_clause' in str(caught.value)
    assert store.latest_artifact(RUN, 'direction') is None


def test_unknown_in_legacy_prose_submission_is_also_not_saved(tmp_path):
    store, pipe, attempt = fixture(tmp_path)
    value = payload()
    value['content'] = 'Prose does not make a false reference valid.'
    value['structured']['options'][0]['satisfies'] = PRIVATE_BAD_VALUE
    with pytest.raises(PipelineError, match='structured.options'):
        pipe.submit(RUN, 'direction', attempt.token, **value)
    assert store.latest_artifact(RUN, 'direction') is None


def test_source_change_keeps_original_stale_attempt_rejection(tmp_path):
    store, pipe, attempt = fixture(tmp_path)
    update = pipe.start_attempt(RUN, 'intake')
    pipe.submit(RUN, 'intake', update.token, content='', summary='New accurate source',
                structured={'facts': [{'label': 'Product', 'value': 'New grey lunch bag', 'source': 'fixture'}],
                            'constraints': ['Use the new product outline']})
    with pytest.raises(PipelineError) as caught:
        pipe.submit(RUN, 'direction', attempt.token, **payload())
    assert caught.value.rule == 'stale_attempt'
    assert store.latest_artifact(RUN, 'direction') is None


def test_rejected_attempt_still_cannot_submit_even_valid_references(tmp_path):
    store, pipe, attempt = fixture(tmp_path)
    pipe.reject(RUN, 'direction', attempt.token, 'Synthetic stop before submission')
    with pytest.raises(PipelineError) as caught:
        pipe.submit(RUN, 'direction', attempt.token, **payload())
    assert caught.value.rule == 'attempt'
    assert store.latest_artifact(RUN, 'direction') is None


def test_lock_is_held_for_exact_brief_reference_check(tmp_path, monkeypatch):
    store, pipe, attempt = fixture(tmp_path)
    original = ab.validate_direction_clause_references
    checked = []
    def verify(brief, structured):
        assert store.run_lock(RUN)._is_owned()
        assert ab.fingerprint(brief) == attempt.inputs['brief:snapshot']
        checked.append(True)
        return original(brief, structured)
    monkeypatch.setattr(ab, 'validate_direction_clause_references', verify)
    pipe.submit(RUN, 'direction', attempt.token, **payload())
    assert checked == [True]


def test_real_resume_handler_returns_safe_error_then_accepts_exact_correction(tmp_path, monkeypatch):
    store, pipe = seed(tmp_path, old_direction=False)
    wrong, corrected = payload(), payload()
    for row in wrong['structured']['options']:
        row['satisfies'] = f'goal.primary,content.format,{PRIVATE_BAD_VALUE}'
    responses = [wrong, corrected]
    def response(request):
        return reply(('submit_stage', responses.pop(0)))
    seen = mock_http(monkeypatch, response)
    settings = settings_for(tmp_path)
    client = DeepSeekClient('synthetic-only', reasoning_effort='low', progress_stream=False)
    with TestClient(build_app(settings, client_factory=lambda: client)) as http:
        command = http.get(f'/api/runs/{COMMAND_RUN}/timeline').json()['next_action']
        created = http.post(f'/api/runs/{COMMAND_RUN}/actions/resume-direction', json={
            'request_id': 'exact-safe-correction', 'expected_context_sha256': command['expected_context_sha256']})
        assert created.status_code == 200, created.text
        job = wait_job(http, created.json()['job_id'])
        assert job['status'] == 'succeeded', job['reason']
        assert len(seen) == 2, 'Only the original work budget may process a corrected result'
        outputs = [m['content'] for m in seen[1]['messages'] if m.get('role') == 'tool']
        assert len(outputs) == 1
        assert 'structured.options[0].satisfies' in outputs[0] and 'goal.primary' in outputs[0]
        assert PRIVATE_BAD_VALUE not in outputs[0] and 'content.format' not in outputs[0]
        saved = store.latest_artifact(COMMAND_RUN, 'direction')
        assert saved.version == 1 and len(saved.structured['options']) == 3
        assert all(r['satisfies'] == 'goal.primary' for r in saved.structured['options'])
        assert store.list_approvals(COMMAND_RUN) == []
        assert not any(e['kind'] in {'agent_images_observed', 'production_output'}
                       for e in store.read_events(COMMAND_RUN))
        assert [e['artifact'] for e in store.read_events(COMMAND_RUN)
                if e['kind'] == 'attempt_submitted' and e['stage'] == 'direction'] == ['direction-v1']


def test_real_resume_stop_before_submit_keeps_no_candidate(tmp_path, monkeypatch):
    store, pipe = seed(tmp_path, old_direction=False)
    started, release = Event(), Event()
    def response(request):
        started.set()
        assert release.wait(10), 'The test must release its own synthetic request'
        return reply(('submit_stage', payload()))
    seen = mock_http(monkeypatch, response)
    client = DeepSeekClient('synthetic-only', reasoning_effort='low', progress_stream=False)
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: client)) as http:
        command = http.get(f'/api/runs/{COMMAND_RUN}/timeline').json()['next_action']
        created = http.post(f'/api/runs/{COMMAND_RUN}/actions/resume-direction', json={
            'request_id': 'stopped-original-task', 'expected_context_sha256': command['expected_context_sha256']})
        assert created.status_code == 200
        assert started.wait(3)
        cancelled = http.post(f'/api/runs/{COMMAND_RUN}/jobs/{created.json()["job_id"]}/cancel')
        assert cancelled.json()['status'] == 'cancelling'
        release.set()
        job = wait_job(http, created.json()['job_id'])
        assert job['status'] == 'cancelled'
        assert len(seen) == 1 and store.latest_artifact(COMMAND_RUN, 'direction') is None
        assert store.list_approvals(COMMAND_RUN) == []
