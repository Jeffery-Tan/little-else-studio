"""Explicit reference recovery retains the original receipt and frozen goal."""
import hashlib
import json

import pytest

from fakes import fixture_task_interpretation, tool_turn, final_turn
from studio.agents.client import Turn, Usage
from studio.conversation_input import receive, accept, start_execution, record_task_interpretation
from studio.materials import accept_upload
from studio.reference_video import task_state
from test_bailian_omni import native, RESULT, MODEL
from test_image_analysis import scene, wait_job
from test_reference_video import URL, request, submit, downloader
from test_server import RUN
from test_video_observation import video_bytes


def original_task(native, *, goal='writing', saved=True, action='recreate'):
    http, store, model, *_ = native
    if saved:
        payload, _ = request(native)
        assert submit(native, payload)['status'] == 'succeeded'
        task = task_state(store, RUN)[0]
    else:
        task = {'reference_id': 'reference-0001', 'intent': 'analyze',
                'instruction': 'Only the opening visual mechanism', 'source_url': URL, 'status': 'started'}
        store.append_event(RUN, 'reference_video_started', reference_id=task['reference_id'],
            request={k: task[k] for k in ('intent', 'instruction', 'source_url')})
    source = task.get('source_url') or task['name']
    text = '请按我给出的参考完成本品完整方案 ' + source
    if goal == 'analysis':
        text = '请拆解这条参考视频 ' + source
    elif goal == 'reference_prompt':
        text = '请把所选参考适配为本品提示词 ' + source
    receipt = receive(store, RUN, text, 'original-conversation', reference_consent=True)
    accept(store, RUN, receipt)
    start_execution(store, RUN, receipt)
    proposal = fixture_task_interpretation(text, deliverable=goal)
    if goal == 'analysis':
        proposal['task'].update(mode='analyze', allowed_stages=[])
    proposal['video_use'] = {'source': source, 'purpose': 'reference', 'quote': text}
    interpretation = record_task_interpretation(store, RUN, receipt, proposal)
    store.append_event(RUN, 'job_created', job_id='original-job', request_id='original-conversation',
        text=text, job_kind='conversation', user_input_id=receipt['message_id'])
    store.append_event(RUN, 'reference_observation_requested', job_id='original-job', source=source,
        source_user_input_id=receipt['message_id'], interpretation_id=interpretation['id'],
        action=action, scope='Only the opening visual mechanism')
    store.append_event(RUN, 'reference_video_execution', reference_id=task['reference_id'], job_id='original-job')
    store.append_event(RUN, 'job_status', job_id='original-job', status='failed', reason='Mock planner failure')
    return task_state(store, RUN)[0], receipt


def recovery_input(task, **over):
    recovery = task['recovery']
    return {'text': '请恢复这项未完成的原任务，保留原来的最终交付。',
        'request_id': 'explicit-recovery', 'reference_consent': recovery['requires_observation'],
        'reference_recovery': {key: recovery[key] for key in
            ('reference_id', 'parent_job_id', 'source_user_input_id', 'interpretation_id')}, **over}


def strict_turns(monkeypatch, model, turns):
    """No fixture interpretation injection: recovery must supply the saved contract."""
    original = model.__class__.complete.__get__(model)
    queue = iter(turns)
    def complete(**kwargs):
        model.turn = next(queue)
        return original(**kwargs)
    monkeypatch.setattr(model, 'complete', complete)


def post_recovery(native, payload):
    response = native[0].post(f'/api/runs/{RUN}/jobs', json=payload)
    assert response.status_code == 200, response.text
    return wait_job(native[0], response.json())


def test_recovery_projection_reads_original_goal_not_observation_scope(native):
    task, receipt = original_task(native)
    assert task['intent'] == 'analyze'
    recovery = task['recovery']
    assert recovery['deliverable'] == 'writing'
    assert recovery['original_text'] == receipt['text']
    assert recovery['reference_scope'] == 'Only the opening visual mechanism'
    assert recovery['parent_job_id'] == 'original-job'
    assert recovery['source_user_input_id'] == receipt['message_id']
    assert recovery['action'] == 'recreate' and recovery['allowed']
    assert not recovery['requires_observation']


def test_standalone_reference_preserves_its_own_goal_without_conversation_recovery(native):
    payload, _ = request(native)
    assert submit(native, payload)['status'] == 'succeeded'
    assert 'recovery' not in task_state(native[1], RUN)[0]


def test_cached_analysis_recovery_has_only_text_model_and_honors_new_real_supplement(native, monkeypatch):
    task, receipt = original_task(native, goal='analysis', action='analyze')
    before = len(native[2].calls)
    monkeypatch.setattr('studio.agents.bailian_omni.connection_info', lambda: None)
    text = '请继续原拆解；补充要求：解释已经观察到的声画关系。'
    strict_turns(monkeypatch, native[2], [final_turn('Mock explanation of the existing observed sound-picture relationship')])
    payload = recovery_input(task, reference_consent=False, text=text)
    job = post_recovery(native, payload)
    assert job['status'] == 'succeeded' and job['deliverable']['goal'] == 'analysis'
    assert len(native[2].calls) == before + 1
    request_text = json.dumps(native[2].calls[before:], ensure_ascii=False)
    assert text in request_text and receipt['text'] in request_text
    assert RESULT['observations'][0]['visible'] in request_text
    assert 'Mock explanation' in job['events'][-1]['text']
    assert native[0].post(f'/api/runs/{RUN}/jobs', json=payload).json()['job_id'] == job['job_id']
    assert len(native[2].calls) == before + 1
    assert job['parent_job_id'] == 'original-job'
    assert job['job_payload']['source_user_input_id'] == receipt['message_id']
    current = task_state(native[1], RUN)[0]
    assert current['job_id'] == job['job_id'] and current['execution_status'] == 'succeeded'
    assert current['recovery']['source_user_input_id'] == receipt['message_id']


def test_cached_prompt_recovery_uses_original_contract_and_sees_new_real_supplement(native, monkeypatch):
    task, original = original_task(native, goal='reference_prompt', action='prompt')
    before = len(native[2].calls)
    strict_turns(monkeypatch, native[2], [
        tool_turn('scope', 'prepare_creative_context', dict(intent='continue', format='video', deliverable='reference_prompt')),
        tool_turn('save', 'save_reference_prompt', dict(prompt='Mock product-specific recovered prompt')),
    ])
    text = '请继续原提示词任务；补充要求：保留本次真实恢复补充。'
    job = post_recovery(native, recovery_input(task, text=text, reference_consent=False))
    assert job['status'] == 'succeeded', job
    assert job['deliverable']['goal'] == 'reference_prompt'
    sent = json.dumps(native[2].calls[before:], ensure_ascii=False)
    assert original['text'] in sent and text in sent
    assert RESULT['observations'][0]['visible'] in sent
    assert len(native[2].calls) == before + 2
    assert not any(e['kind'] == 'agent_task_prepared' for e in native[1].read_events(RUN))


def test_recovery_cannot_downgrade_original_writing_goal_to_analysis(native, monkeypatch):
    task, _ = original_task(native)
    strict_turns(monkeypatch, native[2], [
        tool_turn('wrong', 'prepare_creative_context', dict(intent='status', format='none', deliverable='analysis')),
        final_turn('Mock report cannot complete writing'),
    ])
    job = post_recovery(native, recovery_input(task, reference_consent=False))
    assert job['status'] == 'blocked', job
    assert job['deliverable']['goal'] == 'writing' and job['deliverable']['status'] == 'incomplete'


def test_failed_download_can_upload_original_then_resume_original_planning(native, monkeypatch):
    task, _ = original_task(native, goal='intake', saved=False, action='analyze')
    raw = video_bytes(audio=True)
    accept_upload(native[1], RUN, 'fallback.mp4', raw)
    strict_turns(monkeypatch, native[2], [
        Turn(text=json.dumps(RESULT), stop_reason='end_turn', model=MODEL, usage=Usage(100, 20)),
        tool_turn('scope', 'prepare_creative_context', dict(intent='continue', format='video', deliverable='intake')),
        tool_turn('write', 'write_stage', dict(stage='intake', content='Mock recovered original planning', summary='Mock intake')),
        final_turn('Mock intake completed'),
    ])
    payload = recovery_input(task, reference_consent=True, attachments=[{'name': 'fallback.mp4', 'purpose': 'reference'}])
    payload['reference_recovery'].update(name='fallback.mp4', expected_sha256=hashlib.sha256(raw).hexdigest())
    job = post_recovery(native, payload)
    assert job['status'] == 'succeeded', job
    assert job['deliverable']['goal'] == 'intake'
    current = task_state(native[1], RUN)[0]
    assert current['name'] == 'fallback.mp4' and current['status'] == 'completed'
    assert current['recovery']['deliverable'] == 'intake'
    assert len(native[2].calls) == 4


@pytest.mark.parametrize('text', ['停止', '现在进度怎么样？', '只讨论为什么失败，不要改稿', '只分析这条参考'])
def test_readonly_or_goal_changing_recovery_retains_receipt_without_starting_work(native, text):
    task, _ = original_task(native, saved=False)
    before = len(native[2].calls)
    response = native[0].post(f'/api/runs/{RUN}/jobs', json=recovery_input(task, text=text))
    assert response.status_code == 409, response.text
    assert len(native[2].calls) == before
    assert any(e['kind'] == 'user_message_received' and e['text'] == text for e in native[1].read_events(RUN))


@pytest.mark.parametrize('field', ['parent_job_id', 'source_user_input_id', 'interpretation_id'])
def test_recovery_rejects_mismatched_original_binding(native, field):
    task, _ = original_task(native)
    payload = recovery_input(task)
    payload['reference_recovery'][field] = 'another-identity'
    response = native[0].post(f'/api/runs/{RUN}/jobs', json=payload)
    assert response.status_code == 409, response.text


def test_incomplete_observation_uses_new_receipt_admission_not_old_consent(native):
    task, _ = original_task(native, saved=False)
    response = native[0].post(f'/api/runs/{RUN}/jobs', json=recovery_input(task, reference_consent=False))
    assert response.status_code == 409
    assert '本次恢复尚需视频观察' in response.text


def test_natural_failure_restores_dependency_and_original_goal_in_one_job(native, monkeypatch):
    from studio.tiktok_reference import ReferenceFetchError
    def fail(*_):
        raise ReferenceFetchError('Mock original download failure')
    monkeypatch.setattr('studio.reference_video.fetch_reference', fail)
    text = '请先拆解这条参考，再完成本品需求方案 ' + URL
    proposal = fixture_task_interpretation(text, deliverable='intake')
    proposal['video_use'] = {'source': URL, 'purpose': 'reference', 'quote': text}
    strict_turns(monkeypatch, native[2], [
        tool_turn('observe', 'observe_reference', dict(source=URL, action='analyze',
            scope='Opening visual mechanism', quote=text, task_interpretation=proposal)),
        final_turn('Mock dependency failed'),
    ])
    response = native[0].post(f'/api/runs/{RUN}/jobs', json={
        'text': text, 'request_id': 'actual-natural-original', 'reference_consent': True})
    original = wait_job(native[0], response.json())
    assert original['status'] == 'blocked' and original['deliverable']['goal'] == 'intake'
    task = task_state(native[1], RUN)[0]
    assert task['recovery']['parent_job_id'] == original['job_id']
    assert task['recovery']['original_text'] == text
    downloads = downloader(monkeypatch, video_bytes(audio=True))
    strict_turns(monkeypatch, native[2], [
        Turn(text=json.dumps(RESULT), stop_reason='end_turn', model=MODEL, usage=Usage(100, 20)),
        tool_turn('scope', 'prepare_creative_context', dict(intent='continue', format='video', deliverable='intake')),
        tool_turn('write', 'write_stage', dict(stage='intake', content='Mock original intake restored', summary='Mock intake')),
        final_turn('Mock original intake completed'),
    ])
    recovered = post_recovery(native, recovery_input(task))
    assert recovered['status'] == 'succeeded', recovered
    assert recovered['deliverable']['goal'] == 'intake'
    assert downloads == [URL]
    assert recovered['parent_job_id'] == original['job_id']
    assert len([e for e in native[1].read_events(RUN) if e['kind'] == 'job_created']) == 2


def test_second_failed_recovery_binds_current_owner_and_reuses_same_original_contract(native, monkeypatch):
    task, original = original_task(native)
    strict_turns(monkeypatch, native[2], [final_turn('Mock original goal still incomplete')])
    first = post_recovery(native, recovery_input(task))
    assert first['status'] == 'blocked'
    current = task_state(native[1], RUN)[0]
    assert current['recovery']['parent_job_id'] == first['job_id']
    assert current['recovery']['source_user_input_id'] == original['message_id']
    assert current['recovery']['action'] == 'recreate'
    stale = recovery_input(task, request_id='stale-recovery')
    assert native[0].post(f'/api/runs/{RUN}/jobs', json=stale).status_code == 409
    strict_turns(monkeypatch, native[2], [final_turn('Mock original writing still incomplete')])
    second = post_recovery(native, recovery_input(current, request_id='second-recovery'))
    assert second['parent_job_id'] == first['job_id']
    assert second['deliverable']['goal'] == 'writing'
    newest = task_state(native[1], RUN)[0]['recovery']
    assert newest['parent_job_id'] == second['job_id']
    assert newest['source_user_input_id'] == original['message_id'] and newest['action'] == 'recreate'


def test_restart_preserves_reference_recovery_binding_and_never_resubmits_automatically(native):
    from local_http import TestClient
    from studio.server import build_app
    task, original = original_task(native, goal='analysis', action='analyze')
    native[1].append_event(RUN, 'job_status', job_id='original-job', status='running')
    native[0].app.state.jobs.close()
    from fakes import ScriptedModel
    calls = []
    def recovery_client():
        model = ScriptedModel([final_turn('Mock recovered analysis')], legacy_task_contracts=False)
        calls.append(model)
        return model
    app = build_app(native[5], client_factory=recovery_client)
    with TestClient(app) as http:
        restarted = http.get(f'/api/runs/{RUN}/reference-videos').json()['tasks'][0]
        assert restarted['execution_status'] == 'interrupted'
        assert restarted['recovery']['source_user_input_id'] == original['message_id']
        assert len([e for e in native[1].read_events(RUN) if e['kind'] == 'job_created']) == 2
        assert calls == []
        response = http.post(f'/api/runs/{RUN}/jobs', json=recovery_input(restarted))
        assert response.status_code == 200, response.text
        recovered = wait_job(http, response.json())
        assert recovered['status'] == 'succeeded' and recovered['deliverable']['goal'] == 'analysis'
        assert len(calls) == 1 and len(calls[0].calls) == 1
