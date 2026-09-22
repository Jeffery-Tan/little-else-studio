"""Natural conversation uses one Job and distinct, bounded reference modes."""
import json
import hashlib

import pytest

from fakes import tool_turn, final_turn
from studio.agents.client import Turn, Usage
from studio.creation_mode import current, select
from studio.creative_evidence import creative_evidence
from studio.learning import LearningService
from studio.materials import accept_upload
from test_image_analysis import scene, wait_job
from test_bailian_omni import native, MODEL, RESULT
from test_reference_video import URL, downloader
from test_server import RUN
from test_video_observation import video_bytes


def scripted(native, monkeypatch, turns):
    model=native[2]
    original=model.__class__.complete.__get__(model)
    queue=iter(turns)
    def complete(**kwargs):
        model.turn=next(queue)
        return original(**kwargs)
    monkeypatch.setattr(model,'complete',complete)


def observation():
    return Turn(text=json.dumps(RESULT),stop_reason='end_turn',model=MODEL,usage=Usage(100,20))


def inspect(action, source='local.mp4', quote='分析这个', scope='开场动作与声音'):
    return tool_turn('inspect','observe_reference',dict(source=source,action=action,quote=quote,scope=scope))


def chat(native,text='分析这个',request_id='natural-1',consent=True):
    response=native[0].post(f'/api/runs/{RUN}/jobs',json=dict(text=text,request_id=request_id,reference_consent=consent))
    assert response.status_code==200,response.text
    return wait_job(native[0],response.json())


def test_analyze_ends_turn_without_dispatch_or_adoption(native, monkeypatch):
    accept_upload(native[1],RUN,'local.mp4',video_bytes(audio=True))
    scripted(native,monkeypatch,[inspect('analyze'),observation()])
    job=chat(native)
    assert job['status']=='succeeded',job
    assert len(native[2].calls)==2
    assert not creative_evidence(native[1],RUN)['reference_observations']
    assert current(native[1],RUN) is None
    assert not native[1].latest_artifact(RUN,'intake')
    messages=native[1].read_chat(RUN)
    assert sum('参考视频分析' in r.get('text','') for r in messages)==1
    assert chat(native)['job_id']==job['job_id'] and len(native[2].calls)==2


def test_recreate_and_autonomous_get_different_methods_and_reference_context(native,monkeypatch):
    accept_upload(native[1],RUN,'local.mp4',video_bytes(audio=True))
    scripted(native,monkeypatch,[inspect('recreate',quote='照这条做'),observation(),
        tool_turn('route','prepare_creative_context',dict(intent='new',format='video')),final_turn('这是本款改编方案')])
    assert chat(native,'照这条做')['status']=='succeeded'
    creation=current(native[1],RUN)
    assert creation['mode']=='recreate'
    assert len(creative_evidence(native[1],RUN)['reference_observations'])==1
    sent=json.dumps(native[2].calls[-1],ensure_ascii=False)
    assert '指定参考的视频改编方法' in sent and '开场动作与声音' in sent
    learning=LearningService(native[1])
    for role in ('director','material','writer','reviewer'):
        context=learning.context(RUN,role,format='video',purpose='review' if role=='reviewer' else 'message')
        assert context['creation_mode']['reference_id']==creation['reference_id']
        assert any(s['id']=='reference-adaptation' for s in context['skills'])
    select(native[1],RUN,'autonomous')
    assert not creative_evidence(native[1],RUN)['reference_observations']
    context=learning.context(RUN,'director',format='video')
    assert not any(s['id']=='reference-adaptation' for s in context['skills'])


def test_reuse_reference_after_analysis_never_downloads_or_analyzes_again(native,monkeypatch):
    calls=downloader(monkeypatch,video_bytes(audio=True))
    scripted(native,monkeypatch,[inspect('analyze',source=URL,quote='分析这个'),observation()])
    assert chat(native,'分析这个 '+URL)['status']=='succeeded'
    scripted(native,monkeypatch,[inspect('inspiration',source=URL,quote='只借开头'),
        tool_turn('route','prepare_creative_context',dict(intent='new',format='video')),final_turn('其余自主设计')])
    assert chat(native,'只借开头',request_id='natural-2')['status']=='succeeded'
    assert calls==[URL] and len(native[2].calls)==5
    assert current(native[1],RUN)['mode']=='inspiration'


def test_bare_reference_can_ask_one_question_without_download(native,monkeypatch):
    downloads=downloader(monkeypatch,video_bytes(audio=True))
    scripted(native,monkeypatch,[tool_turn('question','ask_user',dict(question='想分析还是复刻？',
        options=[dict(label='分析'),dict(label='复刻')],recommended=0,context=''))])
    assert chat(native,URL)['status']=='succeeded'
    assert downloads==[] and len(native[2].calls)==1


@pytest.mark.parametrize('fault',['consent','quote','url','scope'])
def test_invalid_request_cannot_cross_reference_boundary(native,monkeypatch,fault):
    downloads=downloader(monkeypatch,video_bytes(audio=True))
    turns=[]
    if fault=='scope':turns.append(tool_turn('readonly','prepare_creative_context',dict(intent='status',format='none')))
    turns.extend([inspect('analyze',source=URL,quote='不存在的指令' if fault=='quote' else '分析这个'),final_turn('保留当前内容')])
    scripted(native,monkeypatch,turns)
    chat(native,'分析这个'+('' if fault=='url' else ' '+URL),consent=fault!='consent')
    assert not downloads


def test_failed_fetch_is_not_retried_in_same_model_turn(native,monkeypatch):
    from studio.tiktok_reference import ReferenceFetchError
    calls=[]
    def fail(*args):calls.append(1);raise ReferenceFetchError('下载失败')
    monkeypatch.setattr('studio.reference_video.fetch_reference',fail)
    scripted(native,monkeypatch,[inspect('analyze',source=URL),inspect('analyze',source=URL),final_turn('请上传原视频继续')])
    chat(native,'分析这个 '+URL)
    assert len(calls)==1


@pytest.mark.parametrize('changed',['source','analysis'])
def test_selected_reference_cannot_silently_change_version(native,monkeypatch,changed):
    from studio.creation_mode import context, reference_candidate
    from studio.materials import resolve
    accept_upload(native[1],RUN,'local.mp4',video_bytes(audio=True))
    scripted(native,monkeypatch,[inspect('recreate',quote='照这条做'),observation(),final_turn('已读取')])
    assert chat(native,'照这条做')['status']=='succeeded'
    selected=current(native[1],RUN)
    candidate=reference_candidate(native[1],RUN,selected['reference_id'])
    if changed=='source':
        resolve(native[1],RUN,'local.mp4').write_bytes(b'changed source')
    else:
        replacement={**candidate,'limitations':['new analysis version']}
        native[1].append_event(RUN,'video_analysis_candidate',candidate=replacement)
    with pytest.raises(ValueError,match='变化'):
        context(native[1],RUN)
    assert not creative_evidence(native[1],RUN)['reference_observations']
