"""Project references use an isolated learning library; never call a model."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from types import SimpleNamespace

import pytest

from test_learning import env, _json
from studio.learning import LearningConflict
from studio.pipeline import Pipeline
from studio.references import (SelectIn, RemoveIn, change_reference, install_reference_routes,
                               reference_state, reference_fingerprint, reference_generation)


def choose(service, case_id='seed-case', purpose='message', request_id='request-one', **changes):
    case = next(c for c in service.catalog()['cases'] if c['id'] == case_id)
    payload = dict(request_id=request_id, case_id=case_id, case_sha256=case['case_sha256'],
                   purpose=purpose, expression='只借鉴前后动作对比',
                   expected_snapshot=reference_fingerprint(service.store, 'run-one'))
    payload.update(changes)
    return SelectIn(**payload)


def test_selection_is_event_only_idempotent_and_exact_hash(env):
    service, store, _ = env
    before = service.library_path.read_bytes()
    payload = choose(service)
    result = change_reference(service, 'run-one', payload)
    assert result['references'][0]['status'] == 'ready'
    assert change_reference(service, 'run-one', payload) == result
    assert len([e for e in store.read_events('run-one') if e['kind'] == 'reference_selected']) == 1
    assert service.library_path.read_bytes() == before
    with pytest.raises(ValueError, match='编号'):
        change_reference(service, 'run-one', payload.model_copy(update={'expression': 'different'}))


@pytest.mark.parametrize('case_id,purpose', [('hidden-case','message'), ('pending-case','message'), ('dependency-case','storyboard')])
def test_ineligible_reference_never_changes_project(env, case_id, purpose):
    service, store, _ = env
    with pytest.raises(ValueError):
        change_reference(service, 'run-one', choose(service, case_id, purpose))
    assert reference_fingerprint(store, 'run-one') is None


def test_explicit_reference_survives_empty_experiment_selection_and_scope_is_exact(env):
    service, store, _ = env
    change_reference(service, 'run-one', choose(service, purpose='storyboard'))
    writer = service.prepare_context('run-one','writer',case_ids=[],purpose='storyboard')
    assert writer['seed_case_ids'] == ['seed-case']
    assert '只借鉴前后动作对比' in writer['text']
    assert 'dependency-case' in writer['case_ids']
    assert '不能迁移他品规格' in writer['text']
    director = service.prepare_context('run-one','director',case_ids=['seed-case'],purpose='message')
    assert director['seed_case_ids'] == []
    assert director['selected_references'] == []
    dependency = service.prepare_context('run-one','director',case_ids=['dependency-case'],purpose='message')
    selected_dependency = next(c for c in dependency['library']['cases'] if c['id'] == 'seed-case')
    assert selected_dependency['eligible_for'] == ['storyboard']
    assert '不具备该用途资格' in dependency['text']
    reviewer = service.prepare_context('run-one','reviewer',case_ids=[],purpose='review')
    assert reviewer['selected_references'] == []
    assert '只借鉴前后动作对比' not in reviewer['text']
    paused = service.prepare_context('run-one','writer',intent='pause',case_ids=[])
    assert paused['selected_references'] == []


def test_feedback_reference_does_not_leak_into_reviewer(env):
    service, _, _ = env
    change_reference(service, 'run-one', choose(service, 'preference-case'))
    creator = service.prepare_context('run-one','director',case_ids=[])
    assert 'preference-case' in creator['seed_case_ids']
    reviewer = service.prepare_context('run-one','reviewer',tags=['proof'],purpose='review')
    assert 'secret-preference' not in _json(reviewer)
    assert '只借鉴前后动作对比' not in reviewer['text']


def test_changed_source_is_reported_and_never_silently_replaced(env):
    service, _, _ = env
    payload = choose(service)
    change_reference(service,'run-one',payload)
    _, doc = service._read()
    doc['cases'][0]['evidence'][0]['observation'] = 'CHANGED observation'
    service.library_path.write_text(_json(doc), encoding='utf-8')
    assert reference_state(service, 'run-one')['references'][0]['status'] == 'changed'
    with pytest.raises(LearningConflict, match='参考内容已变化'):
        service.prepare_context('run-one','director',case_ids=[])
    with pytest.raises(ValueError, match='参考内容已变化'):
        change_reference(service, 'run-one', payload.model_copy(update={'request_id':'request-new','expected_snapshot':reference_fingerprint(service.store,'run-one')}))


def test_selection_changes_invalidate_direction_but_not_intake_and_no_history_is_compatible(env):
    service, store, _ = env
    pipeline = Pipeline(store)
    attempt = pipeline.start_attempt('run-one','intake')
    pipeline.submit('run-one','intake',attempt.token,content='brief',summary='brief')
    attempt = pipeline.start_attempt('run-one','direction')
    pipeline.submit('run-one','direction',attempt.token,content='direction',summary='direction')
    assert not pipeline.stage_state('run-one','direction').stale
    assert 'references:snapshot' not in pipeline.require_prerequisites('run-one','direction')
    change_reference(service,'run-one',choose(service))
    assert pipeline.stage_state('run-one','direction').stale
    assert not pipeline.stage_state('run-one','intake').stale
    attempt = pipeline.start_attempt('run-one','direction')
    assert attempt.inputs['references:snapshot'] == reference_fingerprint(store,'run-one')
    pipeline.submit('run-one','direction',attempt.token,content='updated direction',summary='updated')
    assert not pipeline.stage_state('run-one','direction').stale
    removal = RemoveIn(request_id='remove-one',expected_snapshot=reference_fingerprint(store,'run-one'))
    result = change_reference(service,'run-one',removal,remove_case='seed-case')
    assert result['references'] == []
    assert pipeline.stage_state('run-one','direction').stale
    assert change_reference(service,'run-one',removal,remove_case='seed-case') == result


def test_source_library_change_invalidates_old_artifact_before_approval(env):
    service, store, _ = env
    pipeline = Pipeline(store)
    attempt = pipeline.start_attempt('run-one','intake')
    pipeline.submit('run-one','intake',attempt.token,content='brief',summary='brief')
    change_reference(service,'run-one',choose(service))
    attempt = pipeline.start_attempt('run-one','direction')
    artifact = pipeline.submit('run-one','direction',attempt.token,content='direction',summary='direction')
    assert not pipeline.stage_state('run-one','direction').stale
    snapshot = artifact.inputs['references:snapshot']
    _, doc = service._read()
    doc['cases'][0]['evidence'][0]['observation'] = 'new observation'
    service.library_path.write_text(_json(doc),encoding='utf-8')
    assert snapshot != reference_fingerprint(store,'run-one')
    assert pipeline.stage_state('run-one','direction').stale
    assert not pipeline.stage_state('run-one','direction').can_approve
    assert not pipeline.stage_state('run-one','intake').stale


def test_concurrent_choices_conflict_instead_of_overwriting(env):
    service, store, _ = env
    payloads = [choose(service, request_id='request-first'), choose(service,'dependency-case',request_id='request-second')]
    def save(payload):
        try: return change_reference(service,'run-one',payload)
        except ValueError as exc: return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save,payloads))
    assert sum(isinstance(r,dict) for r in results) == 1
    assert any('项目参考已被修改' in r for r in results if isinstance(r,str))


def test_http_busy_and_unfinished_attempts_prevent_selection(env):
    from fastapi import FastAPI, HTTPException
    from local_http import TestClient
    service, store, _ = env
    jobs = SimpleNamespace(store=store, pipeline=Pipeline(store), lock=threading.RLock(), active=object())
    app = FastAPI()
    def require_run(run_id):
        if run_id != 'run-one': raise HTTPException(404,'Project not found')
    install_reference_routes(app,jobs,require_run,service)
    with TestClient(app) as client:
        payload = choose(service).model_dump()
        assert client.get('/api/runs/missing/references').status_code == 404
        assert client.post('/api/runs/run-one/references',json=payload).status_code == 409
        jobs.active = None
        attempt = jobs.pipeline.start_attempt('run-one','intake')
        assert client.post('/api/runs/run-one/references',json=payload).status_code == 409
        jobs.pipeline.reject('run-one','intake',attempt.token,'fixture')
        assert client.post('/api/runs/run-one/references',json=payload).status_code == 200
        view = client.get('/api/runs/run-one/references').json()
        assert view['references'][0]['case_id'] == 'seed-case'
        removal = {'request_id':'remove-http','expected_snapshot':view['snapshot']}
        assert client.post('/api/runs/run-one/references/seed-case/remove',json=removal).json()['references'] == []


def test_reference_abba_return_to_prior_selection_stays_stale(env):
    """A→B→A on references: frozen generation beats hash equality."""
    service, store, _ = env
    pipeline = Pipeline(store)
    attempt = pipeline.start_attempt('run-one', 'intake')
    pipeline.submit('run-one', 'intake', attempt.token, content='brief', summary='brief')
    change_reference(service, 'run-one', choose(service))
    attempt = pipeline.start_attempt('run-one', 'direction')
    pipeline.submit('run-one', 'direction', attempt.token, content='direction', summary='direction')
    assert not pipeline.stage_state('run-one', 'direction').stale
    frozen = int(pipeline.stage_state('run-one', 'direction').latest.inputs['references:generation'])
    first_snapshot = pipeline.stage_state('run-one', 'direction').latest.inputs['references:snapshot']

    removal = RemoveIn(request_id='remove-abba', expected_snapshot=reference_fingerprint(store, 'run-one'))
    change_reference(service, 'run-one', removal, remove_case='seed-case')
    assert pipeline.stage_state('run-one', 'direction').stale

    change_reference(service, 'run-one', choose(service, request_id='reselect-abba'))
    state = pipeline.stage_state('run-one', 'direction')
    assert state.latest.inputs['references:snapshot'] == first_snapshot
    assert state.stale
    assert reference_generation(store, 'run-one') > frozen
    assert not state.can_approve


def _approved_direction(service, store):
    """intake + a selected reference + an approved direction, the real shape."""
    pipeline = Pipeline(store)
    attempt = pipeline.start_attempt('run-one', 'intake')
    pipeline.submit('run-one', 'intake', attempt.token, content='brief', summary='brief')
    change_reference(service, 'run-one', choose(service))
    attempt = pipeline.start_attempt('run-one', 'direction')
    pipeline.submit('run-one', 'direction', attempt.token, content='direction', summary='direction')
    pipeline.record_review('run-one', 'direction', verdict='pass', findings=[])
    pipeline.record_approval('run-one', 'direction',
                             nonce=pipeline.issue_nonce('run-one', 'direction'), decision='approved')
    assert pipeline.stage_state('run-one', 'direction').status == 'approved'
    return pipeline


def _edit_case(service, note):
    document = json.loads(service.library_path.read_bytes().decode('utf-8-sig'))
    document['cases'][0]['strategy']['proof'] = note
    service.library_path.write_text(_json(document), encoding='utf-8')


def _source_events(store):
    return [e for e in store.read_events('run-one') if e['kind'] == 'reference_source_changed']


def test_a_source_case_edit_outside_the_run_is_recorded_by_the_read_path(env):
    """Red line 11: A→B→A must stay stale even for an edit no run event covers.

    Changing a case from another run — or straight on disk — writes nothing in
    *this* run's journal, so only the read path that notices the change can keep
    the old approval from coming back when the bytes do.
    """
    service, store, _ = env
    pipeline = _approved_direction(service, store)
    original = service.library_path.read_bytes()

    _edit_case(service, 'edited outside this run')
    # The staleness check itself is the read path that must record it.
    assert pipeline.stage_state('run-one', 'direction').stale
    recorded = _source_events(store)
    assert [e['case_id'] for e in recorded] == ['seed-case']
    assert recorded[0]['selected_sha256'] != recorded[0]['current_sha256']

    # Repeated reads of the same edit append nothing more.
    reference_state(service, 'run-one')
    pipeline.stage_state('run-one', 'direction')
    assert len(_source_events(store)) == len(recorded)

    # A→B→A on the source case: the bytes come back, the approval does not.
    service.library_path.write_bytes(original)
    state = pipeline.stage_state('run-one', 'direction')
    assert state.stale and state.status == 'stale'
    assert state.approval is not None, 'the old record is kept, not erased'
    assert state.can_approve is False

    # Landing on the same bytes again is the same change, not a second one.
    _edit_case(service, 'edited outside this run')
    assert pipeline.stage_state('run-one', 'direction').stale
    assert len(_source_events(store)) == len(recorded)

    # The references panel is a read path too: an edit nobody has asked the
    # pipeline about is recorded when he opens it.
    service.library_path.write_bytes(original)
    _edit_case(service, 'a second, different edit')
    reference_state(service, 'run-one')
    assert len(_source_events(store)) == len(recorded) + 1


def test_a_source_case_edit_never_invalidates_a_project_that_did_not_select_it(env):
    """Only this run's own selections are recorded; the library is shared."""
    service, store, _ = env
    store.create_run('run-two', 'Other', 'brief')
    pipeline = Pipeline(store)
    attempt = pipeline.start_attempt('run-two', 'intake')
    pipeline.submit('run-two', 'intake', attempt.token, content='brief', summary='brief')
    attempt = pipeline.start_attempt('run-two', 'direction')
    pipeline.submit('run-two', 'direction', attempt.token, content='direction', summary='direction')

    _edit_case(service, 'edited outside this run')
    assert not pipeline.stage_state('run-two', 'direction').stale
    assert [e for e in store.read_events('run-two') if e['kind'] == 'reference_source_changed'] == []
