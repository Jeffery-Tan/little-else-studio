"""Whole-message in-flight controls through actual isolated HTTP ingress."""
import pytest

from local_http import TestClient
from studio.server import build_app
from studio.steer_intent import classify, needs_stop, continuation_allowed
from test_conversation_runtime_control import BlockingModel, start, wait_idle
from test_server import RUN, make_run, settings_for


@pytest.mark.parametrize('text', [
    '还在做吗', '还在跑吗', '是不是卡住了', '不要停止',
    '啥情况，还在做吗，怎么这么久', '我只是想看一下进度，不用停止任务',
    '进度怎么样？' * 20, '目前是哪一步在等待呢', '模型现在执行正常吗', '别停',
    '你看下现在的状态，好像卡住了一样', '帮我查一下当前进度',
])
def test_natural_status_keeps_exact_original_job_without_successor(tmp_path, text):
    model = BlockingModel()
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        original = start(http, model)
        try:
            response = http.post(f'/api/runs/{RUN}/jobs/{original["job_id"]}/steer', json={
                'text': text, 'request_id': 'question', 'expected_goal_revision': 0})
            assert response.status_code == 200, response.text
            assert response.json()['status'] == 'running'
            assert response.json()['steer_status'] == 'answered'
            assert not http.app.state.jobs.cancelled.is_set()
        finally:
            model.release.set()
            wait_idle(http)
        assert len(model.calls) == 1
        assert not http.app.state.jobs.get(RUN, original['job_id'])['successor_job_id']


@pytest.mark.parametrize('text', ['请停止正在执行的任务', '先停一下，让我看下当前结果',
                                 '停止当前任务，不要自动接续'])
def test_stop_is_terminal_and_never_recycled_as_modification(tmp_path, text):
    model = BlockingModel()
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        original = start(http, model)
        try:
            response = http.post(f'/api/runs/{RUN}/jobs/{original["job_id"]}/steer', json={
                'text': text, 'request_id': 'stop', 'expected_goal_revision': 0})
            assert response.status_code == 200, response.text
            assert response.json()['steer_class'] == 'stop'
            assert response.json()['status'] == 'cancelling'
        finally:
            model.release.set()
            wait_idle(http)
        assert len(model.calls) == 1
        assert not http.app.state.jobs.get(RUN, original['job_id'])['successor_job_id']


def test_explicit_keep_running_defers_mixed_edit_without_losing_original(tmp_path):
    model = BlockingModel()
    text = '不要停止当前任务，结束后把第二镜台词改得自然一点'
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        original = start(http, model)
        try:
            response = http.post(f'/api/runs/{RUN}/jobs/{original["job_id"]}/steer', json={
                'text': text, 'request_id': 'defer', 'expected_goal_revision': 0})
            assert response.status_code == 200, response.text
            assert response.json()['steer_class'] == 'deferred'
            assert not http.app.state.jobs.cancelled.is_set()
        finally:
            model.release.set()
            wait_idle(http)
        successor = http.app.state.jobs.get(RUN, original['job_id'])['successor_job_id']
        assert successor and len(model.calls) == 2
        assert text in str(model.calls[1]['messages'])


@pytest.mark.parametrize('text', [
    '改一下第二镜，进度我看不到', '继续准备脚本，视频先别生成',
    '如果卡住就停止，先继续', '不要停止这个词不要写在口播里',
])
def test_mentions_questions_and_future_prohibitions_are_not_control_commands(text):
    result = classify(text)
    assert result['class'] == 'work'
    assert needs_stop(result) and continuation_allowed(result)
