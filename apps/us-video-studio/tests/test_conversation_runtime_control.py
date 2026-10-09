"""Actual ingress, inbox and cancellation boundaries; isolated data and fake models."""
import copy
import json
import threading
import time

import pytest
from local_http import TestClient
from fakes import ScriptedModel, final_turn, tool_turn
from studio.server import build_app
from studio.steer_intent import classify, continuation_allowed, needs_stop
from test_server import RUN, make_run, settings_for


@pytest.mark.parametrize('text,expected', [
    ('生成完了吗', 'status'),
    ('不要停止当前任务', 'status'),
    ('别取消任务，我只是问进度', 'status'),
    ('暂停', 'stop'),
    ('先别做了，等我确认', 'stop'),
    ('这次先不用生成了', 'stop'),
    ('只讨论一下，先不要改', 'discussion'),
    ('继续准备脚本，视频先别生成', 'work'),
])
def test_inflight_control_respects_negation_wait_and_discussion(text, expected):
    intent = classify(text)
    assert intent['class'] == expected
    assert needs_stop(intent) is (expected in {'stop', 'work'})
    assert continuation_allowed(intent) is (expected in {'work', 'discussion'})


class BlockingModel:
    def __init__(self, proposal=None):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.proposal = proposal

    def complete(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if len(self.calls) == 1:
            self.entered.set()
            assert self.release.wait(8), 'fake model was not released'
            return final_turn('原任务的固定答复')
        if len(self.calls) == 2 and self.proposal:
            return tool_turn('pick', 'propose_user_action', self.proposal)
        return final_turn('本条消息处理结束')


def wait_idle(http):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        jobs = http.app.state.jobs
        with jobs.lock:
            if jobs.active is None:
                return
        time.sleep(.01)
    raise AssertionError('offline execution did not settle')


def start(http, model, run_id=RUN):
    response = http.post(f'/api/runs/{run_id}/jobs', json={
        'text': '先读一下当前项目进展', 'request_id': 'original'})
    assert response.status_code == 200, response.text
    assert model.entered.wait(3)
    return response.json()


def test_status_has_a_real_reply_before_answered_without_a_new_model_call(tmp_path):
    model = BlockingModel()
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        original = start(http, model)
        try:
            response = http.post(f'/api/runs/{RUN}/jobs/{original["job_id"]}/steer', json={
                'text': '生成完了吗', 'request_id': 'status', 'expected_goal_revision': 0})
            assert response.status_code == 200, response.text
            assert response.json()['status'] == 'running'
            assert response.json()['steer_status'] == 'answered'
            assert not http.app.state.jobs.cancelled.is_set()
            store = http.app.state.jobs.store
            events = store.read_events(RUN)
            receipt = next(e for e in events if e['kind'] == 'user_message_received'
                           and e['request_id'] == 'status')
            answer = next(e for e in events if e['kind'] == 'job_steer_answered'
                          and e['request_id'] == 'status')
            assert answer['user_input_id'] == receipt['message_id']
            assert answer['snapshot']['job_status'] == 'running'
            assert '当前任务仍在执行' in answer['text']
            assert any(c['role'] == 'assistant' and c['text'] == answer['text'] for c in store.read_chat(RUN))
            assert len(model.calls) == 1
        finally:
            model.release.set()
            wait_idle(http)
        assert not http.app.state.jobs.get(RUN, original['job_id'])['successor_job_id']


def test_busy_adoption_keeps_genuine_receipt_and_display_authority(tmp_path):
    from test_conversational_decision import seed_direction, RUN as seeded_run
    from studio.decision import candidates
    store, pipeline = seed_direction(tmp_path)
    card = candidates(store, pipeline, seeded_run)[0]
    model = BlockingModel({'action': 'select_direction', 'target': card['target'],
                           'option_id': 'B'})
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        display = http.get(f'/api/runs/{seeded_run}/timeline').json()['display_context_id']
        original = start(http, model, seeded_run)
        try:
            response = http.post(f'/api/runs/{seeded_run}/jobs/{original["job_id"]}/steer', json={
                'text': '采用 B', 'request_id': 'busy-choice', 'expected_goal_revision': 0,
                'display_context_id': display})
            assert response.status_code == 200, response.text
        finally:
            model.release.set()
            wait_idle(http)
        events = store.read_events(seeded_run)
        receipt = next(e for e in events if e['kind'] == 'user_message_received'
                       and e['request_id'] == 'busy-choice')
        child = next(e for e in events if e['kind'] == 'job_created'
                     and e.get('parent_job_id') == original['job_id'])
        assert child['user_input_id'] == receipt['message_id']
        assert receipt['display_context_id'] == display
        assert store.list_approvals(seeded_run)[0].selected_option_id == 'B'
        assert '没有新的真实用户决定' not in json.dumps(model.calls, ensure_ascii=False)
        replay = http.post(f'/api/runs/{seeded_run}/jobs/{original["job_id"]}/steer', json={
            'text': '采用 B', 'request_id': 'busy-choice', 'expected_goal_revision': 0,
            'display_context_id': display})
        assert replay.status_code == 200
        assert len(store.list_approvals(seeded_run)) == 1


def test_multiple_busy_messages_keep_separate_turn_identity(tmp_path):
    model = BlockingModel()
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        original = start(http, model)
        try:
            path = f'/api/runs/{RUN}/jobs/{original["job_id"]}/steer'
            for revision, text in enumerate(('只回答当前项目状态', '再说明已有观察覆盖')):
                response = http.post(path, json={'text': text, 'request_id': f'insert-{revision}',
                                                'expected_goal_revision': revision})
                assert response.status_code == 200, response.text
        finally:
            model.release.set()
            wait_idle(http)
        events = http.app.state.jobs.store.read_events(RUN)
        inputs = [e for e in events if e['kind'] == 'user_message_received'
                  and e['request_id'].startswith('insert-')]
        children = [e for e in events if e['kind'] == 'job_created' and e.get('parent_job_id')]
        assert len(children) == 2
        assert [e['user_input_id'] for e in children] == [e['message_id'] for e in inputs]
        assert len({e['request_id'] for e in children}) == 2
        assert children[0]['job_payload']['pending_user_inputs'] == [inputs[1]['message_id']]
        assert inputs[0]['text'] in json.dumps(model.calls[1]['messages'], ensure_ascii=False)
        assert inputs[1]['text'] not in json.dumps(model.calls[1]['messages'], ensure_ascii=False)
        assert inputs[1]['text'] in json.dumps(model.calls[2]['messages'], ensure_ascii=False)


def test_actual_http_role_hierarchy_requires_saved_artifact_and_exposes_no_prompt(tmp_path):
    model = ScriptedModel([
        tool_turn('route', 'prepare_creative_context',
                  {'intent': 'new', 'format': 'video', 'deliverable': 'direction'}),
        tool_turn('intake', 'write_stage',
                  {'stage': 'intake', 'content': 'Known isolated facts', 'summary': 'fixture'}),
        tool_turn('director', 'dispatch', {'stage': 'direction', 'brief': 'Isolated direction task'}),
        tool_turn('submit', 'submit_stage', {'content': 'Synthetic direction', 'summary': 'fixture'}),
        tool_turn('delivery-check', 'submit_delivery_check', {'verdict': 'ready',
            'summary': 'Synthetic saved direction check for this offline lifecycle.', 'findings': []}),
        final_turn('方向已保存')])
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        response = http.post(f'/api/runs/{RUN}/jobs', json={
            'text': '为这件商品准备创意方向', 'request_id': 'hierarchy'})
        assert response.status_code == 200, response.text
        wait_idle(http)
        store = http.app.state.jobs.store
        before = store.read_events(RUN)
        response = http.get(f'/api/runs/{RUN}/agent-runtime')
        assert response.status_code == 200, response.text
        tasks = response.json()['tasks']
        primary = next(t for t in tasks if t['role'] == 'orchestrator')
        child = next(t for t in tasks if t['role'] == 'director')
        assert primary['task_id'] == http.app.state.jobs.latest(RUN)['job_id']
        assert primary['status'] == child['status'] == 'completed'
        assert len([e for e in before if e['kind'] == 'delivery_self_check']) == 1
        assert model.calls[-2]['tool_names'] == ['submit_delivery_check']
        assert model.calls[-1]['tool_names'] == []
        assert child['parent_task_id'] == child['parent_job_id'] == primary['task_id']
        assert child['source_message_id'] == primary['source_message_id']
        artifact = store.latest_artifact(RUN, 'direction')
        assert child['task_id'] == artifact.attempt
        assert child['artifact_receipt']['sha256'] == artifact.sha256
        assert 'dispatch' in primary['allowed_tools'] and 'dispatch' not in child['allowed_tools']
        assert store.read_events(RUN) == before
        assert 'Isolated direction task' not in response.text


def test_stop_revokes_inherited_busy_inbox_without_starting_remaining_message(tmp_path):
    class TwoTurns(BlockingModel):
        def __init__(self):
            super().__init__()
            self.child_entered, self.child_release = threading.Event(), threading.Event()

        def complete(self, **kwargs):
            if not self.calls:
                return super().complete(**kwargs)
            self.calls.append(copy.deepcopy(kwargs))
            self.child_entered.set()
            assert self.child_release.wait(8)
            return final_turn('子任务固定答复')

    model = TwoTurns()
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        original = start(http, model)
        try:
            path = f'/api/runs/{RUN}/jobs/{original["job_id"]}/steer'
            for revision, text in enumerate(('说明已有作品', '说明已有素材')):
                response = http.post(path, json={'text': text, 'request_id': f'queue-{revision}',
                                                'expected_goal_revision': revision})
                assert response.status_code == 200, response.text
            model.release.set()
            assert model.child_entered.wait(4)
            child = http.app.state.jobs.latest(RUN)
            assert child['job_payload']['pending_user_inputs']
            stopped = http.post(f'/api/runs/{RUN}/jobs/{child["job_id"]}/cancel')
            assert stopped.status_code == 200, stopped.text
        finally:
            model.release.set()
            model.child_release.set()
            wait_idle(http)
        assert len(model.calls) == 2
        final = http.app.state.jobs.latest(RUN)
        assert final['status'] == 'cancelled'
        assert final['steer_status'] == 'discarded'
        assert final['successor_job_id'] is None


@pytest.mark.parametrize('answered_status', [False, True])
def test_restart_preserves_inherited_inbox_but_requires_explicit_recovery(tmp_path, answered_status):
    from studio.agent_runtime import AgentRuntime
    from studio.config import Settings
    from studio.conversation_input import receive
    from studio.jobs import JobManager
    from studio.ledger import Ledger
    from studio.pipeline import Pipeline
    from studio.store import Store
    store = Store(tmp_path)
    store.create_run(RUN, 'Offline restart', 'Synthetic brief')
    receipt = receive(store, RUN, '尚未处理的原话', 'pending')
    store.append_event(RUN, 'job_created', job_id='child-job', request_id='child-request',
                       text='Current child', job_kind='conversation',
                       job_payload={'pending_user_inputs': [receipt['message_id']]})
    store.append_event(RUN, 'job_status', job_id='child-job', status='running')
    if answered_status:
        store.append_event(RUN, 'job_steer_received', job_id='child-job', request_id='status',
                           expected_goal_revision=0, goal_revision=1, text='进度呢', steer_class='status')
        store.append_event(RUN, 'job_steer_answered', job_id='child-job', request_id='status',
                           text='当前仍在执行', classes=['status'])
    runtime = AgentRuntime(store, RUN)
    runtime.create_task('child-job', role='orchestrator', parent_job_id='child-job')
    runtime.transition('child-job', 'running')
    def forbidden_prepare(_):
        raise AssertionError('restart must not replay a model request')
    settings = Settings(tmp_path, 'https://example.invalid')
    jobs = JobManager(store, Pipeline(store), Ledger(store, settings), forbidden_prepare)
    try:
        jobs.initialize()
        recovered = jobs.get(RUN, 'child-job')
        assert recovered['status'] == 'interrupted'
        assert recovered['steer_status'] == 'blocked'
        assert recovered['job_payload']['pending_user_inputs'] == [receipt['message_id']]
        assert runtime.task('child-job')['status'] == 'interrupted'
        assert jobs.active is None
    finally:
        jobs.close()


def test_stop_in_job_finalization_gap_is_cancelled_instead_of_failed(tmp_path):
    model = ScriptedModel([final_turn('固定答复')])
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        jobs = http.app.state.jobs
        original_event = jobs._event
        def stop_at_done(run_id, job_id, kind, **fields):
            row = original_event(run_id, job_id, kind, **fields)
            if kind == 'job_progress' and fields.get('event', {}).get('type') == 'done':
                jobs.cancel(run_id, job_id)
            return row
        jobs._event = stop_at_done
        response = http.post(f'/api/runs/{RUN}/jobs', json={'text': '只讨论当前项目', 'request_id': 'late-stop'})
        assert response.status_code == 200, response.text
        wait_idle(http)
        assert jobs.latest(RUN)['status'] == 'cancelled'
        tasks = http.get(f'/api/runs/{RUN}/agent-runtime').json()['tasks']
        assert tasks[0]['status'] == 'cancelled'


def test_stop_original_job_reaches_active_grandchild_in_the_same_queue(tmp_path):
    class ThreeTurns(BlockingModel):
        def __init__(self):
            super().__init__()
            self.grandchild_entered, self.grandchild_release = threading.Event(), threading.Event()
        def complete(self, **kwargs):
            if not self.calls:
                return super().complete(**kwargs)
            self.calls.append(copy.deepcopy(kwargs))
            if len(self.calls) == 3:
                self.grandchild_entered.set()
                assert self.grandchild_release.wait(8)
            return final_turn('固定后继答复')
    model = ThreeTurns()
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        original = start(http, model)
        try:
            for revision in range(2):
                response = http.post(f'/api/runs/{RUN}/jobs/{original["job_id"]}/steer', json={
                    'text': f'讨论第 {revision + 1} 个问题', 'request_id': f'grand-{revision}',
                    'expected_goal_revision': revision})
                assert response.status_code == 200, response.text
            model.release.set()
            assert model.grandchild_entered.wait(4)
            response = http.post(f'/api/runs/{RUN}/jobs/{original["job_id"]}/cancel')
            assert response.status_code == 200, response.text
            assert response.json()['job_id'] == http.app.state.jobs.latest(RUN)['job_id']
            assert response.json()['status'] == 'cancelling'
        finally:
            model.release.set()
            model.grandchild_release.set()
            wait_idle(http)
        assert http.app.state.jobs.latest(RUN)['status'] == 'cancelled'
        assert len(model.calls) == 3
