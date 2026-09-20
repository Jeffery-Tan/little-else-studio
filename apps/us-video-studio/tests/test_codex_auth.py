"""Browser login lifecycle with fake local processes only; no account changes."""
import json
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from fakes import ScriptedModel, final_turn
from studio import codex_auth
from studio.agents.codex_client import CodexUnavailable
from studio.codex_auth import CodexAuth
from studio.config import Settings
from studio.models import PipelineError
from studio.server import build_app


class FakeProcess:
    def __init__(self):
        self.done = threading.Event()
        self.returncode = None
        self.terminated = False

    def wait(self, timeout=None):
        if not self.done.wait(timeout):
            raise subprocess.TimeoutExpired('fake-codex', timeout)
        return self.returncode

    def finish(self, code):
        self.returncode = code
        self.done.set()

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.finish(-15)

    def kill(self):
        self.finish(-9)


class FakeCLI:
    def __init__(self):
        self.output = 'Not logged in'
        self.code = 1
        self.commands = []
        self.processes = []

    def run(self, args, **kwargs):
        self.commands.append((args, kwargs))
        return SimpleNamespace(returncode=self.code, stdout=self.output)

    def spawn(self, args, **kwargs):
        self.commands.append((args, kwargs))
        process = FakeProcess()
        self.processes.append(process)
        return process

    def logged_in(self):
        self.output, self.code = 'Logged in using ChatGPT', 0


@pytest.fixture
def setup():
    cli = FakeCLI()
    jobs = SimpleNamespace(lock=threading.RLock(), active=None)
    auth = CodexAuth(jobs, resolver=lambda: 'fake-codex', runner=cli.run, spawn=cli.spawn)
    yield auth, cli, jobs
    auth.close()


def wait_finished(auth):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with auth.jobs.lock:
            if auth.process is None:
                return
        time.sleep(.005)
    pytest.fail('fake login watcher did not finish')


def test_status_is_read_only_and_creation_has_no_side_effects(setup):
    auth, cli, _ = setup
    assert cli.commands == []
    assert auth.status()['status'] == 'not_logged_in'
    assert cli.commands[0][0] == ['fake-codex', 'login', 'status']
    assert not cli.processes


def test_duplicate_clicks_share_one_login_and_pending_blocks_jobs(setup):
    auth, cli, _ = setup
    first = auth.start()
    assert first['status'] == 'pending' and first['login_active']
    assert auth.start() == first
    assert len(cli.processes) == 1
    with pytest.raises(PipelineError, match='正在授权'):
        auth.require_idle()
    command, kwargs = cli.commands[-1]
    assert command == ['fake-codex', 'login', '-c', 'forced_login_method="chatgpt"']
    assert kwargs['stdout'] == kwargs['stderr'] == subprocess.DEVNULL
    assert kwargs['stdin'] == subprocess.DEVNULL
    assert kwargs['creationflags'] == getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def test_completed_login_probes_status_and_does_not_replace_account(setup):
    auth, cli, _ = setup
    auth.start()
    cli.logged_in()
    cli.processes[0].finish(0)
    wait_finished(auth)
    assert auth.status()['status'] == 'logged_in'
    assert auth.start()['auth_method'] == 'chatgpt'
    assert len(cli.processes) == 1
    auth.require_idle()


def test_existing_api_key_is_never_overwritten_or_returned(setup):
    auth, cli, _ = setup
    cli.output, cli.code = 'Logged in using an API key - fake-private-value', 0
    result = auth.start()
    assert result['status'] == 'failed' and result['auth_method'] == 'api_key'
    assert 'fake-private-value' not in json.dumps(result)
    assert not cli.processes


def test_existing_chatgpt_login_does_not_spawn_login(setup):
    auth, cli, _ = setup
    cli.logged_in()
    assert auth.start()['status'] == 'logged_in'
    assert not cli.processes


def test_active_job_refuses_login_before_checking_or_spawning(setup):
    auth, cli, jobs = setup
    jobs.active = ('run', 'job')
    with pytest.raises(HTTPException) as caught:
        auth.start()
    assert caught.value.status_code == 409
    assert cli.commands == []


def test_cancel_is_attempt_bound_and_does_not_logout(setup):
    auth, cli, _ = setup
    result = auth.start()
    with pytest.raises(HTTPException) as caught:
        auth.cancel('old-attempt')
    assert caught.value.status_code == 409
    assert not cli.processes[0].terminated
    assert auth.cancel(result['attempt_id'])['status'] == 'not_logged_in'
    assert cli.processes[0].terminated
    new = auth.start()
    assert new['attempt_id'] != result['attempt_id']
    with pytest.raises(HTTPException):
        auth.cancel(result['attempt_id'])
    assert not cli.processes[1].terminated
    assert all('logout' not in args for args, _ in cli.commands)


def test_timeout_can_be_retried(setup):
    auth, cli, _ = setup
    auth.timeout = .01
    auth.start()
    wait_finished(auth)
    assert auth.status()['status'] == 'failed'
    assert '超时' in auth.status()['message']
    assert cli.processes[0].terminated
    auth.timeout = 600
    assert auth.start()['status'] == 'pending'


def test_failure_can_be_retried_and_raw_output_is_not_returned(setup):
    auth, cli, _ = setup
    auth.start()
    cli.processes[0].finish(1)
    wait_finished(auth)
    assert auth.status()['status'] == 'failed'
    assert auth.start()['status'] == 'pending'


def test_missing_cli_and_unrecognised_output_do_not_start_login(setup):
    auth, cli, _ = setup
    def missing():
        raise CodexUnavailable('secret internal detail')
    auth.resolve = missing
    assert auth.start()['status'] == 'unavailable'
    assert not cli.processes
    auth.resolve = lambda: 'fake-codex'
    cli.output = 'private invalid diagnostics'
    result = auth.start()
    assert result['status'] == 'failed'
    assert 'private' not in str(result)
    assert not cli.processes


def test_shutdown_stops_pending_login(setup):
    auth, cli, _ = setup
    auth.start()
    auth.close()
    assert cli.processes[0].terminated
    with pytest.raises(HTTPException):
        auth.start()


def test_environment_removes_api_credentials_and_desktop_identity(monkeypatch):
    monkeypatch.setenv('CODEX_HOME', 'test-home')
    monkeypatch.setenv('CODEX_CA_CERTIFICATE', 'test-ca.pem')
    for key in ('CODEX_THREAD_ID', 'OPENAI_API_KEY', 'CODEX_ACCESS_TOKEN'):
        monkeypatch.setenv(key, 'private')
    env = codex_auth._environment()
    assert env['CODEX_HOME'] == 'test-home'
    assert env['CODEX_CA_CERTIFICATE'] == 'test-ca.pem'
    assert all(key not in env for key in ('CODEX_THREAD_ID', 'OPENAI_API_KEY', 'CODEX_ACCESS_TOKEN'))


@pytest.fixture
def http(tmp_path, monkeypatch):
    cli = FakeCLI()
    monkeypatch.setattr(codex_auth, 'resolve_codex_command', lambda: 'fake-codex')
    monkeypatch.setattr(codex_auth.subprocess, 'run', cli.run)
    monkeypatch.setattr(codex_auth.subprocess, 'Popen', cli.spawn)
    app = build_app(Settings(data_root=tmp_path, base_url='https://example.invalid'),
                    client_factory=lambda: ScriptedModel([final_turn('done')]))
    with TestClient(app) as client:
        yield client, app, cli


def test_http_consent_json_and_cross_origin_guards(http):
    client, _, cli = http
    assert client.post('/api/connections/codex/login', json={'consent': False}).status_code == 400
    assert client.post('/api/connections/codex/login', json={'consent': 'true'}).status_code == 422
    assert client.post('/api/connections/codex/login', json={'consent': True},
                       headers={'Origin': 'https://other.invalid'}).status_code == 403
    assert client.post('/api/connections/codex/login', content='{}').status_code == 415
    assert client.post('/api/connections/codex/login/cancel', content='{}').status_code == 415
    assert client.get('/api/connections/codex/status', headers={'Origin': 'null'}).status_code == 403
    assert not cli.processes
    response = client.get('/api/connections/codex/status')
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['status'] == 'not_logged_in'


def test_http_login_blocks_new_jobs_without_recording_a_model_call(http):
    client, app, cli = http
    client.post('/api/runs', json={'run_id': 'test-run', 'title': 'test', 'brief': 'test'})
    login = client.post('/api/connections/codex/login', json={'consent': True})
    assert login.status_code == 200
    with pytest.raises(PipelineError, match='正在授权'):
        app.state.jobs.submit('test-run', 'hello', 'test-request')
    assert app.state.jobs.ledger.entries() == []
    cancelled = client.post('/api/connections/codex/login/cancel',
                            json={'attempt_id': login.json()['attempt_id']})
    assert cancelled.status_code == 200 and not cancelled.json()['login_active']
    assert cli.processes[0].terminated
