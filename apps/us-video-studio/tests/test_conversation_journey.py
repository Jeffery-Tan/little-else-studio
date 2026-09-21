import hashlib
import time

from fastapi.testclient import TestClient
import pytest
from fakes import ScriptedModel, tool_turn, final_turn
from studio.server import build_app
from studio.store import Store
from studio.interactions import ask, pending_question
from studio.tools.registry import tool_names_for
from test_server import settings_for
from test_production import png


QUESTION = {'question': '你希望更偏哪种表达？', 'context': '我推荐先做自然的日常使用演示。',
            'options': [{'label': '日常分享', 'description': '从生活场景带出商品'},
                        {'label': '直接演示', 'description': '快速说明使用方式'}], 'recommended': 0}


def wait(http, run_id, job):
    for _ in range(500):
        row = http.get(f'/api/runs/{run_id}/jobs/{job["job_id"]}').json()
        if row['status'] not in {'running', 'queued'}:
            return row
        time.sleep(.01)
    pytest.fail('offline job did not finish')


def test_first_message_reserves_exactly_one_project_without_calling_model(tmp_path):
    model = ScriptedModel([])
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        body = {'text': '用这些图做一条自然的商品视频', 'request_id': 'first'}
        first = http.post('/api/conversations', json=body)
        assert first.status_code == 200
        assert http.post('/api/conversations', json=body).json() == first.json()
        assert http.post('/api/conversations', json={**body, 'text': '换一个想法'}).status_code == 409
        assert len(http.get('/api/runs').json()) == 1
        assert not model.calls
        assert http.get(f'/api/runs/{first.json()["run_id"]}/jobs/latest').json() is None


def test_upload_retry_accepts_same_bytes_without_overwrite(tmp_path):
    with TestClient(build_app(settings_for(tmp_path))) as http:
        run = http.post('/api/conversations', json={'text': '参考图', 'request_id': 'upload'}).json()
        raw = png(); sha = hashlib.sha256(raw).hexdigest()
        path = f'/api/runs/{run["run_id"]}/materials?retry_sha256={sha}'
        files = {'file': ('reference.png', raw, 'image/png')}
        assert http.post(path, files=files).status_code == 200
        assert http.post(path, files=files).json()['already_saved']
        assert http.post(path, files={'file': ('reference.png', b'changed', 'image/png')}).status_code == 409
        assert http.get(f'/api/runs/{run["run_id"]}/materials/reference.png').content == raw


def test_question_wait_resume_and_retry_never_grant_approval(tmp_path):
    model = ScriptedModel([tool_turn('ask', 'ask_user', QUESTION), final_turn('我按你的意思完善方案。')])
    store = Store(tmp_path)
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        run_id = http.post('/api/conversations', json={'text': '做条视频', 'request_id': 'chat'}).json()['run_id']
        first = http.post(f'/api/runs/{run_id}/jobs', json={'text': '做条视频', 'request_id': 'start'}).json()
        assert wait(http, run_id, first)['status'] == 'succeeded'
        assert len(model.calls) == 1  # Asking yields immediately, no extra model turn.
        q = http.get(f'/api/runs/{run_id}/timeline').json()['interaction']
        assert q['options'][0]['label'] == '日常分享'
        answer = {'text': '选推荐项，但不要夸张表演', 'request_id': 'answer', 'question_id': q['id']}
        second = http.post(f'/api/runs/{run_id}/jobs', json=answer).json()
        assert wait(http, run_id, second)['status'] == 'succeeded'
        assert http.post(f'/api/runs/{run_id}/jobs', json=answer).json()['job_id'] == second['job_id']
        assert http.post(f'/api/runs/{run_id}/jobs', json={**answer, 'request_id': 'stale'}).status_code == 409
        assert not pending_question(store, run_id)
        assert store.list_approvals(run_id) == []
        assert '日常分享' in str(model.calls[-1])  # The model sees the question on resume.


def test_question_options_validate_and_do_not_leak_tools_to_workers(tmp_path):
    store = Store(tmp_path); store.create_run('fixture', 'Test', 'Test')
    with pytest.raises(ValueError):
        ask(store, 'fixture', {**QUESTION, 'recommended': 3})
    assert pending_question(store, 'fixture') is None
    for role in ['director', 'writer', 'material', 'reviewer']:
        assert 'ask_user' not in tool_names_for(role)
        assert 'review_production' not in tool_names_for(role)
