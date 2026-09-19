"""Reusable input cards use isolated run events and never call a model."""
import json

import pytest
from fastapi.testclient import TestClient

from studio.server import build_app
from studio.store import Store
from studio.pipeline import Pipeline
from test_server import settings_for


@pytest.fixture
def scene(tmp_path):
    store=Store(tmp_path)
    store.create_run('source-run','Fixture lunch bag','User supplied description, capacity unknown')
    with TestClient(build_app(settings_for(tmp_path),client_factory=lambda:pytest.fail('No model call allowed'))) as http:
        yield http,store,Pipeline(store)


def save(scene, **changes):
    http,_,_=scene
    source=http.get('/api/runs/source-run/product-cards').json()['source']
    return http.post('/api/runs/source-run/product-cards',json={
        'request_id':'card-one','source':source,'name':'Lunch bag','facts':'User supplied: lunch bag',
        'evidence':'Project brief','limitations':'Capacity unknown',**changes})


def creation(card, **changes):
    return {'run_id':'new-run','title':'A new story','brief':'Creative request only',
            'product_card':{k:card[k] for k in ('source_run_id','card_id','sha256')},**changes}


def test_save_freezes_source_without_changing_original_and_reuses_in_new_brief(scene):
    http,store,pipe=scene
    before=store.read_run('source-run').to_dict()
    card=save(scene).json()
    assert save(scene).json()==card
    assert store.read_run('source-run').to_dict()==before
    assert len(http.get('/api/product-cards').json()['cards'])==1
    assert '来源为原始需求' in card['source_limits'][0]
    response=http.post('/api/runs',json=creation(card))
    assert response.status_code==200,response.text
    assert 'Creative request only' in response.json()['brief']
    assert 'Capacity unknown' in response.json()['brief']
    assert '未自动重新核验' in response.json()['brief']
    assert not store.list_artifacts('new-run') and not store.list_approvals('new-run')
    assert http.post('/api/runs',json=creation(card)).json()==response.json()
    assert len(store.read_events('new-run'))==1
    assert store.read_events('new-run')[0]['binding']['sha256']==card['sha256']
    assert not list(store.root.glob('**/ledger.jsonl'))


def test_card_save_and_project_creation_reject_changed_identity(scene):
    http,store,_=scene
    card=save(scene).json()
    assert save(scene,facts='Different facts').status_code==409
    payload=creation(card);payload['product_card']['sha256']='0'*64
    assert http.post('/api/runs',json=payload).status_code==400
    assert not (store.run_dir('new-run')/'run.json').exists()
    assert http.post('/api/runs',json=creation(card)).status_code==200
    assert http.post('/api/runs',json=creation(card,title='Different title')).status_code==400


def test_intake_source_limits_survive_user_notes_and_old_cards_are_snapshots(scene):
    http,store,pipe=scene
    attempt=pipe.start_attempt('source-run','intake')
    a=pipe.submit('source-run','intake',attempt.token,content='Detailed source',summary='facts',
        structured={'facts':[{'label':'Color','value':'blue','source':'user'}],
                    'constraints':['No performance claims'],'open_questions':[{'question':'Size?'}]},
        limitations=['Capacity unverified'],evidence=['Photo record'])
    view=http.get('/api/runs/source-run/product-cards').json()
    assert 'Color：blue' in view['facts'] and view['source']['sha256']==a.sha256
    card=save(scene,limitations='Additional unknown').json()
    assert set(card['source_limits'])=={'No performance claims','Size?','Capacity unverified'}
    attempt=pipe.start_attempt('source-run','intake')
    pipe.submit('source-run','intake',attempt.token,content='New facts',summary='new')
    stale=save(scene,request_id='other',source=view['source'])
    assert stale.status_code==409
    r=http.post('/api/runs',json=creation(card))
    assert r.status_code==200 and 'No performance claims' in r.json()['brief']
    assert '版本 1' in r.json()['brief']


def test_tampered_card_or_source_cannot_be_reused(scene):
    http,store,_=scene
    card=save(scene).json()
    run_path=store.run_dir('source-run')/'run.json'
    run=json.loads(run_path.read_text('utf-8'));run['brief']='tampered'
    run_path.write_text(json.dumps(run),encoding='utf-8')
    assert http.post('/api/runs',json=creation(card)).status_code==400
    events=store.run_dir('source-run')/'events.jsonl'
    events.write_text(events.read_text('utf-8').replace('Lunch bag','Changed bag'),encoding='utf-8')
    assert http.get('/api/product-cards').status_code==409


@pytest.mark.parametrize('values',[{'name':' '},{'facts':' '},{'evidence':''},{'limitations':' '},{'facts':'x'*6001}])
def test_incomplete_or_excessive_card_is_rejected(scene,values):
    assert save(scene,**values).status_code in (409,422)
    assert not scene[1].read_events('source-run')


def test_oversize_composed_brief_does_not_create_partial_project(scene):
    card=save(scene).json()
    assert scene[0].post('/api/runs',json=creation(card,brief='a'*20000)).status_code==400
    assert not scene[1].run_dir('new-run').exists()
