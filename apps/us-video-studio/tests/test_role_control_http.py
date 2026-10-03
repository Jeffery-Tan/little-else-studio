"""Exact role control through real local HTTP routes; disposable data only."""
import threading
import time
from types import SimpleNamespace

import pytest

from local_http import TestClient
from studio.agent_sessions import AgentSessions
from studio.server import build_app
from test_server import RUN, make_run, settings_for
from test_jobs import wait_job
from test_revision_scope import env, RUN as REVISION_RUN, QUOTE, patch, plan
from fakes import ScriptedModel, final_turn, tool_turn
from studio.tools.dispatch import Dispatcher


def test_explicit_role_resume_after_status_turn_keeps_scope_and_is_idempotent(env):
    store, pipe, settings, ledger = env
    first = Dispatcher(store, pipe, settings, ScriptedModel([final_turn('Waiting for explicit continuation')]),
                       ledger, REVISION_RUN).dispatch('writing', QUOTE, revision_scope=plan(env))
    assert first.submitted is None
    sessions = AgentSessions(store, REVISION_RUN)
    assert sessions.snapshot(first.session_id)['status'] == 'waiting'
    store.append_event(REVISION_RUN, 'creative_context_selected',
        selection={'intent': 'status', 'format': 'none', 'tags': [], 'case_ids': None})
    model = ScriptedModel([tool_turn('save', 'submit_patch', patch(value='Resumed exact field'))])
    with TestClient(build_app(settings, client_factory=lambda: model)) as http:
        path = f'/api/runs/{REVISION_RUN}/role-sessions/{first.session_id}/resume'
        payload = {'request_id': 'explicit-role-resume', 'expected_task_id': first.task_id,
                   'text': '继续这项未完成的创作任务，保留已保存内容并先核对当前依据。'}
        response = http.post(path, json=payload)
        assert response.status_code == 200, response.text
        job_id = response.json()['job_id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = http.get(f'/api/runs/{REVISION_RUN}/jobs/{job_id}').json()
            if job['status'] in {'succeeded', 'failed', 'cancelled', 'blocked'}:
                break
            threading.Event().wait(.01)
        assert job['status'] == 'succeeded', job['reason']
        retry = http.post(path, json=payload)
        assert retry.status_code == 200 and retry.json()['job_id'] == job_id
    current = store.latest_artifact(REVISION_RUN, 'writing')
    assert current.version == 2 and current.structured['segments'][0]['line'] == 'Resumed exact field'
    assert current.structured['segments'][1]['line'] == 'Keep second line'
    assert pipe.stage_state(REVISION_RUN, 'writing').approval is None
    assert len(model.calls) == 1


def test_stopped_role_resume_is_rejected_before_creating_a_job(env):
    store, _, settings, _ = env
    sessions = AgentSessions(store, REVISION_RUN)
    row = sessions.ensure(role='writer', stage='writing')
    sessions.cancel(row['session_id'])
    with TestClient(build_app(settings, client_factory=lambda: pytest.fail('no model client'))) as http:
        response = http.post(f'/api/runs/{REVISION_RUN}/role-sessions/{row["session_id"]}/resume',
            json={'request_id': 'stopped-role', 'text': '继续'})
        assert response.status_code == 409
    assert not any(e['kind'] == 'job_created' for e in store.read_events(REVISION_RUN))


def test_targeted_messages_and_stop_preserve_the_parent_and_other_roles(tmp_path):
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: pytest.fail('no model'))) as http:
        make_run(http)
        jobs = http.app.state.jobs
        entered, release = threading.Event(), threading.Event()

        def prepare(_rid):
            def run(_text, _emit, check):
                entered.set()
                assert release.wait(5)
                check()
                return SimpleNamespace(text='已读取原任务', stopped_because='done', rounds=1, used_tools=[])
            return run, {}

        job = jobs.submit(RUN, '原任务', 'parent', prepare=prepare)
        assert entered.wait(2)
        sessions = AgentSessions(jobs.store, RUN)
        selected = sessions.ensure(role='director', stage='direction', parent_job_id=job['job_id'])
        other = sessions.ensure(role='writer', stage='writing', parent_job_id=job['job_id'])
        sessions.claim(selected['session_id'], parent_job_id=job['job_id'])
        sessions.claim(other['session_id'], parent_job_id=job['job_id'])
        path = f'/api/runs/{RUN}/jobs/{job["job_id"]}/steer'
        try:
            payload = {'text': '这次开头要更直接，其他内容保留', 'request_id': 'role-message',
                       'expected_goal_revision': 0, 'target_session_id': selected['session_id']}
            response = http.post(path, json=payload)
            assert response.status_code == 200, response.text
            assert response.json()['status'] == 'running'
            assert response.json()['steer_status'] == 'delivered'
            assert not jobs.cancelled.is_set()
            assert len(sessions.pending(selected['session_id'])) == 1
            assert not sessions.pending(other['session_id'])
            assert http.post(path, json=payload).status_code == 200
            assert len(sessions.pending(selected['session_id'])) == 1
            public = http.get(f'/api/runs/{RUN}/role-sessions').json()
            assert payload['text'] not in str(public)
            response = http.post(path, json={'text': '停止', 'request_id': 'role-stop',
                'expected_goal_revision': 1, 'target_session_id': selected['session_id']})
            assert response.status_code == 200, response.text
            assert response.json()['status'] == 'running'
            assert sessions.snapshot(selected['session_id'])['stop_requested']
            assert not sessions.snapshot(other['session_id'])['stop_requested']
            assert not jobs.cancelled.is_set()
        finally:
            sessions.finish(other['session_id'], 'failed', reason='fixture ended')
            release.set()
        finished = wait_job(http, job['job_id'])
        assert finished['status'] == 'succeeded'
        assert not finished['successor_job_id']


def test_a_role_target_from_another_job_is_rejected_without_delivery(tmp_path):
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: pytest.fail('no model'))) as http:
        make_run(http)
        jobs = http.app.state.jobs
        entered, release = threading.Event(), threading.Event()
        def prepare(_rid):
            def run(*_):
                entered.set()
                assert release.wait(5)
                return SimpleNamespace(text='done', stopped_because='done', rounds=1, used_tools=[])
            return run, {}
        job = jobs.submit(RUN, '原任务', 'parent', prepare=prepare)
        assert entered.wait(2)
        sessions = AgentSessions(jobs.store, RUN)
        old = sessions.ensure(role='director', stage='direction', parent_job_id='another-job')
        sessions.claim(old['session_id'])
        try:
            response = http.post(f'/api/runs/{RUN}/jobs/{job["job_id"]}/steer', json={
                'text': '修改开头', 'request_id': 'wrong-role', 'expected_goal_revision': 0,
                'target_session_id': old['session_id']})
            assert response.status_code == 409
            assert not sessions.pending(old['session_id'])
            assert not jobs.cancelled.is_set()
        finally:
            sessions.finish(old['session_id'], 'failed', reason='fixture ended')
            release.set()
        wait_job(http, job['job_id'])
