"""Offline object revisions preserve old work, approvals and money boundaries."""
import json
import threading
import time

import pytest
from local_http import TestClient

from studio.agents.client import Turn, Usage, ModelError
from studio.ledger import Ledger
from studio.pipeline import Pipeline
from studio.server import build_app
from studio.store import Store
from test_server import RUN, settings_for


def seed(store):
    store.create_run(RUN, 'Offline revision fixture', 'Synthetic creative brief')
    pipe = Pipeline(store)
    def write(stage, body, structure):
        attempt = pipe.start_attempt(RUN, stage)
        return pipe.submit(RUN, stage, attempt.token, content=body, summary=stage,
                           structured=structure, evidence=['original source'], limitations=['unknown size'])
    write('intake', 'brief', {})
    base = write('direction', 'A: original hook. B: keep this direction. Extra prose stays.', {
        'options': [{'key':'A','claim':'Bring lunch','hook':'Original hook'},
                    {'key':'B','claim':'Weekend picnic','hook':'Keep hook'}],
        'recommended':'A','recommended_because':'Original reason','prerequisites':['Need product image']})
    pipe.record_review(RUN,'direction',verdict='pass',findings=[])
    pipe.record_approval(RUN,'direction',nonce=pipe.issue_nonce(RUN,'direction'),decision='approved',selected_option_id='A')
    write('material_plan','Original materials',{'items':[{'shot':'Opening','status':'不确定'}]})
    write('writing','Shot one and shot two',{'segments':[{'tc':'0:00–0:03','picture':'Desk','line':'Line one'},
          {'tc':'0:03–0:06','picture':'Bag','line':'Line two'}],'total_duration':'6秒'})
    return pipe, base


class FixtureModel:
    def __init__(self):
        self.calls=[]; self.entered=threading.Event(); self.release=threading.Event(); self.release.set()
        self.error=None
        self.result={'content':'A: new hook. B: keep this direction. Extra prose stays.', 'summary':'Changed opening only'}
        self.usage=Usage(100,60)
    def complete(self, **kwargs):
        self.calls.append(kwargs); self.entered.set(); assert self.release.wait(5)
        if self.error: raise self.error
        return Turn(text=json.dumps(self.result),stop_reason='end_turn',model='fixture-model',usage=self.usage)


@pytest.fixture
def scene(tmp_path, monkeypatch):
    store=Store(tmp_path); pipe,base=seed(store); model=FixtureModel(); settings=settings_for(tmp_path)
    monkeypatch.setattr('studio.learning.LearningService.prepare_context',lambda *a,**k: {
        'text':'SYNTHETIC_METHOD_CONTEXT','snapshot_sha256':'f'*64,'evidence_ref':'fixture-method-ref'})
    with TestClient(build_app(settings,client_factory=lambda:model)) as http:
        yield http,store,pipe,base,model,settings
    model.release.set()


def draft(scene, stage='direction', **over):
    http,store,_,_,_,_=scene
    base=store.latest_artifact(RUN,stage)
    payload={'request_id':'save-1','expected_version':base.version,'expected_sha256':base.sha256,
             'edits':[{'row':0,'field':'hook' if stage=='direction' else 'line','value':'New hook'}],**over}
    return http.post(f'/api/runs/{RUN}/edits/{stage}',json=payload)


def generate(scene, d, **over):
    return scene[0].post(f"/api/runs/{RUN}/edits/{d['stage']}/{d['draft_id']}/generate",json={
        'request_id':'generate-1','expected_draft_sha256':d['draft_sha256'],'expected_revision':0,'consent':True,**over})


def wait(http,job):
    for _ in range(300):
        result=http.get(f"/api/runs/{RUN}/jobs/{job['job_id']}").json()
        if result['status'] not in ('queued','running','cancelling'):return result
        time.sleep(.01)
    pytest.fail('fixture job did not finish')


def test_save_is_free_immutable_version_bound_and_idempotent(scene):
    http,store,pipe,base,model,settings=scene
    before=store.read_artifact_text(RUN,'direction',1)
    first=draft(scene); assert first.status_code==200,first.text
    assert draft(scene).json()['draft_id']==first.json()['draft_id']
    assert draft(scene,edits=[{'row':0,'field':'hook','value':'Another'}]).status_code==409
    assert store.latest_artifact(RUN,'direction').version==1
    assert store.read_artifact_text(RUN,'direction',1)==before
    assert pipe.stage_state(RUN,'direction').approval is not None
    assert not pipe.stage_state(RUN,'writing').stale
    assert not model.calls and not Ledger(store,settings).entries()
    reopened=http.get(f'/api/runs/{RUN}/edits/direction').json()
    assert reopened['drafts'][0]['changes']==[{'row':0,'field':'hook','before':'Original hook','after':'New hook'}]


def test_generate_preserves_other_objects_resets_approval_invalidates_downstream(scene):
    http,store,pipe,base,model,settings=scene
    d=draft(scene).json(); response=generate(scene,d); assert response.status_code==200,response.text
    job=wait(http,response.json()); assert job['status']=='succeeded',job
    latest=store.latest_artifact(RUN,'direction')
    assert latest.version==2 and store.verify_artifact(RUN,'direction',2)
    assert latest.structured['options'][0]['hook']=='New hook'
    assert latest.structured['options'][1]==base.structured['options'][1]
    assert latest.structured['prerequisites']==base.structured['prerequisites']
    assert latest.limitations==base.limitations and latest.evidence==[*base.evidence,'fixture-method-ref']
    assert pipe.stage_state(RUN,'direction').review is None and pipe.stage_state(RUN,'direction').approval is None
    assert pipe.stage_state(RUN,'material_plan').stale and pipe.stage_state(RUN,'writing').stale
    assert len(store.list_approvals(RUN,'direction'))==1
    assert 'SYNTHETIC_METHOD_CONTEXT' in model.calls[0]['system'] and model.calls[0]['tools']==[]
    assert 'Extra prose stays' in model.calls[0]['messages'][0]['content']
    assert generate(scene,d).json()['job_id']==job['job_id']
    assert generate(scene,d,request_id='different').status_code==409
    assert len(model.calls)==1 and len(Ledger(store,settings).entries())==1
    compare=http.get(f'/api/runs/{RUN}/artifacts/direction/compare?from_version=1&to_version=2')
    assert compare.status_code==200,compare.text
    fields=compare.json()['changes']
    assert next(c for c in fields if c['path']==['options',0,'hook'])['after']=='New hook'
    assert not any(c['path'][:2]==['options',1] for c in fields)
    assert '-A: original' in compare.json()['prose_diff']


def test_writing_revision_preserves_second_shot_and_timing(scene):
    http,store,pipe,_,model,_=scene
    old=store.latest_artifact(RUN,'writing')
    d=draft(scene,'writing').json()
    assert wait(http,generate(scene,d).json())['status']=='succeeded'
    new=store.latest_artifact(RUN,'writing')
    assert new.structured['segments'][0]['line']=='New hook'
    assert new.structured['segments'][1]==old.structured['segments'][1]
    assert new.structured['total_duration']==old.structured['total_duration']
    assert not pipe.stage_state(RUN,'direction').stale


def test_timing_edit_marks_total_for_review_and_preserves_other_shots(scene):
    http,store,_,_,model,_=scene
    old=store.latest_artifact(RUN,'writing')
    d=draft(scene,'writing',edits=[{'row':0,'field':'tc','value':'  0:00–0:04  '}]).json()
    assert d['edits'][0]['value']=='0:00–0:04'
    assert wait(http,generate(scene,d).json())['status']=='succeeded'
    new=store.latest_artifact(RUN,'writing')
    assert new.structured['total_duration'].startswith('待复核')
    assert new.structured['segments'][1]==old.structured['segments'][1]
    assert '待复核' in model.calls[0]['messages'][0]['content']
    assert '节奏' in new.structured['assumptions'][-1]


def test_cannot_erase_an_entire_shot_or_save_whitespace_only_change(scene):
    assert draft(scene,edits=[{'row':0,'field':'hook','value':' Original hook '}]).status_code==409
    old=scene[1].latest_artifact(RUN,'writing')
    edits=[{'row':0,'field':field,'value':''} for field in old.structured['segments'][0]]
    assert draft(scene,'writing',edits=edits).status_code==409


@pytest.mark.parametrize('edits',[[{'row':0,'field':'key','value':'Z'}],
    [{'row':99,'field':'hook','value':'x'}],[{'row':True,'field':'hook','value':'x'}],
    [{'row':0,'field':'hook','value':'x'}]*2,[{'row':0,'field':'hook','value':'Original hook'}],
    [{'row':0,'field':'hook','value':'x'*4001}]])
def test_bad_cells_cannot_write(scene,edits):
    assert draft(scene,edits=edits).status_code in (409,422)
    assert not any(e['kind']=='revision_draft' for e in scene[1].read_events(RUN))


def test_conflicts_refuse_stale_edits_and_tampered_comparison(scene):
    http,store,pipe,base,model,_=scene
    d=draft(scene).json()
    attempt=pipe.start_attempt(RUN,'intake')
    pipe.submit(RUN,'intake',attempt.token,content='Changed input',summary='New input')
    workspace=http.get(f'/api/runs/{RUN}/edits/direction').json()
    assert not workspace['editable'] and '[edit]' not in workspace['reason']
    assert generate(scene,d).status_code==409
    assert draft(scene,request_id='save-2').status_code==409
    (store.run_dir(RUN)/'artifacts'/'direction-v1.md').write_text('tampered',encoding='utf-8')
    assert http.get(f'/api/runs/{RUN}/artifacts/direction/compare?from_version=1&to_version=1').status_code==409
    assert not model.calls


@pytest.mark.parametrize('mode',['declined','bad_hash','changed_config','unknown_usage','invalid_output','transport'])
def test_generation_boundaries_preserve_old_versions_and_cost_records(scene,mode):
    http,store,pipe,_,model,settings=scene; d=draft(scene).json();over={}
    if mode=='declined':over['consent']=False
    if mode=='bad_hash':over['expected_draft_sha256']='0'*64
    if mode=='changed_config':over['expected_revision']=5
    if mode=='unknown_usage':model.usage=Usage(None,None)
    if mode=='invalid_output':model.result={'content':''}
    if mode=='transport':model.error=ModelError(500,'synthetic failure')
    r=generate(scene,d,**over)
    if mode in ('declined','bad_hash','changed_config'):
        assert r.status_code==409;assert not model.calls;return
    job=wait(http,r.json())
    assert len(model.calls)==1 and len(Ledger(store,settings).entries())==1
    assert pipe.stage_state(RUN,'direction').open_attempt is None
    if mode!='unknown_usage':assert store.latest_artifact(RUN,'direction').version==1 and job['status']=='failed'
    if mode in ('unknown_usage','transport'):
        assert http.get(f'/api/runs/{RUN}/recovery').json()['can_continue']
        assert Ledger(store,settings).totals().unknown_calls == 1


def test_inflight_revision_blocks_other_writers_and_cancel_keeps_returned_version(scene):
    http,store,pipe,base,model,_=scene; d=draft(scene).json();model.release.clear()
    job=generate(scene,d).json();assert model.entered.wait(2)
    assert draft(scene,request_id='other').status_code==409
    assert generate(scene,d).json()['job_id']==job['job_id']
    assert http.post(f"/api/runs/{RUN}/jobs/{job['job_id']}/cancel",json={}).status_code==200
    model.release.set();assert wait(http,job)['status']=='cancelled'
    assert store.latest_artifact(RUN,'direction').version==2
    assert pipe.stage_state(RUN,'direction').approval is None
    assert len(model.calls)==1
