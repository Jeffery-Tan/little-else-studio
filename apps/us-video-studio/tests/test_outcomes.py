"""Publication outcomes are explicit, local user reports, never observed analytics."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from studio.outcomes import install_outcome_routes
from studio.store import Store
from test_revisions import seed
from test_server import RUN


@pytest.fixture
def scene(tmp_path):
    store = Store(tmp_path)
    pipeline, _ = seed(store)
    app = FastAPI()
    jobs = SimpleNamespace(store=store, pipeline=pipeline, lock=threading.RLock(), active=None)
    def require_run(run_id):
        if not (store.run_dir(run_id) / 'run.json').exists():
            raise HTTPException(404, 'missing run')
    install_outcome_routes(app, jobs, require_run)
    with TestClient(app) as http:
        yield http, store, jobs


def publication(scene, **changes):
    artifact = scene[1].latest_artifact(RUN, 'writing')
    return scene[0].post(f'/api/runs/{RUN}/outcomes', json={
        'request_id': 'publication-one', 'title': 'Commute story',
        'url': 'https://www.tiktok.com/@fixture/video/123',
        'published_at': '2026-01-01T08:00:00+08:00', 'version': artifact.version,
        'artifact_sha256': artifact.sha256, 'differences': 'Final cut has not been checked against this draft.',
        **changes})


def snapshot(scene, **changes):
    return scene[0].post(f'/api/runs/{RUN}/outcomes/publication-one/snapshots', json={
        'request_id': 'snapshot-one', 'expected_latest_id': None,
        'observed_at': '2026-01-02T12:00:00+08:00',
        'window_start': '2026-01-01T08:00:00+08:00',
        'window_end': '2026-01-02T08:00:00+08:00', 'traffic': 'unknown',
        'views': 0, 'source': 'Manually transcribed platform report',
        'notes': 'Attribution and final video quality have not been checked.', **changes})


def test_records_preserve_unknown_zero_and_do_not_change_artifacts_approvals_or_learning(scene):
    http, store, _ = scene
    before = {str(p.relative_to(store.root)): p.read_bytes() for p in store.root.rglob('*')
              if p.is_file() and p.name != 'events.jsonl'}
    p = publication(scene)
    assert p.status_code == 200, p.text
    assert p.json()['verification'] == 'user_reported'
    s = snapshot(scene)
    assert s.status_code == 200, s.text
    assert s.json()['views'] == 0 and s.json()['orders'] is None and s.json()['revenue_usd'] is None
    assert s.json()['verification'] == 'user_reported'
    state = http.get(f'/api/runs/{RUN}/outcomes').json()
    assert state['artifacts'][0]['integrity_ok'] is True
    assert state['publications'][0]['latest'] == s.json()
    assert state['publications'][0]['source_integrity_ok'] is True
    assert 'trend' not in state and 'ranking' not in state
    after = {str(p.relative_to(store.root)): p.read_bytes() for p in store.root.rglob('*')
             if p.is_file() and p.name != 'events.jsonl'}
    assert before == after
    new_events = [e for e in store.read_events(RUN) if e['kind'] in ('publication_recorded', 'outcome_snapshot_recorded')]
    assert len(new_events) == 2 and all(e['actor'] == 'user' for e in new_events)


def test_idempotence_and_append_only_corrections(scene):
    p = publication(scene).json()
    s = snapshot(scene).json()
    corrected = snapshot(scene, request_id='snapshot-two', expected_latest_id=s['snapshot_id'], views=31)
    assert corrected.status_code == 200
    assert publication(scene).json() == p
    assert snapshot(scene).json() == s
    assert publication(scene, title='different').status_code == 409
    assert snapshot(scene, views=15).status_code == 409
    assert snapshot(scene, request_id='snapshot-three', views=20).status_code == 409
    history = scene[0].get(f'/api/runs/{RUN}/outcomes').json()['publications'][0]['snapshots']
    assert len(history) == 2 and history[0]['views'] == 0 and history[1]['views'] == 31


def test_concurrent_snapshots_cannot_overwrite_each_other(scene):
    publication(scene)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda request_id: snapshot(scene, request_id=request_id), ['one', 'two']))
    assert sorted(r.status_code for r in responses) == [200, 409]
    state = scene[0].get(f'/api/runs/{RUN}/outcomes').json()
    assert len(state['publications'][0]['snapshots']) == 1


def test_duplicate_url_rejected_and_distinct_work_may_share_draft(scene):
    assert publication(scene).status_code == 200
    assert publication(scene, request_id='other').status_code == 409
    assert publication(scene, request_id='other', url='https://www.tiktok.com/@fixture/video/124').status_code == 200


def test_historical_writing_version_can_be_linked_without_adopting_newer_draft(scene):
    store = scene[1]
    original = store.read_artifact(RUN, 'writing', 1)
    store.write_artifact(RUN, 'writing', 2, 'A different later draft', summary='later',
                         author_role='writer', attempt='isolated-fixture')
    result = publication(scene, version=1, artifact_sha256=original.sha256)
    assert result.status_code == 200 and result.json()['version'] == 1
    assert snapshot(scene).status_code == 200
    state = scene[0].get(f'/api/runs/{RUN}/outcomes').json()
    assert [a['version'] for a in state['artifacts']] == [1, 2]
    assert state['publications'][0]['artifact_sha256'] == original.sha256


@pytest.mark.parametrize('change', [
    {'views': None}, {'views': -1}, {'views': True}, {'views': 1.5}, {'views': '2'},
    {'revenue_usd': -1}, {'spend_usd': '2.3'}, {'spend_usd': True},
    {'source': ' '}, {'notes': ''}, {'traffic': 'viral'},
    {'observed_at': '2026-01-02T12:00:00'}, {'observed_at': '2999-01-01T00:00:00Z'},
    {'window_start': '2026-01-03T00:00:00Z'}, {'window_end': '2026-01-02T13:00:00+08:00'},
])
def test_invalid_snapshot_fields_cannot_write_events(scene, change):
    publication(scene)
    count = len(scene[1].read_events(RUN))
    assert snapshot(scene, **change).status_code == 422
    assert len(scene[1].read_events(RUN)) == count


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity'])
def test_nonfinite_amount_rejected(scene, value):
    publication(scene)
    body = {'request_id':'bad', 'expected_latest_id':None, 'observed_at':'2026-01-02T00:00:00Z',
            'window_start':'2026-01-01T00:00:00Z', 'window_end':'2026-01-02T00:00:00Z',
            'traffic':'unknown', 'source':'fixture', 'notes':'fixture', 'spend_usd':'MAGIC'}
    response = scene[0].post(f'/api/runs/{RUN}/outcomes/publication-one/snapshots',
        content=json.dumps(body).replace('"MAGIC"', value), headers={'Content-Type':'application/json'})
    assert response.status_code == 422


@pytest.mark.parametrize('url', [
    'http://www.tiktok.com/video/1', 'https://user:password@www.tiktok.com/v',
    'https://127.0.0.1/v', 'https://[::1]/v', 'https://localhost/v',
    'https://machine.local/v', 'https://10.0.0.1/v', 'https://example.com:4760/v',
    'https://example.com/ bad', 'https://example.com\\@127.0.0.1/v',
])
def test_invalid_publication_url_rejected_without_any_fetch(scene, url):
    assert publication(scene, url=url).status_code == 422


@pytest.mark.parametrize('changes', [{'published_at':'2026-01-01'}, {'published_at':'2999-01-01T00:00:00Z'},
                                     {'title':' '}, {'differences':' '}, {'version':True}])
def test_invalid_publication_fields(scene, changes):
    assert publication(scene, **changes).status_code == 422


def test_window_cannot_predate_publication(scene):
    publication(scene)
    assert snapshot(scene, window_start='2025-12-31T00:00:00Z').status_code == 409


def test_wrong_hash_and_tampered_source_are_rejected_and_visible(scene):
    http, store, _ = scene
    assert publication(scene, artifact_sha256='0'*64).status_code == 409
    publication(scene)
    path = store.run_dir(RUN) / 'artifacts' / 'writing-v1.md'
    path.write_text('tampered', encoding='utf-8')
    assert snapshot(scene).status_code == 409
    state = http.get(f'/api/runs/{RUN}/outcomes').json()
    assert state['artifacts'][0]['integrity_ok'] is False
    assert state['publications'][0]['source_integrity_ok'] is False


def test_tampered_record_rejected(scene):
    publication(scene)
    path = scene[1].run_dir(RUN) / 'events.jsonl'
    path.write_text(path.read_text('utf-8').replace('Commute story', 'Tampered story'), encoding='utf-8')
    assert scene[0].get(f'/api/runs/{RUN}/outcomes').status_code == 409
    assert snapshot(scene).status_code == 409


def test_busy_service_and_non_us_rejected(scene):
    scene[2].active = {'job_id':'busy'}
    assert publication(scene).status_code == 409
    scene[2].active = None
    path = scene[1].run_dir(RUN) / 'run.json'
    run = json.loads(path.read_text('utf-8')); run['market'] = 'SEA'
    path.write_text(json.dumps(run), encoding='utf-8')
    assert publication(scene).status_code == 409
    assert scene[0].get(f'/api/runs/{RUN}/outcomes').status_code == 409
