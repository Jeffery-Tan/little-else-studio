"""Persistent jobs and local-origin boundaries; synthetic clients only."""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from fakes import ScriptedModel, final_turn, tool_turn
from studio.jobs import WriterLease
from studio.ledger import Ledger
from studio.models import PipelineError
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
    {'origin': 'https://testserver'},
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
