"""Real producer/HTTP regressions for new inputs and interrupted saved output."""
import threading
from types import SimpleNamespace

import pytest

from fakes import ScriptedModel, final_turn, tool_turn, fixture_task_interpretation
from local_http import TestClient
from studio.agent_sessions import AgentSessions, ROLE_RESUME_TEXT
from studio.agents.orchestrator import Orchestrator
from studio.conversation_input import receive, accept, start_execution, record_task_interpretation
from studio.models import PipelineError
from studio.server import build_app
from studio.tools.dispatch import Dispatcher
from test_role_input_boundaries import wait_job
from test_revision_scope import env, RUN, QUOTE, plan, patch, plan_args, revision_meaning


def prepare(text, goal, intent, *, path=None):
    if intent == 'edit' and goal == 'revision':
        assert path is not None, 'Synthetic natural revisions must declare their exact known field'
        meaning = revision_meaning(text, path=path)
    else:
        meaning = fixture_task_interpretation(text, intent=intent, deliverable=goal)
    return tool_turn('route', 'prepare_creative_context', {
        'intent': intent, 'format': 'video', 'deliverable': goal,
        'task_interpretation': meaning})


@pytest.mark.parametrize('text,goal,intent,path', [
    ('这次重新构思整个视频方案，全文重写。', 'writing', 'new', None),
    ('只把第二句改得自然一点，其他内容保留。', 'revision', 'edit', ['segments', 1, 'line']),
])
def test_new_producer_task_cannot_borrow_old_local_scope_without_exact_proof(env, text, goal, intent, path):
    store, pipe, settings, ledger = env
    first = Dispatcher(store, pipe, settings, ScriptedModel([final_turn('Waiting')]), ledger, RUN).dispatch(
        'writing', QUOTE, revision_scope=plan(env))
    before = store.latest_artifact(RUN, 'writing')
    model = ScriptedModel([
        prepare(text, goal, intent, path=path),
        tool_turn('resume', 'resume_role', {'session_id': first.session_id, 'quote': text}),
        final_turn('本轮新稿已经完成。'), final_turn('本轮新稿已经完成。'),
    ], legacy_task_contracts=False)
    with TestClient(build_app(settings, client_factory=lambda: model)) as http:
        response = http.post(f'/api/runs/{RUN}/jobs', json={'text': text, 'request_id': 'new-task-old-scope'})
        assert response.status_code == 200, response.text
        result = wait_job(http, response.json()['job_id'])
    assert result['status'] == 'blocked' and result['deliverable']['status'] == 'incomplete', result
    assert store.latest_artifact(RUN, 'writing') == before
    assert AgentSessions(store, RUN).snapshot(first.session_id)['turn'] == 1
    assert 'session_input_scope' in model.requests_text()


@pytest.mark.parametrize('same_field', [True, False])
def test_fresh_producer_revision_plan_must_match_old_base_and_entire_field_set(env, same_field):
    store, pipe, settings, ledger = env
    first = Dispatcher(store, pipe, settings, ScriptedModel([final_turn('Waiting')]), ledger, RUN).dispatch(
        'writing', QUOTE, revision_scope=plan(env))
    text = '只把第一句改得自然一点，其他内容保留。' if same_field else '只把第二句改得自然一点，其他内容保留。'
    path = ['segments', 0 if same_field else 1, 'line']
    turns = [prepare(text, 'revision', 'edit', path=path),
             tool_turn('plan', 'plan_revision', plan_args(store, quote=text, path=path)),
             tool_turn('resume', 'resume_role', {'session_id': first.session_id, 'quote': text})]
    if same_field:
        turns.append(tool_turn('save', 'submit_patch', patch(value='New exact input was handled')))
    turns.extend([final_turn('处理已完成。'), final_turn('处理已完成。')])
    model = ScriptedModel(turns, legacy_task_contracts=False)
    result = Orchestrator(store, pipe, settings, model, ledger, RUN).send(text)
    row = AgentSessions(store, RUN).snapshot(first.session_id)
    if same_field:
        assert result.deliverable['status'] == 'complete', result
        assert store.latest_artifact(RUN, 'writing').structured['segments'][0]['line'] == 'New exact input was handled'
        assert store.latest_artifact(RUN, 'writing').structured['segments'][1]['line'] == 'Keep second line'
        assert row['turn'] == 2 and text in model.requests_text()
    else:
        assert result.deliverable['status'] == 'incomplete'
        assert store.latest_artifact(RUN, 'writing').version == 1 and row['turn'] == 1
        assert '本轮修订计划与旧角色' in model.requests_text()


@pytest.mark.parametrize('path,version,allowed', [
    (['segments', 0, 'line'], 1, True),
    (['segments', 1, 'line'], 1, False),
    (['segments', 0, 'line'], 2, False),
])
def test_trusted_feedback_resume_requires_exact_old_field_and_base(env, path, version, allowed):
    store, pipe, settings, ledger = env
    model = ScriptedModel([final_turn('Waiting'), tool_turn('save', 'submit_patch', patch())], legacy_task_contracts=False)
    dispatcher = Dispatcher(store, pipe, settings, model, ledger, RUN)
    first = dispatcher.dispatch('writing', QUOTE, revision_scope=plan(env))
    base = store.latest_artifact(RUN, 'writing')
    text = '只调整选中的这一句，其他内容保留。'
    target = {'kind': 'writing', 'run_id': RUN, 'stage': 'writing', 'version': version,
              'artifact_sha256': base.sha256, 'path': path, 'quote': base.structured['segments'][path[1]]['line']}
    receipt = receive(store, RUN, text, 'selected-resume', feedback_target=target)
    accept(store, RUN, receipt)
    start_execution(store, RUN, receipt)
    record_task_interpretation(store, RUN, receipt, fixture_task_interpretation(text, intent='edit', deliverable='revision'))
    if allowed:
        outcome = dispatcher.resume_session(first.session_id, user_input_id=receipt['message_id'])
        assert outcome.submitted == 'writing-v2'
        assert len(model.calls) == 2
    else:
        with pytest.raises(PipelineError):
            dispatcher.resume_session(first.session_id, user_input_id=receipt['message_id'])
        assert len(model.calls) == 1 and store.latest_artifact(RUN, 'writing') == base


@pytest.mark.parametrize('typed', [False, True])
def test_historical_crash_after_save_projects_receipt_without_rewriting_history(env, typed):
    store, pipe, settings, ledger = env
    dispatcher = Dispatcher(store, pipe, settings, ScriptedModel([tool_turn('save', 'submit_patch', patch())]), ledger, RUN)
    first = dispatcher.dispatch('writing', QUOTE, revision_scope=plan(env))
    sessions = AgentSessions(store, RUN)
    historical = sessions._read(first.session_id)
    historical.pop('input_closed', None)
    historical.update(artifact_receipt=None, status='failed', reason='Old crash after output was written')
    sessions._save(historical)
    pending = receive(store, RUN, '此前收到但未处理的新补充', 'historical-crash-pending')
    sessions.append(first.session_id, 'inbox_received', message_id=pending['message_id'],
                    source_message_id=pending['message_id'], text=pending['text'])
    events = store.read_events(RUN)
    snapshot = sessions.snapshot(first.session_id)
    assert snapshot['input_closed'] and snapshot['resume_command'] is None
    assert snapshot['artifact_receipt'] == {'stage': 'writing', 'version': 2,
                                          'sha256': store.latest_artifact(RUN, 'writing').sha256}
    assert '第 2 版已保存' in snapshot['pending_input_notice'] and '尚未处理' in snapshot['pending_input_notice']
    assert sessions._read(first.session_id)['artifact_receipt'] is None
    assert store.read_events(RUN) == events
    with TestClient(build_app(settings, client_factory=lambda: pytest.fail('saved legacy output cannot start a new job'))) as http:
        payload = {'text': ROLE_RESUME_TEXT if typed else '请继续处理此前补充',
                   'request_id': 'legacy-saved-resume', 'expected_task_id': first.task_id}
        if typed:
            payload['resume_action'] = 'continue'
        response = http.post(f'/api/runs/{RUN}/role-sessions/{first.session_id}/resume', json=payload)
        assert response.status_code == 409 and 'session_new_baseline' in response.text
    assert not any(e['kind'] == 'job_created' for e in store.read_events(RUN))
    assert len(sessions.pending(first.session_id)) == 1


def test_ui_late_target_after_role_completed_preserves_receipt_and_rejection_identity(env):
    store, _, settings, _ = env
    entered, release = threading.Event(), threading.Event()

    def prepare_runner(_):
        def run(*_):
            entered.set()
            assert release.wait(10)
            return SimpleNamespace(stopped_because='done', text='Parent ended', rounds=0, used_tools=[])
        return run, {}

    with TestClient(build_app(settings, client_factory=lambda: pytest.fail('late role input cannot call a model'))) as http:
        jobs = http.app.state.jobs
        job = jobs.submit(RUN, 'Original parent task', 'still-running-parent', prepare=prepare_runner)
        assert entered.wait(2)
        sessions = AgentSessions(store, RUN)
        role = sessions.ensure(role='director', stage='direction', parent_job_id=job['job_id'])
        sessions.claim(role['session_id'])
        sessions.finish(role['session_id'], 'completed')
        payload = {'request_id': 'closed-role-input', 'text': '开头更直接，其他内容保留',
                   'expected_goal_revision': 0, 'target_session_id': role['session_id']}
        path = f'/api/runs/{RUN}/jobs/{job["job_id"]}/steer'
        try:
            response = http.post(path, json=payload)
            assert response.status_code == 409 and 'session_closed' in response.text
            assert http.post(path, json=payload).json() == response.json()
            changed = http.post(path, json={**payload, 'text': 'Changed text cannot reuse this identity'})
            assert changed.status_code == 409 and 'request_conflict' in changed.text
            receipt = http.get(f'/api/runs/{RUN}/request-receipts/closed-role-input',
                params={'kind': 'steer', 'job_id': job['job_id']}).json()
            assert receipt['rejected'] is True and receipt['received'] is False
            assert len([e for e in store.read_events(RUN) if e['kind'] == 'user_message_received'
                        and e['request_id'] == payload['request_id']]) == 1
            assert not sessions.pending(role['session_id']) and not jobs.cancelled.is_set()
            assert not any(e['kind'] == 'job_steer_delivered' for e in store.read_events(RUN))
        finally:
            release.set()
        finished = wait_job(http, job['job_id'])
        assert finished['status'] == 'succeeded', finished
