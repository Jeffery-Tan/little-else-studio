"""Immediate stop clauses survive explanations without granting a successor."""
import pytest

from local_http import TestClient
from studio.server import build_app
from studio.steer_intent import classify, continuation_allowed, needs_stop
from test_conversation_runtime_control import BlockingModel, start, wait_idle
from test_server import RUN, make_run, settings_for


@pytest.mark.parametrize('text', [
    '先停一下，我想重新考虑这个方向',
    '停止当前任务，这个结果不对',
    '不要继续了，先说明现在做到了哪里',
    '这次先不用生成了，成本太高',
    '我不满意现在的方向，停止当前任务',
    '停止当前任务，然后把第二镜改成室内',
    '先停一下，再开始一个新项目',
    '请先停止当前任务，我想重新考虑这个方向',
    '马上停止当前任务，我需要核对结果',
    '停止当前任务，等我看完结果再决定下一步',
    '当前方案不对，先停一下',
])
def test_unconditional_stop_wins_over_explanation_or_new_work(text):
    result = classify(text)
    assert result['class'] == 'stop'
    assert needs_stop(result)
    assert not continuation_allowed(result)


@pytest.mark.parametrize('text', [
    '不要停止当前任务，继续观察',
    '如果卡住了，停止当前任务',
    '停止当前任务，如果它卡住了',
    '要是又报错，先停一下',
    '先把当前任务做完，停止当前任务',
    '等当前任务结束，先停一下',
    '先做完再停',
    '他说，停止当前任务',
    '把台词改成“停止当前任务，这不对”',
    '不要停止，先停一下这句话只是台词',
    '停止当前任务？',
    '先停一下吗？我不确定',
    '继续准备脚本，视频先别生成',
])
def test_qualified_or_quoted_stops_are_not_unconditional_stop_controls(text):
    assert classify(text)['class'] != 'stop'


@pytest.mark.parametrize('text', [
    '先停一下，我想重新考虑这个方向',
    '停止当前任务，然后把第二镜改成室内',
])
def test_compound_stop_preserves_full_input_and_never_starts_a_successor(tmp_path, text):
    model = BlockingModel()
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        original = start(http, model)
        try:
            response = http.post(f'/api/runs/{RUN}/jobs/{original["job_id"]}/steer', json={
                'text': text, 'request_id': 'compound-stop', 'expected_goal_revision': 0})
            assert response.status_code == 200, response.text
            assert response.json()['steer_class'] == 'stop'
            assert response.json()['status'] == 'cancelling'
        finally:
            model.release.set()
            wait_idle(http)
        job = http.app.state.jobs.get(RUN, original['job_id'])
        assert job['status'] == 'cancelled'
        assert not job['successor_job_id']
        assert len(model.calls) == 1
        rows = http.app.state.jobs.store.read_events(RUN)
        receipt = next(row for row in rows if row['kind'] == 'user_message_received'
                       and row['request_id'] == 'compound-stop')
        steer = next(row for row in rows if row['kind'] == 'job_steer_received'
                     and row['request_id'] == 'compound-stop')
        assert receipt['text'] == steer['text'] == text
