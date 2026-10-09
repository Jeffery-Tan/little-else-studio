"""Explicit selected-field edits use the writer and return to the user."""
from __future__ import annotations

import time

import pytest
from local_http import TestClient

from fakes import ScriptedModel, final_turn, tool_turn
from studio.pipeline import Pipeline
from studio.server import build_app
from studio.store import Store
from test_server import RUN, make_run, settings_for


def seed(pipe):
    def write(stage, structured):
        attempt = pipe.start_attempt(RUN, stage)
        return pipe.submit(RUN, stage, attempt.token, content='', summary=stage,
                           structured=structured, evidence=['fixture'], limitations=[])

    write('intake', {'facts': [{'label': 'Product', 'value': 'Bag', 'source': 'fixture'}]})
    write('direction', {'options': [{'key': 'A', 'claim': 'Lunch', 'hook': 'Desk',
                                     'satisfies': 'goal.primary'},
                                    {'key': 'B', 'claim': 'Picnic', 'hook': 'Park',
                                     'satisfies': 'goal.primary'}], 'recommended': 'B'})
    pipe.record_review(RUN, 'direction', verdict='pass', findings=[])
    pipe.record_approval(RUN, 'direction', nonce=pipe.issue_nonce(RUN, 'direction'),
                         decision='approved', selected_option_id='A')
    write('material_plan', {'items': [{'shot': 'Desk', 'status': '不确定'}]})
    writing = write('writing', {'segments': [
        {'tc': '0–3', 'picture': 'Open bag', 'line': 'Original line', 'sound': 'Room', 'duration': '3'},
        {'tc': '3–6', 'picture': 'Lunch', 'line': 'Keep second line', 'sound': 'Room', 'duration': '3'}],
        'total_duration': '6秒', 'assumptions': ['Pacing unverified']})
    pipe.record_review(RUN, 'writing', verdict='pass', findings=[])
    pipe.record_approval(RUN, 'writing', nonce=pipe.issue_nonce(RUN, 'writing'), decision='approved')
    return writing


def wait_job(http, job_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = http.get(f'/api/runs/{RUN}/jobs/{job_id}').json()
        if job['status'] in {'succeeded', 'failed', 'blocked', 'cancelled', 'interrupted'}:
            return job
        time.sleep(.01)
    raise AssertionError('synthetic task did not settle')


def target(artifact, **changes):
    return {'kind': 'writing', 'run_id': RUN, 'stage': 'writing', 'version': artifact.version,
            'artifact_sha256': artifact.sha256, 'quote': 'Original line',
            'path': ['segments', 0, 'line'], **changes}


@pytest.mark.parametrize(('content_format', 'writing_name'), [('video', '脚本'), ('photo', '图文方案')])
def test_explicit_writing_field_uses_only_writer(tmp_path, monkeypatch, content_format, writing_name):
    monkeypatch.setattr('studio.learning.LearningService.prepare_context', lambda *a, **k: {
        'text': 'FIXTURE_METHOD', 'snapshot_sha256': 'f' * 64, 'evidence_ref': 'fixture-method'})
    model = ScriptedModel([
        tool_turn('patch', 'submit_patch', {'changes': [
            {'path': ['segments', 0, 'line'], 'value': 'Try this line'}], 'summary': 'Changed opening'})])
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        store = Store(tmp_path)
        pipe = Pipeline(store)
        original = seed(pipe)
        store.append_event(RUN, 'creative_context_selected', selection={
            'intent': 'continue', 'format': content_format, 'tags': ['fixture-tag'], 'case_ids': None})
        url = f'/api/runs/{RUN}/actions/revise-writing'
        payload = {'request_id': 'edit-1', 'text': '只把第一句改得自然一点，其他保留',
                   'feedback_target': target(original)}

        wrong = http.post(url, json={**payload, 'request_id': 'wrong',
                                     'feedback_target': target(original, path=['segments', 1, 'line'])})
        assert wrong.status_code == 409 and not model.calls
        accepted = http.post(url, json=payload)
        assert accepted.status_code == 200, accepted.text
        job = wait_job(http, accepted.json()['job_id'])
        assert job['status'] == 'succeeded' and job['deliverable']['status'] == 'complete'
        assert len(model.calls) == 1
        assert 'submit_patch' in model.calls[0]['tool_names']
        assert 'dispatch' not in model.calls[0]['tool_names']
        assert not any('prepare_creative_context' in c['tool_names'] for c in model.calls)
        revised = store.latest_artifact(RUN, 'writing')
        assert revised.version == 2
        reply = store.read_chat(RUN)[-1]['text']
        assert reply == f'{writing_name}第 2 版已保存。请阅读这一版后决定是否采用。'
        assert 'writing-v' not in reply
        assert job['revision_result'] == {
            'stage': 'writing', 'version': 2, 'artifact_sha256': revised.sha256,
            'change_status': 'saved', 'review_status': 'not_requested'}
        assert http.get(f'/api/runs/{RUN}/jobs/{job["job_id"]}',
                        params={'after_seq': job['cursor']}).json()['revision_result'] == job['revision_result']
        assert revised.structured['segments'][0]['line'] == 'Try this line'
        assert revised.structured['segments'][1]['line'] == 'Keep second line'
        assert pipe.stage_state(RUN, 'writing').review is None
        assert pipe.stage_state(RUN, 'writing').approval is None
        assert pipe.stage_state(RUN, 'direction').approval.selected_option_id == 'A'
        tasks = [e for e in store.read_events(RUN) if e['kind'] == 'agent_task_prepared']
        assert len(tasks) == 1 and tasks[0]['role'] == 'writer'
        assert tasks[0]['plan_id']
        selections = [e['selection'] for e in store.read_events(RUN)
                      if e['kind'] == 'creative_context_selected']
        assert selections[-1]['intent'] == 'edit' and selections[-1]['tags'] == ['fixture-tag']
        assert http.post(url, json=payload).json()['job_id'] == job['job_id']
        assert len(model.calls) == 1


def test_saved_writing_revision_needs_no_second_model_call(tmp_path, monkeypatch):
    monkeypatch.setattr('studio.learning.LearningService.prepare_context', lambda *a, **k: {
        'text': 'FIXTURE_METHOD', 'snapshot_sha256': 'f' * 64, 'evidence_ref': 'fixture-method'})
    model = ScriptedModel([
        tool_turn('patch', 'submit_patch', {'changes': [
            {'path': ['segments', 0, 'line'], 'value': 'A new opening'}],
            'summary': 'Changed opening'})])
    with TestClient(build_app(settings_for(tmp_path, max_tool_rounds=1), client_factory=lambda: model)) as http:
        make_run(http)
        store = Store(tmp_path)
        pipe = Pipeline(store)
        original = seed(pipe)
        accepted = http.post(f'/api/runs/{RUN}/actions/revise-writing', json={
            'request_id': 'edit-review-case', 'text': '只修改第一句',
            'feedback_target': target(original)})
        assert accepted.status_code == 200, accepted.text
        job = wait_job(http, accepted.json()['job_id'])
        revised = store.latest_artifact(RUN, 'writing')
        assert revised.version == 2
        assert revised.structured['segments'][0]['line'] == 'A new opening'
        assert revised.structured['segments'][1]['line'] == 'Keep second line'
        assert job['status'] == 'succeeded'
        assert job['revision_result'] == {
            'stage': 'writing', 'version': 2, 'artifact_sha256': revised.sha256,
            'change_status': 'saved', 'review_status': 'not_requested'}
        assert http.get(f'/api/runs/{RUN}/jobs/{job["job_id"]}',
                        params={'after_seq': job['cursor']}).json()['revision_result'] == job['revision_result']
        assert pipe.stage_state(RUN, 'writing').approval is None
        assert len(model.calls) == 1
        assert job['deliverable']['status'] == 'complete'
        assert pipe.stage_state(RUN, 'writing').review is None


def test_writing_revision_without_patch_never_reports_saved(tmp_path, monkeypatch):
    monkeypatch.setattr('studio.learning.LearningService.prepare_context', lambda *a, **k: {
        'text': 'FIXTURE_METHOD', 'snapshot_sha256': 'f' * 64, 'evidence_ref': 'fixture-method'})
    model = ScriptedModel([final_turn('No patch submitted.')])
    with TestClient(build_app(settings_for(tmp_path, max_tool_rounds=1), client_factory=lambda: model)) as http:
        make_run(http)
        store = Store(tmp_path)
        original = seed(Pipeline(store))
        accepted = http.post(f'/api/runs/{RUN}/actions/revise-writing', json={
            'request_id': 'edit-no-patch', 'text': '只修改第一句',
            'feedback_target': target(original)})
        assert accepted.status_code == 200, accepted.text
        job = wait_job(http, accepted.json()['job_id'])
        assert job['status'] == 'failed'
        assert job['revision_result'] is None
        assert store.latest_artifact(RUN, 'writing').version == 1
        assert len(model.calls) == 1


def test_selected_text_normal_send_and_status_remain_conversation(tmp_path):
    model = ScriptedModel([final_turn('先讨论这句。'), final_turn('当前进度已读取。')])
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        store = Store(tmp_path)
        original = seed(Pipeline(store))
        for request_id, text, selected in (
            ('discuss-1', '这句为什么这样写？', True), ('status-1', '现在进度如何？', False)):
            body = {'request_id': request_id, 'text': text}
            if selected:
                body['feedback_target'] = target(original)
            accepted = http.post(f'/api/runs/{RUN}/jobs', json=body)
            assert accepted.status_code == 200, accepted.text
            assert wait_job(http, accepted.json()['job_id'])['status'] == 'succeeded'
        assert len(model.calls) == 2
        assert not any(e['kind'] == 'agent_task_prepared' for e in store.read_events(RUN))
        assert store.latest_artifact(RUN, 'writing').version == 1
