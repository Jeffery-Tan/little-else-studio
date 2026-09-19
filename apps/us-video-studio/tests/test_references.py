"""Project references use an isolated learning library; never call a model."""
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from test_learning import env, _json
from studio.learning import LearningConflict
from studio.pipeline import Pipeline
from studio.references import (SelectIn, RemoveIn, change_reference, install_reference_routes,
                               reference_state, reference_fingerprint)


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
    from fastapi.testclient import TestClient
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
