"""Real local RPC pipes and a synthetic child; no Codex or provider invocation."""
import base64
import io
import json
import queue
import sys
import time

from PIL import Image
import pytest

from studio.agents import codex_image as adapter
from studio.agents import codex_client as transport
from studio.agents.client import JobCancelled, ModelError


CHILD = r'''
import json, sys, threading, time
from pathlib import Path
scenario, logname, encoded, config_json = sys.argv[1:]
log = Path(logname)
lock = threading.Lock()
def send(value):
    with lock:
        print(json.dumps(value), flush=True)
def event(method, **params):
    send({'method':method, 'params':{'threadId':'thread-image', 'turnId':'turn-image', **params}})
def picture():
    event('item/completed', item={'id':'one-image', 'type':'imageGeneration',
          'status':'completed', 'result':encoded, 'savedPath':'untrusted-path'})
def stream():
    while True:
        event('codex/event', message='synthetic progress')
        time.sleep(0.01)
for line in sys.stdin:
    msg = json.loads(line)
    method, params = msg.get('method'), msg.get('params', {})
    with log.open('a', encoding='utf-8') as out:
        out.write(json.dumps({'method':method, 'threadId':params.get('threadId'),
                             'turnId':params.get('turnId')})+'\n')
    if method == 'initialize':
        send({'id':msg['id'], 'result':{}})
    elif method == 'config/read':
        send({'id':msg['id'], 'result':{'config':json.loads(config_json)}})
    elif method == 'thread/start':
        send({'id':msg['id'], 'result':{'sandbox':{'type':'readOnly', 'networkAccess':False},
              'approvalPolicy':'never', 'modelProvider':'openai', 'model':'fixture-model',
              'cwd':params['cwd'], 'runtimeWorkspaceRoots':[params['cwd']], 'instructionSources':[],
              'thread':{'id':'thread-image', 'ephemeral':True, 'environments':[], 'turns':[]}}})
    elif method == 'turn/start':
        if scenario == 'missing-start-ack':
            continue
        send({'id':msg['id'], 'result':{'turn':{'id':'turn-image'}}})
        if scenario == 'continuous':
            threading.Thread(target=stream, daemon=True).start()
        if scenario == 'saved-before-stop':
            picture()
    elif method == 'turn/interrupt':
        if scenario in {'no-interrupt-ack', 'continuous'}:
            continue
        send({'id':msg['id'], 'result':{}})
        if scenario == 'late-image':
            picture()
            event('thread/tokenUsage/updated', tokenUsage={'total':{'inputTokens':101,'outputTokens':35}})
            event('turn/completed', turn={'id':'turn-image', 'status':'completed'})
        elif scenario == 'late-failed-image':
            event('item/completed', item={'id':'one-image', 'type':'imageGeneration',
                  'status':'failed', 'failure':{'type':'synthetic-failure'}, 'result':''})
        elif scenario == 'foreign-completion':
            send({'method':'turn/completed', 'params':{'threadId':'other-thread',
                  'turnId':'turn-image', 'turn':{'id':'turn-image', 'status':'interrupted'}}})
        else:
            event('turn/completed', turn={'id':'turn-image', 'status':'interrupted'})
'''


@pytest.fixture
def rpc(monkeypatch, tmp_path):
    child = tmp_path / 'synthetic_image_rpc.py'
    child.write_text(CHILD, encoding='utf-8')
    log = tmp_path / 'rpc.jsonl'
    payload = io.BytesIO()
    Image.new('RGB', (3, 2), 'blue').save(payload, format='PNG')
    encoded = base64.b64encode(payload.getvalue()).decode()
    config = {'features':{**dict.fromkeys(transport.DISABLED_FEATURES, False),
                          'image_generation':True, 'code_mode_host':True,
                          'skip_host_skill_discovery':True},
              'web_search':'disabled', 'sandbox_mode':'read-only', 'approval_policy':'never',
              'model_provider':'openai', 'forced_login_method':'chatgpt', 'project_doc_max_bytes':0,
              'developer_instructions':'', 'agents':{'enabled':False}}
    real_popen = transport.subprocess.Popen
    session_class = adapter._ImageSession
    sessions, state = [], {'scenario':'interrupted', 'started_at':None, 'stopped_at':None}

    def popen(argv, **kwargs):
        assert argv[:3] == ['synthetic-rpc', 'app-server', '--stdio']
        return real_popen([sys.executable, '-u', str(child), state['scenario'], str(log),
                           encoded, json.dumps(config)], **kwargs)

    def factory(*args):
        session = session_class(*args)
        sessions.append(session)
        return session

    def entries():
        return [json.loads(line) for line in log.read_text('utf-8').splitlines()] if log.exists() else []

    def check():
        if state['scenario']=='pre-turn-stop' and any(row['method']=='thread/start' for row in entries()):
            state['stopped_at'] = state['stopped_at'] or time.monotonic()
            raise JobCancelled()
        if any(row['method']=='turn/start' for row in entries()):
            state['started_at'] = state['started_at'] or time.monotonic()
            if time.monotonic()-state['started_at'] >= 0.15:
                state['stopped_at'] = state['stopped_at'] or time.monotonic()
                raise JobCancelled()

    monkeypatch.setattr(transport.subprocess, 'Popen', popen)
    monkeypatch.setattr(adapter, '_ImageSession', factory)
    client = adapter.CodexImageClient(command='synthetic-rpc', timeout=30, check_cancel=check)
    client._capability, client._mcp_names = {'available':True}, ()
    return client, state, sessions, entries, payload.getvalue()


@pytest.mark.parametrize('scenario', ['interrupted', 'no-interrupt-ack', 'continuous',
                                     'foreign-completion', 'late-image', 'saved-before-stop', 'late-failed-image'])
def test_stop_bounded_over_real_silent_and_active_rpc_pipes(rpc, tmp_path, scenario):
    client, state, sessions, entries, png = rpc
    state['scenario'] = scenario
    result = client.generate(adapter.ImageRequest('synthetic one image', tmp_path/'output'))
    elapsed = time.monotonic()-state['stopped_at']
    assert elapsed < adapter.CANCEL_SECONDS + 0.25
    assert result.status == 'unknown' and result.failure_code == 'cancelled'
    assert result.usage_status == 'unknown' and result.provider_invoked
    assert result.thread_id == 'thread-image' and result.turn_id == 'turn-image'
    rows = entries()
    assert sum(row['method']=='turn/start' for row in rows) == 1
    assert [row for row in rows if row['method']=='turn/interrupt'] == [
        {'method':'turn/interrupt','threadId':'thread-image','turnId':'turn-image'}]
    assert sessions[0].process.poll() is not None and not sessions[0].reader.is_alive()
    assert sessions[0].process.stdout.closed and sessions[0].process.stdin.closed
    if scenario in {'late-image','saved-before-stop'}:
        assert len(result.images) == 1 and result.observed_image_calls == 1
        assert result.images[0].path.read_bytes() == png
        if scenario == 'late-image':
            assert (result.usage.input_tokens, result.usage.output_tokens) == (101,35)
    else:
        assert not result.images and result.usage.input_tokens is None
        if scenario=='late-failed-image':
            assert result.observed_image_calls == 1
            assert 'reported failure after stop' in result.message


def test_stop_during_turn_start_without_identity_does_not_guess_interrupt(rpc, tmp_path):
    client, state, sessions, entries, _ = rpc
    state['scenario'] = 'missing-start-ack'
    result = client.generate(adapter.ImageRequest('synthetic one image', tmp_path/'output'))
    assert time.monotonic()-state['stopped_at'] < 1
    assert result.status == 'unknown' and result.failure_code == 'cancelled'
    assert result.thread_id == 'thread-image' and result.turn_id == ''
    assert result.provider_invoked and result.usage_status == 'unknown'
    assert sum(row['method']=='turn/start' for row in entries()) == 1
    assert not any(row['method']=='turn/interrupt' for row in entries())
    assert sessions[0].process.poll() is not None and not sessions[0].reader.is_alive()


def test_pre_turn_stop_closes_real_pipe_without_claiming_invocation(rpc, tmp_path):
    client, state, sessions, entries, _ = rpc
    state['scenario'] = 'pre-turn-stop'
    with pytest.raises(transport.CodexUnavailable) as caught:
        client.generate(adapter.ImageRequest('synthetic one image', tmp_path/'output'))
    assert caught.value.provider_invoked is False
    assert time.monotonic()-state['stopped_at'] < 1
    assert not any(row['method'] in {'turn/start','turn/interrupt'} for row in entries())
    assert sessions[0].cancel_deadline is not None
    assert sessions[0].process.poll() is not None and not sessions[0].reader.is_alive()


def test_image_poll_denies_one_native_request_without_unbounded_drain():
    session = adapter._ImageSession.__new__(adapter._ImageSession)
    session.deadline = time.monotonic()+1
    session.pending = [{'id':index, 'method':'item/commandExecution/requestApproval', 'params':{}}
                       for index in range(1000)]
    session.denied_request = False
    sent = []
    session.send = sent.append
    assert session.event_poll() == {}
    assert len(session.pending) == 999 and session.denied_request
    assert sent == [{'id':0, 'result':{'decision':'decline'}}]


def test_pending_progress_cannot_extend_original_deadline():
    session = adapter._ImageSession.__new__(adapter._ImageSession)
    session.deadline = time.monotonic()-1
    session.pending = [{'method':'codex/event','params':{}}]
    with pytest.raises(ModelError, match='timed out'):
        session.event_poll()
    assert len(session.pending) == 1


def test_cancellable_rpc_preserves_deferred_notifications_and_response_identity():
    session = adapter._ImageSession.__new__(adapter._ImageSession)
    notification = {'method':'thread/started', 'params':{'threadId':'unrelated'}}
    session.pending, session.messages = [notification], queue.Queue()
    session.messages.put({'id':1, 'result':{'accepted':True}})
    session.next_id, session.denied_request = 0, False
    session.deadline, session.cancel_deadline = time.monotonic()+1, None
    session.check_cancel = lambda: None
    sent = []
    session.send = sent.append
    assert session.request('turn/start', {'threadId':'thread-image'}) == {'accepted':True}
    assert session.pending == [notification]
    assert sent == [{'id':1, 'method':'turn/start', 'params':{'threadId':'thread-image'}}]
    assert session.turn_start_sent


def test_actual_image_rpc_records_no_send_when_its_last_check_stops():
    session = adapter._ImageSession.__new__(adapter._ImageSession)
    session.next_id, session.cancel_deadline = 0, None
    sent = []
    session.send = sent.append

    def stop():
        raise JobCancelled()

    session.check_cancel = stop
    with pytest.raises(JobCancelled):
        session.request('turn/start', {'threadId':'thread-image'})
    assert not session.turn_start_sent and not sent
    assert session.cancel_deadline is not None


def test_image_rpc_write_failure_remains_possibly_submitted():
    session = adapter._ImageSession.__new__(adapter._ImageSession)
    session.next_id, session.cancel_deadline = 0, None
    session.check_cancel = lambda: None

    def broken_write(_):
        raise ModelError(0, 'synthetic write failure')

    session.send = broken_write
    with pytest.raises(ModelError):
        session.request('turn/start', {'threadId':'thread-image'})
    assert session.turn_start_sent
