"""Real HTTP, saved role output and trusted input boundaries; no live models."""
import threading
import time

import pytest

from fakes import ScriptedModel, final_turn, tool_turn, fixture_task_interpretation
from local_http import TestClient
from studio.agent_sessions import AgentSessions, SessionJournal
from studio.conversation_input import receive, accept, start_execution, record_task_interpretation
from studio.models import PipelineError
from studio.server import build_app
from studio.tools.dispatch import Dispatcher
from studio.revision_scope import plan_revision
from test_revision_scope import env, RUN, QUOTE, patch, plan, plan_args


def wait_job(http, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = http.get(f'/api/runs/{RUN}/jobs/{job_id}')
        assert response.status_code == 200, response.text
        job = response.json()
        if job['status'] in {'succeeded', 'failed', 'blocked', 'cancelled', 'interrupted'}:
            return job
        threading.Event().wait(.01)
    raise AssertionError('Offline role task did not settle')


def revision_model(store, text, value):
    model = ScriptedModel([
        tool_turn('prepare', 'prepare_creative_context', {
            'intent': 'edit', 'format': 'video', 'deliverable': 'revision',
            'task_interpretation': fixture_task_interpretation(text, intent='edit', deliverable='revision')}),
        tool_turn('scope', 'plan_revision', plan_args(store, quote=text)),
        tool_turn('assign', 'dispatch', {'stage': 'writing', 'brief': text}),
        tool_turn('save', 'submit_patch', patch(value=value)),
        final_turn('本轮准确修改已保存。'),
    ], legacy_task_contracts=False)
    complete = model.complete

    def returned_plan(**kwargs):
        turn = complete(**kwargs)
        for call in turn.tool_calls:
            if call.name == 'dispatch':
                scope = next(e['scope'] for e in reversed(store.read_events(RUN))
                             if e['kind'] == 'agent_revision_planned')
                call.input['plan_id'] = scope['plan_id']
                for block in turn.raw_blocks:
                    if block.get('id') == call.id:
                        block['input'] = call.input
        return turn

    model.complete = returned_plan
    return model


def test_late_targeted_input_is_durably_rejected_then_new_feedback_uses_saved_baseline(env, monkeypatch):
    store, pipe, settings, _ = env
    saved, release = threading.Event(), threading.Event()
    original = SessionJournal.tool_result

    def paused_after_commit(self, call, output, is_error, skipped):
        original(self, call, output, is_error, skipped)
        if call.name == 'submit_patch' and not is_error:
            saved.set()
            assert release.wait(10)

    monkeypatch.setattr(SessionJournal, 'tool_result', paused_after_commit)
    model = revision_model(store, QUOTE, 'Saved before the late input')
    active = {'model': model}
    with TestClient(build_app(settings, client_factory=lambda: active['model'])) as http:
        response = http.post(f'/api/runs/{RUN}/jobs', json={'text': QUOTE, 'request_id': 'original-revision'})
        assert response.status_code == 200, response.text
        job = response.json()
        try:
            assert saved.wait(5)
            sessions = AgentSessions(store, RUN)
            role = sessions.sessions()[0]
            assert role['status'] == 'running' and role['input_closed']
            assert role['artifact_receipt']['version'] == 2
            text = '只把第一句改成新的结论，其他内容保留'
            payload = {'text': text, 'request_id': 'late-role-input', 'expected_goal_revision': 0,
                       'target_session_id': role['session_id']}
            path = f'/api/runs/{RUN}/jobs/{job["job_id"]}/steer'
            first = http.post(path, json=payload)
            assert first.status_code == 409 and 'session_closed' in first.text
            assert '第 2 版已保存' in first.text and '新稿' in first.text
            assert http.post(path, json=payload).json() == first.json()
            receipt = http.get(f'/api/runs/{RUN}/request-receipts/late-role-input',
                params={'kind': 'steer', 'job_id': job['job_id']}).json()
            assert receipt['rejected'] is True and receipt['received'] is False
            assert receipt['code'] == 'session_closed'
            assert not sessions.pending(role['session_id'])
            assert len([e for e in store.read_events(RUN) if e['kind'] == 'user_message_received'
                        and e['request_id'] == 'late-role-input']) == 1
            assert not any(e['kind'] == 'job_steer_delivered' for e in store.read_events(RUN))
        finally:
            release.set()
        finished = wait_job(http, job['job_id'])
        assert finished['status'] == 'succeeded' and not finished.get('successor_job_id')
        assert len(model.calls) == 5 and text not in model.requests_text()
        assert sessions.snapshot(role['session_id'])['status'] == 'completed'
        closed_resume = http.post(f'/api/runs/{RUN}/role-sessions/{role["session_id"]}/resume', json={
            'text': text, 'request_id': 'closed-role-resume', 'expected_task_id': role['task_id']})
        assert closed_resume.status_code == 409 and 'session_new_baseline' in closed_resume.text
        assert len(model.calls) == 5

        # A genuinely new correction freezes v2, rather than replaying the old v1 plan.
        monkeypatch.setattr(SessionJournal, 'tool_result', original)
        active['model'] = revision_model(store, text, 'New feedback against the saved v2')
        response = http.post(f'/api/runs/{RUN}/jobs', json={'text': text, 'request_id': 'new-baseline-feedback'})
        assert response.status_code == 200, response.text
        revised = wait_job(http, response.json()['job_id'])
        assert revised['status'] == 'succeeded', revised
        scopes = [e['scope'] for e in store.read_events(RUN) if e['kind'] == 'agent_revision_planned']
        assert [scope['version'] for scope in scopes] == [1, 2]
        assert store.latest_artifact(RUN, 'writing').version == 3
        assert store.latest_artifact(RUN, 'writing').structured['segments'][0]['line'] == 'New feedback against the saved v2'
        assert store.latest_artifact(RUN, 'writing').structured['segments'][1]['line'] == 'Keep second line'
        assert pipe.stage_state(RUN, 'writing').approval is None


def test_legacy_saved_output_with_pending_input_cannot_return_a_false_cached_resume(env):
    store, pipe, settings, ledger = env
    first = Dispatcher(store, pipe, settings, ScriptedModel([tool_turn('save', 'submit_patch', patch())]),
                       ledger, RUN).dispatch('writing', QUOTE, revision_scope=plan(env))
    sessions = AgentSessions(store, RUN)
    text = '此前已经收到但尚未处理的新补充'
    pending = receive(store, RUN, text, 'legacy-pending-input')
    # This is the pre-fix journal shape: accepted after output commit, never consumed.
    sessions.append(first.session_id, 'inbox_received', message_id=pending['message_id'],
                    source_message_id=pending['message_id'], text=text)
    row = sessions.snapshot(first.session_id)
    assert row['input_closed'] and row['pending_message_count'] == 1
    assert '尚未处理' in row['pending_input_notice'] and '第 2 版已保存' in row['pending_input_notice']
    with TestClient(build_app(settings, client_factory=lambda: pytest.fail('cached output must not start a model'))) as http:
        response = http.post(f'/api/runs/{RUN}/role-sessions/{first.session_id}/resume', json={
            'text': '请处理此前补充', 'request_id': 'legacy-pending-resume', 'expected_task_id': first.task_id})
        assert response.status_code == 409 and 'session_new_baseline' in response.text
    assert len(sessions.pending(first.session_id)) == 1
    assert store.latest_artifact(RUN, 'writing').version == 2
    assert not any(e['kind'] == 'job_created' for e in store.read_events(RUN))


@pytest.mark.parametrize('text', ['停止', '现在进度怎么样？', '只讨论为什么失败，不要改稿'])
def test_readonly_role_resume_preserves_real_input_without_a_job_attempt_or_model(env, text):
    store, pipe, settings, ledger = env
    first = Dispatcher(store, pipe, settings, ScriptedModel([final_turn('Waiting for explicit continuation')]),
                       ledger, RUN).dispatch('writing', QUOTE, revision_scope=plan(env))
    attempts = [e for e in store.read_events(RUN) if e['kind'] == 'attempt_started']
    with TestClient(build_app(settings, client_factory=lambda: pytest.fail('read-only resume cannot freeze a model'))) as http:
        response = http.post(f'/api/runs/{RUN}/role-sessions/{first.session_id}/resume', json={
            'text': text, 'request_id': 'readonly-resume', 'expected_task_id': first.task_id})
        assert response.status_code == 409 and 'session_input_scope' in response.text
        assert '普通对话' in response.text and '未启动' in response.text
    events = store.read_events(RUN)
    assert [e for e in events if e['kind'] == 'attempt_started'] == attempts
    assert not any(e['kind'] == 'job_created' for e in events)
    assert any(e['kind'] == 'user_message_received' and e['text'] == text for e in events)
    assert AgentSessions(store, RUN).snapshot(first.session_id)['turn'] == 1


def test_role_resume_honors_trusted_discussion_interpretation_without_keyword_match(env):
    store, pipe, settings, ledger = env
    first = Dispatcher(store, pipe, settings, ScriptedModel([final_turn('Waiting')]),
                       ledger, RUN).dispatch('writing', QUOTE, revision_scope=plan(env))
    text = '我想弄清这份表达背后的判断依据。'
    receipt = receive(store, RUN, text, 'semantic-discussion-resume')
    accept(store, RUN, receipt)
    start_execution(store, RUN, receipt)
    interpretation = fixture_task_interpretation(text, intent='status', format='none', deliverable='follow_up')
    interpretation['task']['mode'] = 'discuss'
    record_task_interpretation(store, RUN, receipt, interpretation)
    with TestClient(build_app(settings, client_factory=lambda: pytest.fail('trusted discussion cannot run the role'))) as http:
        response = http.post(f'/api/runs/{RUN}/role-sessions/{first.session_id}/resume', json={
            'text': text, 'request_id': receipt['request_id'], 'expected_task_id': first.task_id})
        assert response.status_code == 409 and 'session_input_scope' in response.text
    assert AgentSessions(store, RUN).snapshot(first.session_id)['turn'] == 1
    assert not any(e['kind'] == 'job_created' for e in store.read_events(RUN))


def test_internal_resume_rechecks_current_input_before_closing_old_attempt(env):
    store, pipe, settings, ledger = env
    dispatcher = Dispatcher(store, pipe, settings, ScriptedModel([final_turn('Waiting')]), ledger, RUN)
    first = dispatcher.dispatch('writing', QUOTE, revision_scope=plan(env))
    receipt = receive(store, RUN, '停止', 'internal-readonly-resume')
    with pytest.raises(PipelineError, match='session_input_scope'):
        dispatcher.resume_session(first.session_id, user_input_id=receipt['message_id'])
    assert AgentSessions(store, RUN).snapshot(first.session_id)['turn'] == 1
    assert len(dispatcher.client.calls) == 1


def test_negated_stop_with_explicit_local_continuation_still_resumes(env):
    store, pipe, settings, ledger = env
    first = Dispatcher(store, pipe, settings, ScriptedModel([final_turn('Waiting')]),
                       ledger, RUN).dispatch('writing', QUOTE, revision_scope=plan(env))
    text = '不要停止，请继续只把第一句改得自然一点，其他内容保留'
    receipt = receive(store, RUN, text, 'negated-stop-resume')
    accept(store, RUN, receipt)
    start_execution(store, RUN, receipt)
    record_task_interpretation(store, RUN, receipt,
        fixture_task_interpretation(text, intent='edit', deliverable='revision'))
    # The producer has already scoped this actual input to the same baseline
    # and field; the old resume endpoint itself contributes no such meaning.
    store.append_event(RUN, 'agent_turn_scope_started', turn_id='interpreted-resume-turn',
                       user_input_id=receipt['message_id'])
    plan_revision(store, pipe, RUN, plan_args(store, quote=text),
                  turn_id='interpreted-resume-turn', user_message=text)
    model = ScriptedModel([tool_turn('save', 'submit_patch', patch(value='Continued, not cancelled'))],
                          legacy_task_contracts=False)
    with TestClient(build_app(settings, client_factory=lambda: model)) as http:
        response = http.post(f'/api/runs/{RUN}/role-sessions/{first.session_id}/resume', json={
            'text': text, 'request_id': receipt['request_id'], 'expected_task_id': first.task_id})
        assert response.status_code == 200, response.text
        job = wait_job(http, response.json()['job_id'])
        assert job['status'] == 'succeeded', job
    assert len(model.calls) == 1
    assert text in model.requests_text()
    assert store.latest_artifact(RUN, 'writing').structured['segments'][0]['line'] == 'Continued, not cancelled'


@pytest.mark.parametrize('text,typed', [
    ('我想弄清这份表达背后的判断依据。', False),
    ('帮我把第一句改得更自然一点', False),
    ('帮我把第一句改得更自然一点', True),
])
def test_uninterpreted_custom_resume_cannot_use_endpoint_or_button_marker_as_permission(env, text, typed):
    store, pipe, settings, ledger = env
    first = Dispatcher(store, pipe, settings, ScriptedModel([final_turn('Waiting')]),
                       ledger, RUN).dispatch('writing', QUOTE, revision_scope=plan(env))
    payload = {'text': text, 'request_id': 'custom-resume', 'expected_task_id': first.task_id}
    if typed:
        payload['resume_action'] = 'continue'
    attempts = [e for e in store.read_events(RUN) if e['kind'] == 'attempt_started']
    with TestClient(build_app(settings, client_factory=lambda: pytest.fail('uninterpreted text cannot run'))) as http:
        path = f'/api/runs/{RUN}/role-sessions/{first.session_id}/resume'
        response = http.post(path, json=payload)
        assert response.status_code == 409 and 'session_input_scope' in response.text
        assert '普通对话' in response.text and '原话已保存' in response.text
        assert http.post(path, json=payload).json() == response.json()
    events = store.read_events(RUN)
    assert len([e for e in events if e['kind'] == 'user_message_received' and e['text'] == text]) == 1
    assert not any(e['kind'] in {'job_created', 'role_resume_action_bound'} for e in events)
    assert [e for e in events if e['kind'] == 'attempt_started'] == attempts
    assert AgentSessions(store, RUN).snapshot(first.session_id)['turn'] == 1


@pytest.mark.parametrize('task_binding', [None, 'another-attempt'])
def test_fixed_resume_command_requires_accurate_task_binding(env, task_binding):
    store, pipe, settings, ledger = env
    first = Dispatcher(store, pipe, settings, ScriptedModel([final_turn('Waiting')]),
                       ledger, RUN).dispatch('writing', QUOTE, revision_scope=plan(env))
    command = AgentSessions(store, RUN).snapshot(first.session_id)['resume_command']
    with TestClient(build_app(settings, client_factory=lambda: pytest.fail('unbound button cannot run'))) as http:
        response = http.post(f'/api/runs/{RUN}/role-sessions/{first.session_id}/resume', json={
            **command, 'request_id': 'unbound-button', 'expected_task_id': task_binding})
        assert response.status_code == 409
    assert not any(e['kind'] in {'job_created', 'role_resume_action_bound'} for e in store.read_events(RUN))
    assert AgentSessions(store, RUN).snapshot(first.session_id)['turn'] == 1


def test_fixed_text_without_button_action_or_interpretation_is_not_a_resume_decision(env):
    store, pipe, settings, ledger = env
    first = Dispatcher(store, pipe, settings, ScriptedModel([final_turn('Waiting')]),
                       ledger, RUN).dispatch('writing', QUOTE, revision_scope=plan(env))
    command = AgentSessions(store, RUN).snapshot(first.session_id)['resume_command']
    with TestClient(build_app(settings, client_factory=lambda: pytest.fail('text is not a UI action'))) as http:
        response = http.post(f'/api/runs/{RUN}/role-sessions/{first.session_id}/resume', json={
            'text': command['text'], 'request_id': 'untyped-button', 'expected_task_id': first.task_id})
        assert response.status_code == 409 and 'session_input_scope' in response.text
    assert not any(e['kind'] == 'job_created' for e in store.read_events(RUN))
