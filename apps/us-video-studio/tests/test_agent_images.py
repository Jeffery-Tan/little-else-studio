"""Actual synthetic pixels, isolated jobs and fake model transport only."""
import base64
import json

import httpx
import pytest

from fakes import tool_turn, final_turn, ScriptedModel
from studio.agent_images import observe
from studio.agents.bailian_omni import OmniClient, MODEL
from studio.agents.client import Turn, Usage, run_agent_loop
from studio.creative_evidence import creative_evidence
from studio.evidence import evidence_fingerprint
from studio.ledger import Ledger
from studio.materials import accept_upload, resolve
from studio.pipeline import Pipeline
from studio.models import PipelineError
from test_image_analysis import scene
from test_bailian_omni import native, BASE, stream_response
from test_natural_reference import scripted, chat
from test_server import RUN


def result(count=1):
    return Turn(text=json.dumps({'images':[{'id':f'image-{i+1}', 'findings':[f'visible panel {i}'],
        'limitations':['Material and performance not verified']} for i in range(count)]}),
        model=MODEL,stop_reason='end_turn',usage=Usage(100,40))


def call(native, items):
    return observe(native[1],native[5],RUN,items,endpoint=BASE,emit=lambda _:None,
                   check_cancel=lambda:None,client_factory=lambda:native[2])


@pytest.mark.parametrize('scene', [12], indirect=True)
def test_seven_images_in_one_job_reach_direction_and_review_without_manual_analysis(native,monkeypatch):
    for i in range(6): accept_upload(native[1],RUN,f'other-{i}.png',native[4])
    names=['fixture.png']+[f'other-{i}.png' for i in range(6)]
    scripted(native,monkeypatch,[
        tool_turn('route','prepare_creative_context',dict(intent='new',format='video',deliverable='direction')),
        tool_turn('look','observe_images',{'images':[{'name':n,'purpose':'product'} for n in names]}),
        result(7),
        tool_turn('intake','write_stage',dict(stage='intake',content='Observed fixture panels',summary='Observed')),
        final_turn('建议下一步派导演'),
        tool_turn('director','dispatch',dict(stage='direction',brief='Create concrete directions')),
        tool_turn('save','submit_stage',dict(content='Two concrete fixture directions',summary='Compare')),
        tool_turn('review','request_review',dict(stage='direction')),
        tool_turn('verdict','finish_review',dict(verdict='pass',findings=[])),
        final_turn('两个具体方向，推荐第一种开场。'),
    ])
    job=chat(native,'用这些图片给商品做创意')
    assert job['status']=='succeeded',job
    assert len(native[2].calls)==10
    blocks=native[2].calls[2]['messages'][0]['content']
    assert sum(b['type']=='image' for b in blocks)==7
    assert all(base64.b64decode(b['source']['data'])==native[4] for b in blocks if b['type']=='image')
    assert '本轮约定交付具体创意方向' in native[2].calls[5]['messages'][-1]['content']
    for index in (6,8):
        assert 'visible panel 6' in json.dumps(native[2].calls[index],ensure_ascii=False)
    state=Pipeline(native[1]).stage_state(RUN,'direction')
    assert state.review.verdict=='pass' and state.approval is None
    assert native[1].read_chat(RUN)[-1]['text']=='两个具体方向，推荐第一种开场。'
    assert not native[1].list_approvals(RUN)
    assert chat(native,'用这些图片给商品做创意')['job_id']==job['job_id']
    assert len(native[2].calls)==10


def test_reuse_is_bound_to_bytes_and_purpose_and_not_human_confirmed(native):
    before=evidence_fingerprint(native[1],RUN)
    native[2].turn=result()
    items=[dict(name='fixture.png',purpose='product')]
    call(native,items)
    assert evidence_fingerprint(native[1],RUN)!=before
    assert call(native,items)['reused']==1 and len(native[2].calls)==1
    row=creative_evidence(native[1],RUN)['product_observations'][0]
    assert row['source_kind']=='agent_model_observation' and 'confirmed_at' not in row
    assert row['call_id'] and row['sha256']
    assert Ledger(native[1],native[5]).entries(RUN)[0].estimated_cny is not None
    resolve(native[1],RUN,'fixture.png').write_bytes(b'changed')
    assert not creative_evidence(native[1],RUN)['product_observations']


@pytest.mark.parametrize('images',[[dict(name='../fixture.png',purpose='product')],
    [dict(name='fixture.png',purpose='product')]*2, [dict(name='fixture.png',purpose='unknown')]])
def test_invalid_sources_do_not_call_provider(native,images):
    with pytest.raises(PipelineError): call(native,images)
    assert not native[2].calls


def test_invalid_result_does_not_enter_evidence_and_usage_survives(native):
    native[2].turn=result(2)
    with pytest.raises(PipelineError): call(native,[dict(name='fixture.png',purpose='product')])
    assert not creative_evidence(native[1],RUN)['product_observations']
    assert len(Ledger(native[1],native[5]).entries(RUN))==1


def test_readonly_scope_cannot_start_image_observation(native,monkeypatch):
    scripted(native,monkeypatch,[tool_turn('route','prepare_creative_context',dict(intent='status',format='none')),
        tool_turn('look','observe_images',{'images':[dict(name='fixture.png',purpose='product')]}),final_turn('仅查询')])
    chat(native,'看进度')
    assert len(native[2].calls)==3
    assert not creative_evidence(native[1],RUN)['product_observations']


def test_omni_wire_sends_multi_image_pixels(monkeypatch):
    seen=[]
    def serve(request):
        seen.append(json.loads(request.content));return httpx.Response(200,text=stream_response())
    real=httpx.Client
    monkeypatch.setattr('studio.agents.bailian_omni.httpx.Client',lambda **kw:real(transport=httpx.MockTransport(serve),**kw))
    block={'type':'image','source':{'type':'base64','media_type':'image/png','data':base64.b64encode(b'fixture').decode()}}
    OmniClient(BASE,'fake-key').complete(model=MODEL,tools=[],system='test',messages=[{'role':'user','content':[block,block]}])
    assert len(seen)==1
    assert [p['type'] for p in seen[0]['messages'][1]['content']]==['image_url','image_url']


def test_completion_reminder_is_bounded_and_does_not_replay_failed_tools():
    model=ScriptedModel([final_turn('premature'),final_turn('still incomplete')])
    run_agent_loop(model,model='fixture',system='',messages=[],tools=[],execute_tool=lambda *_:None,
                   completion_check=lambda:'Finish the selected goal')
    assert len(model.calls)==2
    model=ScriptedModel([tool_turn('failed','unavailable',{}),final_turn('blocked')])
    run_agent_loop(model,model='fixture',system='',messages=[],tools=[],execute_tool=lambda *_:None,
                   completion_check=lambda:'Finish the selected goal')
    assert len(model.calls)==2
