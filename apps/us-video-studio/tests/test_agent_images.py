"""Actual synthetic pixels, isolated jobs and fake model transport only."""
import base64
import json
import hashlib
import re

import httpx
import pytest

from fakes import tool_turn, final_turn, ScriptedModel
from studio.agent_images import observe
from studio.agents.bailian_omni import OmniClient, MODEL
from studio.agents.client import Turn, Usage, ModelError, run_agent_loop
from studio.creative_evidence import creative_evidence
from studio.evidence import evidence_fingerprint
from studio.ledger import Ledger
from studio.materials import accept_upload, resolve
from studio.agent_images import saved_observations
from test_observation_image_input import noisy_png
from studio.pipeline import Pipeline
from studio.models import PipelineError
from studio.tools.registry import REGISTRY
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


def image_analysis_context(text):
    # Explicit fixture task declaration; observing pixels does not request a
    # direction or select a photo/video production format.
    return dict(intent='new', format='none', deliverable='analysis', task_interpretation={
        'task': dict(quote=text, mode='analyze', deliverable='analysis', format='none',
                     allowed_stages=[], pause_after=False),
        'constraints': [],
    })


def tool_result_for(model, call_id):
    for request in model.calls:
        for message in request['messages']:
            content = message.get('content')
            if isinstance(content, list):
                for block in content:
                    if block.get('type') == 'tool_result' and block.get('tool_use_id') == call_id:
                        return block
    raise AssertionError(f'No actual tool result for {call_id}')


@pytest.mark.parametrize('scene', [12], indirect=True)
def test_seven_images_in_one_job_reach_direction_without_manual_analysis(native,monkeypatch):
    for i in range(6): accept_upload(native[1],RUN,f'other-{i}.png',native[4])
    names=['fixture.png']+[f'other-{i}.png' for i in range(6)]
    scripted(native,monkeypatch,[
        tool_turn('route','prepare_creative_context',dict(intent='new',format='video',deliverable='direction')),
        tool_turn('look','observe_images',{'images':[{'name':n,'purpose':'product'} for n in names]}),
        result(7),
        tool_turn('intake','write_stage',dict(stage='intake',content='Observed fixture panels',summary='Observed')),
        final_turn('建议下一步派导演'),
        tool_turn('save','submit_stage',dict(content='Two concrete fixture directions',summary='Compare')),
    ])
    job=chat(native,'用这些图片给商品做创意')
    assert job['status']=='succeeded',job
    assert len(native[2].calls)==6
    blocks=native[2].calls[2]['messages'][0]['content']
    assert sum(b['type']=='image' for b in blocks)==7
    assert all(base64.b64decode(b['source']['data'])==native[4] for b in blocks if b['type']=='image')
    # Once intake and observation are ready, code dispatches the director
    # without another producer reminder or tool-selection round.
    assert 'visible panel 6' in json.dumps(native[2].calls[5],ensure_ascii=False)
    state=Pipeline(native[1]).stage_state(RUN,'direction')
    assert state.review is None and state.can_approve and state.approval is None
    assert '方向已保存' in native[1].read_chat(RUN)[-1]['text']
    assert not native[1].list_approvals(RUN)
    assert chat(native,'用这些图片给商品做创意')['job_id']==job['job_id']
    assert len(native[2].calls)==6


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
    # A failed internal tool no longer skips the deliverable reminder: the model
    # gets one more chance to continue team work before the turn ends incomplete.
    model=ScriptedModel([tool_turn('failed','read_project_state',{}),final_turn('blocked'),
                         final_turn('give up')])
    executed=[]
    def fail_internal_tool(name, args):
        executed.append((name, args))
        raise ValueError('Synthetic internal read failure')
    loop=run_agent_loop(model,model='fixture',system='',messages=[],
                   tools=[REGISTRY['read_project_state'].to_api()],execute_tool=fail_internal_tool,
                   completion_check=lambda:'Finish the selected goal')
    assert len(model.calls)==3
    assert 'Finish the selected goal' in model.requests_text()
    assert executed==[('read_project_state',{})]
    assert loop.stopped_because=='incomplete'
    assert len(loop.tool_log)==1 and loop.tool_log[0]['ok'] is False


def answering_observer(native, monkeypatch, *, fail_batch=None):
    model = native[2]
    complete = model.__class__.complete.__get__(model)

    def answer(**kwargs):
        ids = [re.match(r'图片 (image-\d+)', block['text']).group(1)
               for block in kwargs['messages'][0]['content'] if block['type'] == 'text']
        text = json.dumps({'images': [{'id': ident, 'findings': ['Visible synthetic panel'],
                                     'limitations': ['Test-only pixels']} for ident in ids]})
        if fail_batch == len(model.calls) + 1:
            text = 'invalid paid response'
        model.turn = Turn(text=text, model=MODEL, stop_reason='end_turn', usage=Usage(100, 40))
        return complete(**kwargs)

    monkeypatch.setattr(model, 'complete', answer)


def test_large_source_is_auto_compressed_and_provenance_keeps_original(native):
    raw = noisy_png()
    accept_upload(native[1], RUN, 'large.png', raw)
    native[2].turn = result()
    response = call(native, [dict(name='large.png', purpose='product')])
    item = response['observations'][0]
    source = native[2].calls[0]['messages'][0]['content'][1]['source']
    sent = base64.b64decode(source['data'])
    assert source['media_type'] == 'image/jpeg' and sent != raw and len(sent) < len(raw)
    assert resolve(native[1], RUN, 'large.png').read_bytes() == raw
    assert item['sha256'] == hashlib.sha256(raw).hexdigest()
    assert item['image_input']['input_sha256'] == hashlib.sha256(sent).hexdigest()
    assert any('压缩副本' in text for text in item['limitations'])
    assert call(native, [dict(name='large.png', purpose='product')])['reused'] == 1
    assert len(native[2].calls) == 1


def test_batches_are_bounded_and_all_selected_images_are_observed(native, monkeypatch):
    for name in ('second.png', 'third.png'):
        accept_upload(native[1], RUN, name, native[4])
    monkeypatch.setattr('studio.agents.bailian_omni.MAX_INLINE_BYTES', len(native[4]) * 2 - 1)
    answering_observer(native, monkeypatch)
    response = call(native, [dict(name=n, purpose='product') for n in ('fixture.png', 'second.png', 'third.png')])
    assert response['batches'] == 3 and len(response['observations']) == 3
    assert len(native[2].calls) == 3
    for request in native[2].calls:
        sent_bytes = sum(len(base64.b64decode(b['source']['data'])) for b in request['messages'][0]['content'] if b['type'] == 'image')
        assert sent_bytes <= len(native[4]) * 2 - 1
    assert len(Ledger(native[1], native[5]).entries(RUN)) == 3


def test_failed_batch_preserves_completed_observations_and_never_replays(native, monkeypatch):
    for name in ('second.png', 'third.png'):
        accept_upload(native[1], RUN, name, native[4])
    monkeypatch.setattr('studio.agents.bailian_omni.MAX_INLINE_BYTES', len(native[4]) * 2 - 1)
    answering_observer(native, monkeypatch, fail_batch=2)
    items = [dict(name=n, purpose='product') for n in ('fixture.png', 'second.png', 'third.png')]
    with pytest.raises(PipelineError, match='此前已保存 1 张'):
        call(native, items)
    assert len(native[2].calls) == 2 and len(saved_observations(native[1], RUN)) == 1
    # An explicit later invocation reuses the first successful source.
    answering_observer(native, monkeypatch)
    assert call(native, items)['reused'] == 1
    assert len(native[2].calls) == 4 and len(saved_observations(native[1], RUN)) == 3


@pytest.mark.parametrize('scene', [12], indirect=True)
def test_preflight_can_be_corrected_in_same_turn(native, monkeypatch):
    text = '先看商品图'
    scripted(native, monkeypatch, [
        tool_turn('route', 'prepare_creative_context', image_analysis_context(text)),
        tool_turn('invalid', 'observe_images', {'images': [dict(name='missing.png', purpose='product')]}),
        tool_turn('valid', 'observe_images', {'images': [dict(name='fixture.png', purpose='product')]}),
        result(), final_turn('观察已保存'),
    ])
    job = chat(native, text)
    assert job['status'] == 'succeeded', job['status']
    assert len(saved_observations(native[1], RUN)) == 1
    assert len([r for r in Ledger(native[1], native[5]).entries(RUN) if r.stage == 'image_analysis']) == 1
    assert tool_result_for(native[2], 'route')['is_error'] is False
    assert tool_result_for(native[2], 'invalid')['is_error'] is True
    assert '图片必须来自本项目' in tool_result_for(native[2], 'invalid')['content']
    assert tool_result_for(native[2], 'valid')['is_error'] is False
    events = native[1].read_events(RUN)
    task = next(e['interpretation']['task'] for e in events if e['kind'] == 'user_task_interpretation_recorded')
    assert task == image_analysis_context(text)['task_interpretation']['task']
    assert not any(e['kind'] == 'agent_task_prepared' for e in events)
    assert all(native[1].latest_artifact(RUN, stage) is None
               for stage in ('intake', 'direction', 'material_plan', 'writing'))
    assert not native[1].list_approvals(RUN)


@pytest.mark.parametrize('scene', [12], indirect=True)
def test_image_analysis_cannot_start_creative_stages_after_observation(native, monkeypatch):
    text = '只分析商品图，不做创作'
    scripted(native, monkeypatch, [
        tool_turn('route', 'prepare_creative_context', image_analysis_context(text)),
        tool_turn('look', 'observe_images', {'images': [dict(name='fixture.png', purpose='product')]}),
        result(),
        tool_turn('wrong-intake', 'write_stage', dict(stage='intake', content='Should not save', summary='Bad')),
        tool_turn('wrong-dispatch', 'dispatch', dict(stage='direction', brief='Should not run')),
        final_turn('只交付观察'),
    ])
    job = chat(native, text)
    assert job['status'] == 'succeeded', job['status']
    assert len(saved_observations(native[1], RUN)) == 1
    assert tool_result_for(native[2], 'wrong-intake')['is_error'] is True
    assert tool_result_for(native[2], 'wrong-dispatch')['is_error'] is True
    assert not any(e['kind'] == 'agent_task_prepared' for e in native[1].read_events(RUN))
    assert all(native[1].latest_artifact(RUN, stage) is None
               for stage in ('intake', 'direction', 'material_plan', 'writing'))
    assert not native[1].list_approvals(RUN)


@pytest.mark.parametrize('scene', [12], indirect=True)
def test_failed_paid_image_analysis_stays_incomplete_and_cannot_replay(native, monkeypatch):
    text = '只分析商品图'
    scripted(native, monkeypatch, [
        tool_turn('route', 'prepare_creative_context', image_analysis_context(text)),
        tool_turn('first', 'observe_images', {'images': [dict(name='fixture.png', purpose='product')]}),
        result(2),
        tool_turn('replay', 'observe_images', {'images': [dict(name='fixture.png', purpose='product')]}),
        final_turn('观察失败'), final_turn('观察失败'),
    ])
    job = chat(native, text)
    assert job['status'] == 'blocked', job['status']
    assert not saved_observations(native[1], RUN)
    assert len([r for r in Ledger(native[1], native[5]).entries(RUN) if r.stage == 'image_analysis']) == 1
    assert tool_result_for(native[2], 'first')['is_error'] is True
    replay = tool_result_for(native[2], 'replay')
    assert replay['is_error'] is True and '已尝试' in replay['content']
    assert not any(e['kind'] == 'agent_images_observed' for e in native[1].read_events(RUN))
    assert not native[1].list_approvals(RUN)


@pytest.mark.parametrize('scene', [12], indirect=True)
@pytest.mark.parametrize('mode, text', [('status', '看进度'), ('stop', '停止'),
                                      ('discuss', '讨论一下图片'), ('clarify', '先澄清用途')])
def test_nonanalysis_task_cannot_start_image_observation(native, monkeypatch, mode, text):
    context = dict(intent='pause' if mode == 'stop' else 'status', format='none', deliverable='follow_up',
                   task_interpretation={'task': dict(quote=text, mode=mode, deliverable='follow_up',
                       format='none', allowed_stages=[], pause_after=mode == 'stop'), 'constraints': []})
    scripted(native, monkeypatch, [
        tool_turn('route', 'prepare_creative_context', context),
        tool_turn('look', 'observe_images', {'images': [dict(name='fixture.png', purpose='product')]}),
        final_turn('仅处理本轮只读请求'),
    ])
    chat(native, text)
    assert not saved_observations(native[1], RUN)
    assert not [r for r in Ledger(native[1], native[5]).entries(RUN) if r.stage == 'image_analysis']
    assert tool_result_for(native[2], 'look')['is_error'] is True


@pytest.mark.parametrize('scene', [12], indirect=True)
def test_paid_failure_blocks_same_turn_replay_and_cannot_count_as_observed(native, monkeypatch):
    scripted(native, monkeypatch, [
        tool_turn('route', 'prepare_creative_context', dict(intent='new', format='photo', deliverable='direction')),
        tool_turn('first', 'observe_images', {'images': [dict(name='fixture.png', purpose='product')]}),
        result(2),
        tool_turn('replay', 'observe_images', {'images': [dict(name='fixture.png', purpose='product')]}),
        tool_turn('wrong-intake', 'write_stage', dict(stage='intake', content='Should not save', summary='Bad')),
        final_turn('未完成'), final_turn('未完成'),
    ])
    job = chat(native, '看图后给创意方向')
    assert job['status'] == 'blocked'
    assert not saved_observations(native[1], RUN) and not native[1].latest_artifact(RUN, 'intake')
    assert len([r for r in Ledger(native[1], native[5]).entries(RUN) if r.stage == 'image_analysis']) == 1


@pytest.mark.parametrize('invoked', [False, True, None])
def test_only_explicitly_uninvoked_transport_failures_can_correct_input(native, invoked):
    from studio.agent_images import ImagePreflightError
    native[2].error = ModelError(0, 'local or transport failure', provider_invoked=invoked)
    expected = ImagePreflightError if invoked is False else ModelError
    with pytest.raises(expected):
        call(native, [dict(name='fixture.png', purpose='product')])
    row = Ledger(native[1], native[5]).entries(RUN)[0]
    assert row.usage_status == ('known' if invoked is False else 'unknown')
    assert not saved_observations(native[1], RUN)


def test_later_uninvoked_batch_failure_keeps_prior_paid_batch_and_cannot_reopen_selection(native, monkeypatch):
    accept_upload(native[1], RUN, 'second.png', native[4])
    monkeypatch.setattr('studio.agents.bailian_omni.MAX_INLINE_BYTES', len(native[4]) * 2 - 1)
    answering_observer(native, monkeypatch)
    complete = native[2].complete

    def fail_second(**kwargs):
        if len(native[2].calls) == 1:
            native[2].error = ModelError(0, 'Synthetic local second-batch rejection', provider_invoked=False)
        return complete(**kwargs)

    monkeypatch.setattr(native[2], 'complete', fail_second)
    with pytest.raises(ModelError):
        call(native, [dict(name=name, purpose='product') for name in ('fixture.png', 'second.png')])
    assert len(saved_observations(native[1], RUN)) == 1
    entries = Ledger(native[1], native[5]).entries(RUN)
    assert len(entries) == 2 and entries[0].output_tokens == 40
    assert entries[1].usage_status == 'known' and entries[1].output_tokens == 0


@pytest.mark.parametrize('scene', [12], indirect=True)
def test_existing_intake_cannot_dispatch_direction_after_failed_observation(native, monkeypatch):
    pipeline = Pipeline(native[1])
    attempt = pipeline.start_attempt(RUN, 'intake')
    pipeline.submit(RUN, 'intake', attempt.token, content='Existing valid brief', summary='Existing')
    scripted(native, monkeypatch, [
        tool_turn('route', 'prepare_creative_context', dict(intent='new', format='photo', deliverable='direction')),
        tool_turn('look', 'observe_images', {'images': [dict(name='fixture.png', purpose='product')]}),
        result(2),
        tool_turn('bypass', 'dispatch', dict(stage='direction', brief='Should not run')),
        final_turn('未完成'), final_turn('未完成'),
    ])
    job = chat(native, '先看图再给方向')
    assert job['status'] == 'blocked' and not native[1].latest_artifact(RUN, 'direction')
    assert not saved_observations(native[1], RUN)
    assert not any(e['kind'] == 'agent_task_prepared' for e in native[1].read_events(RUN))
