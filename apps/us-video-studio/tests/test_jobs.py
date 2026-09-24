"""Persistent jobs and local-origin boundaries; synthetic clients only."""
from __future__ import annotations

import threading
import time
import hashlib
import io

import pytest
from local_http import TestClient
from PIL import Image

from fakes import ScriptedModel, final_turn, tool_turn
from studio.jobs import WriterLease
from studio.ledger import Ledger
from studio.models import PipelineError
from studio.materials import accept_upload
from studio.pipeline import Pipeline
from studio.production import digest
from studio.server import build_app
from studio.store import Store
from test_server import RUN, make_run, settings_for


def wait_job(http, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = http.get(f'/api/runs/{RUN}/jobs/{job_id}')
        assert result.status_code == 200, result.text
        job = result.json()
        if job['status'] in {'succeeded', 'failed', 'blocked', 'cancelled', 'interrupted'}:
            return job
        threading.Event().wait(.01)
    raise AssertionError('synthetic job did not finish')


def post_job(http, request_id='one', text='本次测试'):
    result = http.post(f'/api/runs/{RUN}/jobs', json={'text': text, 'request_id': request_id})
    assert result.status_code == 200, result.text
    return result.json()


def test_inline_script_feedback_rejects_changed_version_before_model_call(tmp_path):
    model = ScriptedModel([final_turn('收到。')])
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        pipe = Pipeline(Store(tmp_path))
        for stage in ('intake', 'direction', 'material_plan', 'writing'):
            attempt = pipe.start_attempt(RUN, stage)
            artifact = pipe.submit(RUN, stage, attempt.token,
                content='这一句需要核对。' if stage == 'writing' else f'{stage} fixture', summary=stage)
            if stage == 'direction':
                pipe.record_review(RUN, stage, verdict='pass', findings=[])
                nonce = pipe.issue_nonce(RUN, stage)
                pipe.record_approval(RUN, stage, nonce=nonce, decision='approved')
        target = {'run_id': RUN, 'stage': 'writing', 'version': 1,
                  'artifact_sha256': artifact.sha256, 'quote': '这一句需要核对。'}
        wrong = http.post(f'/api/runs/{RUN}/jobs', json={'text': '请修改', 'request_id': 'bad',
            'feedback_target': {**target, 'artifact_sha256': 'b' * 64}})
        assert wrong.status_code == 409
        invented = http.post(f'/api/runs/{RUN}/jobs', json={'text': '请修改', 'request_id': 'invented',
            'feedback_target': {**target, 'quote': '不存在的台词'}})
        assert invented.status_code == 409
        assert not model.calls
        accepted = http.post(f'/api/runs/{RUN}/jobs', json={'text': '请修改', 'request_id': 'good',
            'feedback_target': target})
        assert accepted.status_code == 200, accepted.text
        assert '这一句需要核对。' in accepted.json()['text']
        assert accepted.json()['job_payload']['feedback_target']['artifact_sha256'] == artifact.sha256
        wait_job(http, accepted.json()['job_id'])
        assert artifact.sha256 in model.calls[0]['system']


def test_media_feedback_requires_the_saved_result_bytes(tmp_path):
    model = ScriptedModel([final_turn('收到。')])
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        raw = io.BytesIO()
        Image.new('RGB', (240, 240), (20, 40, 60)).save(raw, 'PNG')
        accept_upload(Store(tmp_path), RUN, 'production-fixture.png', raw.getvalue())
        sha = hashlib.sha256(raw.getvalue()).hexdigest()
        service = http.app.state.production
        service.event(RUN, 'production_attempt', attempt_id='made-output', draft_id='fixture',
                      media_kind='image', status='succeeded')
        service.event(RUN, 'production_output', attempt_id='made-output',
                      output={'name': 'production-fixture.png', 'sha256': sha,
                              'url': f'/api/runs/{RUN}/materials/production-fixture.png'})
        target = {'kind': 'media', 'run_id': RUN, 'attempt_id': 'made-output', 'output_sha256': sha}
        rejected = http.post(f'/api/runs/{RUN}/jobs', json={'text': '画面需要调整',
            'request_id': 'wrong-media', 'feedback_target': {**target, 'output_sha256': 'a' * 64}})
        assert rejected.status_code == 409
        assert not model.calls
        accepted = http.post(f'/api/runs/{RUN}/jobs', json={'text': '画面需要调整',
            'request_id': 'right-media', 'feedback_target': target})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()['job_payload']['feedback_target']['attempt_id'] == 'made-output'
        assert 'made-output' not in accepted.json()['text']
        wait_job(http, accepted.json()['job_id'])
        assert 'made-output' in model.calls[0]['system']


def test_production_draft_feedback_binds_exact_draft_and_optional_attempt(tmp_path):
    model = ScriptedModel([final_turn('收到第一条。'), final_turn('收到第二条。'),
                           final_turn('已记录需核对的错误。')])
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        store = Store(tmp_path)
        drafts = []
        for draft_id in ('draft-current', 'draft-other'):
            draft = {'draft_id': draft_id, 'kind': 'video', 'prompt': '已有制作描述'}
            draft['draft_sha256'] = digest(draft)
            store.append_event(RUN, 'production_draft', draft=draft)
            drafts.append(draft)
        current, other = drafts
        http.app.state.production.event(RUN, 'production_attempt',
            attempt_id='failed-current', draft_id=current['draft_id'],
            draft_sha256=current['draft_sha256'], media_kind='video', status='failed',
            message='输入描述要求嘴部表演，但当前画面是静态商品图。',
            endpoint='https://private.invalid/raw-request')
        http.app.state.production.event(RUN, 'production_attempt',
            attempt_id='failed-other', draft_id=other['draft_id'],
            draft_sha256=other['draft_sha256'], media_kind='video', status='failed')
        target = {'kind': 'production_draft', 'run_id': RUN,
                  'draft_id': current['draft_id'], 'draft_sha256': current['draft_sha256'],
                  'attempt_id': 'failed-current'}
        for request_id, wrong in (
            ('wrong-run', {**target, 'run_id': 'other-run'}),
            ('wrong-draft', {**target, 'draft_id': 'missing-draft'}),
            ('wrong-sha', {**target, 'draft_sha256': 'a' * 64}),
            ('wrong-attempt', {**target, 'attempt_id': 'failed-other'}),
            ('missing-attempt', {**target, 'attempt_id': 'missing-attempt'}),
        ):
            rejected = http.post(f'/api/runs/{RUN}/jobs', json={
                'text': '请调整这版描述', 'request_id': request_id, 'feedback_target': wrong})
            assert rejected.status_code == 409, rejected.text
        assert not model.calls
        accepted = http.post(f'/api/runs/{RUN}/jobs', json={
            'text': '请调整这版描述', 'request_id': 'valid-attempt', 'feedback_target': target})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()['text'] == '请调整这版描述'
        assert accepted.json()['job_payload']['feedback_target'] == target
        wait_job(http, accepted.json()['job_id'])
        assert current['draft_sha256'] in model.calls[0]['system']
        assert 'failed-current' in model.calls[0]['system']
        assert '输入描述要求嘴部表演，但当前画面是静态商品图。' in model.calls[0]['system']
        assert 'message_untrusted' in model.calls[0]['system']
        assert 'private.invalid' not in model.calls[0]['system']
        assert 'draft-current' not in accepted.json()['text']

        without_attempt = {key: value for key, value in target.items() if key != 'attempt_id'}
        accepted = http.post(f'/api/runs/{RUN}/jobs', json={
            'text': '讨论这份方案', 'request_id': 'valid-draft',
            'feedback_target': without_attempt})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()['text'] == '讨论这份方案'
        assert accepted.json()['job_payload']['feedback_target'] == without_attempt
        wait_job(http, accepted.json()['job_id'])
        assert 'attempt_evidence' not in model.calls[1]['system']

        http.app.state.production.status(RUN, 'failed-current', status='failed',
            message='https://private.invalid/result?api_key=synthetic-secret')
        accepted = http.post(f'/api/runs/{RUN}/jobs', json={
            'text': '请解释这次失败', 'request_id': 'sensitive-message',
            'feedback_target': target})
        assert accepted.status_code == 200, accepted.text
        wait_job(http, accepted.json()['job_id'])
        assert '具体说明已省略' in model.calls[2]['system']
        assert 'synthetic-secret' not in model.calls[2]['system']
        assert len(model.calls) == 3


def test_job_replay_idempotency_and_context_refs(tmp_path):
    model = ScriptedModel([final_turn('测试回复')])
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        assert http.get(f'/api/runs/{RUN}/jobs/latest').json() is None
        created = post_job(http)
        job = wait_job(http, created['job_id'])
        assert job['status'] == 'succeeded'
        assert job['events'][-1]['type'] == 'done'
        assert job['events'][-1]['text'] == '测试回复'
        assert any(e['type'] == 'context_ready' and e['messages_sha256'] for e in job['events'])
        assert job['context_refs']['run_brief_sha256']
        assert 'orchestrator' in job['model_config']['roles']
        assert post_job(http)['job_id'] == job['job_id']
        assert http.get(f'/api/runs/{RUN}/jobs/latest').json()['job_id'] == job['job_id']
        assert http.get(f'/api/runs/{RUN}/jobs/{job["job_id"]}',
                        params={'after_seq': job['cursor']}).json()['events'] == []
        conflict = http.post(f'/api/runs/{RUN}/jobs', json={'text': '另一条', 'request_id': 'one'})
        assert conflict.status_code == 409
        assert len(model.calls) == 1
        assert len(http.get(f'/api/runs/{RUN}/chat').json()) == 2
    # A new process/session reads completed progress without asking the model again.
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: pytest.fail('no replay model'))) as http:
        assert http.get(f'/api/runs/{RUN}/jobs/latest').json()['status'] == 'succeeded'
        assert post_job(http)['job_id'] == job['job_id']


def test_model_end_turn_does_not_claim_missing_writing_was_delivered(tmp_path):
    model = ScriptedModel([
        tool_turn('route', 'prepare_creative_context', {'intent': 'continue', 'format': 'video', 'deliverable': 'writing'}),
        final_turn('完整方案已经做好。'),
        final_turn('目前没有写稿，不能称为已交付。'),
    ])
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        job = wait_job(http, post_job(http, text='继续做完整方案')['job_id'])
        assert job['status'] == 'blocked'
        assert job['deliverable']['status'] == 'incomplete'
        assert 'writing 当前准确版本' in job['reason']
        assert any(e['type'] == 'done' and e['stopped_because'] == 'incomplete' for e in job['events'])


def test_refresh_does_not_own_worker_and_root_busy_is_explicit(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Slow:
        calls = 0
        def complete(self, **kwargs):
            self.calls += 1
            entered.set()
            assert release.wait(4)
            return final_turn('完成')
    model = Slow()
    app = build_app(settings_for(tmp_path), client_factory=lambda: model)
    with TestClient(app) as http:
        make_run(http)
        created = post_job(http)
        assert entered.wait(2)
        refreshed = TestClient(app)
        assert refreshed.get(f'/api/runs/{RUN}/jobs/latest').json()['status'] == 'running'
        assert post_job(refreshed)['job_id'] == created['job_id']
        http.post('/api/runs', json={'run_id': 'other-run', 'title': '其他', 'brief': '测试'})
        busy = http.post('/api/runs/other-run/jobs', json={'text': '其他', 'request_id': 'two'})
        assert busy.status_code == 409
        assert '未调用模型' in busy.json()['detail']
        release.set()
        assert wait_job(refreshed, created['job_id'])['status'] == 'succeeded'
        assert model.calls == 1


def test_cancel_waits_for_inflight_settlement_then_blocks_tool(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Slow:
        def complete(self, **kwargs):
            entered.set()
            assert release.wait(4)
            return tool_turn('late', 'write_stage', {'stage': 'intake', 'content': '不可写入', 'summary': '测试'})
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: Slow())) as http:
        make_run(http)
        job = post_job(http)
        assert entered.wait(2)
        cancel = http.post(f'/api/runs/{RUN}/jobs/{job["job_id"]}/cancel')
        assert cancel.json()['status'] == 'cancelling'
        assert cancel.json()['unresolved_calls'][0]['call_status'] == 'pending'
        release.set()
        final = wait_job(http, job['job_id'])
        assert final['status'] == 'cancelled'
        assert final['unresolved_calls'] == []
        assert http.get(f'/api/runs/{RUN}/artifacts').json() == []
        rows = http.get(f'/api/runs/{RUN}/ledger').json()['rows']
        assert len(rows) == 1 and rows[0]['usage_status'] == 'known'


def test_cancelled_late_final_never_enters_chat(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Slow:
        def complete(self, **kwargs):
            entered.set()
            assert release.wait(4)
            return final_turn('旧目标的迟到回复')
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: Slow())) as http:
        make_run(http)
        job = post_job(http)
        assert entered.wait(2)
        assert http.post(f'/api/runs/{RUN}/jobs/{job["job_id"]}/cancel').json()['status'] == 'cancelling'
        release.set()
        assert wait_job(http, job['job_id'])['status'] == 'cancelled'
        assert all('旧目标的迟到回复' not in row['text'] for row in http.get(f'/api/runs/{RUN}/chat').json())


def test_inflight_steer_settles_old_job_then_continues_once(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Slow:
        def __init__(self):
            self.calls = []
        def complete(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                entered.set()
                assert release.wait(4)
                return final_turn('旧方向不应保存')
            return final_turn('已按调整后的目标处理')
    model = Slow()
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        first = post_job(http, text='先做完整视频方向')
        assert entered.wait(2)
        path = f'/api/runs/{RUN}/jobs/{first["job_id"]}/steer'
        change = {'request_id': 'adjust-1', 'text': '先只分析，不写脚本', 'expected_goal_revision': 0}
        accepted = http.post(path, json=change)
        assert accepted.status_code == 200
        assert accepted.json()['goal_revision'] == 1
        assert accepted.json()['steer_status'] == 'received'
        assert http.post(path, json=change).status_code == 200
        assert http.post(path, json={**change, 'text': '另一条'}).status_code == 409
        second = {'request_id': 'adjust-2', 'text': '保留已有观察', 'expected_goal_revision': 1}
        assert http.post(path, json=second).json()['goal_revision'] == 2
        release.set()
        old = wait_job(http, first['job_id'])
        assert old['status'] == 'cancelled'
        assert old['successor_job_id']
        successor = wait_job(http, old['successor_job_id'])
        assert successor['status'] == 'succeeded'
        assert successor['parent_job_id'] == first['job_id']
        assert len(model.calls) == 2
        request = str(model.calls[1]['messages'])
        assert '先做完整视频方向' in request
        assert '先只分析，不写脚本' in request
        assert '保留已有观察' in request
        chat = http.get(f'/api/runs/{RUN}/chat').json()
        assert not any('旧方向不应保存' in row['text'] for row in chat)
        assert chat[-1]['text'] == '已按调整后的目标处理'
        assert http.post(path, json=change).json()['successor_job_id'] == successor['job_id']


def test_stop_after_steer_discards_automatic_continuation(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Slow:
        def complete(self, **kwargs):
            entered.set()
            assert release.wait(4)
            return final_turn('旧回复')
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: Slow())) as http:
        make_run(http)
        first = post_job(http)
        assert entered.wait(2)
        path = f'/api/runs/{RUN}/jobs/{first["job_id"]}'
        http.post(path + '/steer', json={
            'text': '改目标', 'request_id': 'change', 'expected_goal_revision': 0})
        assert http.post(path + '/cancel').json()['steer_status'] == 'discarded'
        release.set()
        old = wait_job(http, first['job_id'])
        assert old['status'] == 'cancelled' and old['successor_job_id'] is None
        assert http.get(f'/api/runs/{RUN}/jobs/latest').json()['job_id'] == first['job_id']


def test_stop_cannot_be_reinterpreted_as_new_steering(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Slow:
        def complete(self, **kwargs):
            entered.set()
            assert release.wait(4)
            return final_turn('旧回复')
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: Slow())) as http:
        make_run(http)
        first = post_job(http)
        assert entered.wait(2)
        path = f'/api/runs/{RUN}/jobs/{first["job_id"]}'
        assert http.post(path + '/cancel').json()['status'] == 'cancelling'
        rejected = http.post(path + '/steer', json={
            'text': '现在改目标', 'request_id': 'after-stop', 'expected_goal_revision': 0})
        assert rejected.status_code == 409
        release.set()
        old = wait_job(http, first['job_id'])
        assert old['successor_job_id'] is None


def test_job_event_subscription_replays_durable_marks_without_private_payloads(tmp_path):
    model = ScriptedModel([final_turn('完成')])
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        first = post_job(http)
        ended = wait_job(http, first['job_id'])
        response = http.get(f'/api/runs/{RUN}/jobs/{first["job_id"]}/events')
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/event-stream')
        assert 'data: {"seq":' in response.text
        assert '"kind": "job_status"' in response.text
        assert '完成' not in response.text
        after = http.get(f'/api/runs/{RUN}/jobs/{first["job_id"]}/events',
                         params={'after_seq': ended['cursor']})
        assert after.text == ''


def test_restart_marks_interrupted_and_preserves_unknown_call(tmp_path):
    settings = settings_for(tmp_path)
    store = Store(tmp_path)
    store.create_run(RUN, '测试', '测试')
    store.append_event(RUN, 'job_created', job_id='interrupted-job', request_id='old', text='旧指令',
                       model_config={}, context_refs={})
    store.append_event(RUN, 'job_status', job_id='interrupted-job', status='running')
    call_id = Ledger(store, settings).begin_call(RUN, role='orchestrator', stage='intake', model='synthetic')
    original_ledger = (store.run_dir(RUN) / 'ledger.jsonl').read_bytes()
    model = ScriptedModel([final_turn('不应调用')])
    with TestClient(build_app(settings, client_factory=lambda: model)) as http:
        job = http.get(f'/api/runs/{RUN}/jobs/latest').json()
        assert job['status'] == 'interrupted' and job['can_continue']
        assert job['unresolved_calls'][0]['call_id'] == call_id
        assert '不会自动重发' in job['recovery']
        assert post_job(http, 'old', '旧指令')['job_id'] == 'interrupted-job'
        next_job = post_job(http, 'new', '先继续')
        assert wait_job(http, next_job['job_id'])['status'] == 'succeeded'
        assert len(model.calls) == 1
        assert (store.run_dir(RUN) / 'ledger.jsonl').read_bytes().startswith(original_ledger)
        assert Ledger(store, settings).totals().unknown_calls == 1


@pytest.mark.parametrize(('before', 'after'), [
    ('running', 'interrupted'), ('cancelled', 'cancelled'),
])
def test_restart_keeps_steer_receipt_without_replaying_model(tmp_path, before, after):
    settings = settings_for(tmp_path)
    store = Store(tmp_path)
    store.create_run(RUN, '测试', '测试')
    store.append_event(RUN, 'job_created', job_id='old-job', request_id='original',
                       text='先做方向', model_config={}, context_refs={}, job_kind='conversation')
    store.append_event(RUN, 'job_status', job_id='old-job', status=before)
    store.append_event(RUN, 'job_steer_received', job_id='old-job', request_id='adjust',
                       text='改成分析', expected_goal_revision=0, goal_revision=1)
    with TestClient(build_app(settings, client_factory=lambda: pytest.fail('no replay model'))) as http:
        job = http.get(f'/api/runs/{RUN}/jobs/old-job').json()
        assert job['status'] == after
        assert job['steer_status'] == 'blocked'
        assert '未自动接续' in job['steer_reason']
        assert job['successor_job_id'] is None
        assert http.get(f'/api/runs/{RUN}/jobs/latest').json()['job_id'] == 'old-job'


@pytest.mark.parametrize(('before', 'after'), [
    ('running', 'interrupted'), ('cancelled', 'cancelled'),
])
def test_restart_resumes_saved_steer_only_after_explicit_action(tmp_path, before, after):
    settings = settings_for(tmp_path)
    store = Store(tmp_path)
    store.create_run(RUN, '测试', '测试')
    store.append_event(RUN, 'job_created', job_id='old-job', request_id='original',
                       text='先做方向', model_config={}, context_refs={}, job_kind='conversation')
    store.append_event(RUN, 'job_status', job_id='old-job', status=before)
    store.append_event(RUN, 'job_steer_received', job_id='old-job', request_id='adjust',
                       text='改成分析', expected_goal_revision=0, goal_revision=1)
    model = ScriptedModel([final_turn('按新目标完成')])
    with TestClient(build_app(settings, client_factory=lambda: model)) as http:
        old = http.get(f'/api/runs/{RUN}/jobs/old-job').json()
        assert old['status'] == after and old['steer_status'] == 'blocked'
        assert old['steer_latest_text'] == '改成分析'
        assert model.calls == []
        resumed = http.post(f'/api/runs/{RUN}/jobs/old-job/resume-steer', json={})
        assert resumed.status_code == 200, resumed.text
        child = wait_job(http, resumed.json()['job_id'])
        assert child['status'] == 'succeeded'
        assert child['parent_job_id'] == 'old-job'
        assert '先做方向' in child['text'] and '改成分析' in child['text']
        assert len(model.calls) == 1
        retry = http.post(f'/api/runs/{RUN}/jobs/old-job/resume-steer', json={})
        assert retry.status_code == 200 and retry.json()['job_id'] == child['job_id']
        assert len(model.calls) == 1


def test_unresolved_calls_are_limited_to_this_run(tmp_path):
    """Another run's pending ledger row must not appear in this run's job view."""
    settings = settings_for(tmp_path)
    store = Store(tmp_path)
    store.create_run(RUN, '本 run', '测试')
    store.create_run('other-run', '另一 run', '测试')
    other_call = Ledger(store, settings).begin_call(
        'other-run', role='orchestrator', stage='intake', model='synthetic')
    store.append_event(RUN, 'job_created', job_id='job-a', request_id='r1', text='hi',
                       model_config={}, context_refs={})
    store.append_event(RUN, 'job_status', job_id='job-a', status='interrupted')
    with TestClient(build_app(settings, client_factory=lambda: ScriptedModel([final_turn('x')]))) as http:
        job = http.get(f'/api/runs/{RUN}/jobs/latest').json()
        assert job['status'] == 'interrupted'
        assert all(row['call_id'] != other_call for row in job['unresolved_calls'])
        assert job['unresolved_calls'] == []
        assert [r.call_id for r in Ledger(store, settings).entries(RUN)] == []
        assert other_call in [r.call_id for r in Ledger(store, settings).entries('other-run')]


def test_writer_lease_refuses_second_service_and_releases(tmp_path):
    first = WriterLease(tmp_path)
    second = WriterLease(tmp_path)
    first.acquire()
    try:
        with pytest.raises(PipelineError, match='写入进程'):
            second.acquire()
    finally:
        first.close()
    second.acquire()
    second.close()


@pytest.mark.parametrize('headers', [
    {'origin': 'https://attacker.invalid'}, {'host': 'attacker.invalid'},
    {'origin': 'null'}, {'sec-fetch-site': 'cross-site'},
    {'host': 'localhost:4760', 'origin': 'http://localhost:9999'},
    {'origin': 'https://testserver'}, {'host': 'testserver'},
])
def test_cross_origin_requests_never_reach_jobs_or_key_save(tmp_path, headers):
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: pytest.fail('no model'))) as http:
        make_run(http)
        assert http.post(f'/api/runs/{RUN}/jobs', headers=headers,
            json={'text': 'test', 'request_id': 'bad'}).status_code == 403
        assert http.put('/api/connections', headers=headers, json={}).status_code == 403
        assert http.get('/api/connections', headers=headers).status_code == 403
        assert http.get(f'/api/runs/{RUN}/jobs/latest').json() is None


def test_json_content_type_required_before_state_mutation(tmp_path):
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: pytest.fail('no model'))) as http:
        make_run(http)
        assert http.post(f'/api/runs/{RUN}/jobs', content='{"text":"x","request_id":"one"}',
                         headers={'content-type': 'text/plain'}).status_code == 415
        assert http.put('/api/connections', content='{}',
                        headers={'content-type': 'application/x-www-form-urlencoded'}).status_code == 415


def test_validation_errors_do_not_echo_credentials(tmp_path):
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: pytest.fail('no model'))) as http:
        response = http.put('/api/connections', json=[{'api_key': 'synthetic-secret-must-not-echo'}])
        assert response.status_code == 422
        assert 'synthetic-secret-must-not-echo' not in response.text


def test_role_connections_are_frozen_per_job_and_keys_never_enter_events(tmp_path, monkeypatch):
    import studio.agents.client as transport
    import studio.connections as connections
    monkeypatch.setattr(connections, '_dpapi', lambda data, **kwargs: data)
    entered, release = threading.Event(), threading.Event()
    model = ScriptedModel([
        tool_turn("route", "prepare_creative_context", {"intent": "edit", "format": "video"}),
        tool_turn('i', 'write_stage', {'stage': 'intake', 'content': '需求', 'summary': '需求'}),
        tool_turn('d', 'dispatch', {'stage': 'direction', 'brief': '两个方向'}),
        tool_turn('s', 'submit_stage', {'content': '方案', 'summary': '方向'}),
        final_turn('主控结束'),
    ])
    seen = []
    class Adapter:
        def __init__(self, base_url, token, **kwargs):
            seen.append((base_url, token))
        def complete(self, **kwargs):
            if not model.calls:
                entered.set()
                assert release.wait(4)
            return model.complete(**kwargs)
    monkeypatch.setattr(transport, 'AnthropicCompatClient', Adapter)
    def payload(revision, key, model_name):
        return {'expected_revision': revision, 'connections': [{
            'id': 'test', 'provider': 'anthropic_compat',
            'base_url': 'https://example.invalid/anthropic', 'api_key': key}],
            'roles': {role: {'connection': 'test', 'model': model_name} for role in
                      ('orchestrator', 'director', 'material', 'writer', 'reviewer')}}
    with TestClient(build_app(settings_for(tmp_path))) as http:
        make_run(http)
        saved = http.put('/api/connections', json=payload(0, 'synthetic-key-v1', 'model-v1'))
        assert saved.status_code == 200 and 'synthetic-key-v1' not in saved.text
        job = post_job(http)
        assert entered.wait(2)
        changed = http.put('/api/connections', json=payload(1, 'synthetic-key-v2', 'model-v2'))
        assert changed.status_code == 200
        release.set()
        final = wait_job(http, job['job_id'])
        assert final['status'] == 'succeeded'
        assert len(model.calls) == 5  # Current-turn routing plus a real director submission.
        assert http.get(f"/api/runs/{RUN}/artifacts/direction/1").status_code == 200
        assert {c['model'] for c in model.calls} == {'model-v1'}
        assert {token for _, token in seen} == {'synthetic-key-v1'}
        assert final['model_config']['revision'] == 1
        assert http.get('/api/connections').json()['revision'] == 2
        persisted = (tmp_path / 'runs' / RUN / 'events.jsonl').read_text(encoding='utf-8')
        assert 'synthetic-key-v1' not in persisted and 'synthetic-key-v2' not in persisted


@pytest.mark.parametrize('provider', ['codex_cli', 'anthropic_compat'])
def test_connection_preflight_problem_is_visible_without_unknown_charge(tmp_path, monkeypatch, provider):
    from studio.agents.codex_client import CodexClient, CodexUnavailable
    from studio.connections import ConnectionStore
    if provider == 'codex_cli':
        def unavailable(self):
            raise CodexUnavailable('Codex CLI is not installed or not on PATH')
        monkeypatch.setattr(CodexClient, 'preflight', unavailable)
    else:
        ConnectionStore(tmp_path).save({'expected_revision': 0,
            'connections': [{'id': 'missing-key', 'provider': provider,
                             'base_url': 'https://example.invalid/anthropic'}],
            'roles': {role: {'connection': 'missing-key', 'model': 'synthetic'} for role in
                      ('orchestrator', 'director', 'material', 'writer', 'reviewer')}})
    with TestClient(build_app(settings_for(tmp_path))) as http:
        make_run(http)
        job = wait_job(http, post_job(http)['job_id'])
        assert job['status'] == 'blocked'
        assert ('PATH' if provider == 'codex_cli' else 'API key') in job['reason']
        assert job['unresolved_calls'] == []
        assert http.get(f'/api/runs/{RUN}/ledger').json()['rows'] == []
