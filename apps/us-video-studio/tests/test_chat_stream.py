"""Shared Chat Completions framing, using synthetic HTTP only."""
import hashlib
import json
import threading

import httpx
import pytest

from studio.agents.client import JobCancelled, ModelError
from studio.agents.deepseek_client import DeepSeekClient
from studio.agents.mimo_client import MiMoClient
from studio.agents.requests import safe_response_diagnostic
from test_deepseek_client import transport
from test_image_analysis import scene
from test_deepseek_wait import WaitingBody, install_transport


PARAMS = dict(model='mimo-v2.6-flash', system='Synthetic', messages=[], tools=[])
USAGE = {'prompt_tokens': 12, 'completion_tokens': 18}
PRIVATE = 'SYNTHETIC_PRIVATE_RESPONSE'


def frame(value, *, event=None):
    return (f'event: {event}\n' if event else '') + 'data: ' + json.dumps(value) + '\n\n'


def chunk(delta=None, *, finish=None, usage=None):
    value = {'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
    if usage is not None:
        value['usage'] = usage
    return value


def reply(wire):
    return httpx.Response(200, text=wire, headers={'content-type': 'text/event-stream'})


@pytest.mark.parametrize('client_class', [DeepSeekClient, MiMoClient])
def test_separate_usage_event_retains_text_and_final_accounting(monkeypatch, client_class):
    wire = frame(chunk({'content': 'Observed.'}, finish='stop'))
    wire += frame({'choices': [], 'usage': USAGE}) + 'data: [DONE]\n\n'
    seen = transport(monkeypatch, [reply(wire)])
    result = client_class('synthetic-only').complete_with_progress(**PARAMS)
    assert result.text == 'Observed.' and result.stop_reason == 'end_turn'
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 18)
    assert len(seen) == 1


def test_multiline_sse_and_empty_keepalive_do_not_break_json_or_leak_reasoning(monkeypatch):
    wire = ': keep-alive\n\ndata:\n\n'
    value = json.dumps(chunk({'reasoning_content': PRIVATE}), indent=2)
    wire += '\n'.join('data: ' + line for line in value.splitlines()) + '\n\n'
    wire += frame(chunk({'content': 'Observed.'}, finish='stop', usage=USAGE))
    wire += 'data: [DONE]\n\n'
    seen = transport(monkeypatch, [reply(wire)])
    progress = []
    result = MiMoClient('synthetic-only').complete_with_progress(**PARAMS, on_progress=progress.append)
    assert result.text == 'Observed.' and PRIVATE not in str(progress)
    assert result.raw_blocks[0]['reasoning_content'] == PRIVATE and len(seen) == 1


@pytest.mark.parametrize('wire', [
    frame({'error': {'message': PRIVATE, 'code': 400}}),
    frame({'message': PRIVATE}, event='error'),
    frame({'error': {'message': PRIVATE, 'code': 503}, 'usage': USAGE}, event='error'),
    frame(chunk({'content': PRIVATE}, finish='stop', usage=USAGE))
        + frame({'error': {'message': PRIVATE}}) + 'data: [DONE]\n\n',
])
def test_http_200_provider_error_is_specific_private_and_never_retried(monkeypatch, wire):
    seen = transport(monkeypatch, [reply(wire)])
    with pytest.raises(ModelError) as caught:
        MiMoClient('synthetic-only').complete_with_progress(**PARAMS)
    error = caught.value
    has_usage = 'prompt_tokens' in wire
    assert len(seen) == 1 and error.retryable is False
    assert error.usage_status == ('partial' if has_usage else 'unknown')
    if has_usage:
        assert (error.usage.input_tokens, error.usage.output_tokens) == (12, 18)
    assert error.response_diagnostic['provider'] == 'mimo'
    assert error.response_diagnostic['code'] == 'provider_error'
    assert 'MiMo' in str(error) and 'DeepSeek' not in str(error)
    assert PRIVATE not in str(error) + str(error.public_message) + json.dumps(error.response_diagnostic)


@pytest.mark.parametrize('bad,field,code', [
    ([], 'response', 'expected_object'),
    ({'choices': []}, 'choices', 'expected_single_choice'),
    ({'choices': [None]}, 'choice', 'expected_object'),
    ({'choices': [{'index': 1}]}, 'choice', 'invalid_identity'),
    (chunk([]), 'delta', 'expected_object'),
    (chunk({'content': [PRIVATE]}), 'content', 'expected_text'),
    (chunk({'reasoning_content': [PRIVATE]}), 'reasoning_content', 'expected_text'),
    (chunk({'tool_calls': {'private': PRIVATE}}), 'tool_calls', 'expected_array'),
    (chunk({'tool_calls': [None]}), 'tool_call', 'expected_object'),
    (chunk({'tool_calls': [{'index': True}]}), 'tool_call', 'invalid_identity'),
    (chunk({'tool_calls': [{'index': 0, 'function': []}]}), 'tool_call', 'expected_function'),
    (chunk({'tool_calls': [{'index': 0, 'function': {'arguments': [PRIVATE]}}]}),
        'tool_arguments', 'expected_text'),
    (chunk({}, finish={'private': PRIVATE}), 'finish_reason', 'expected_text'),
])
def test_malformed_frames_record_safe_exact_location(monkeypatch, bad, field, code):
    raw = json.dumps(bad)
    wire = frame(chunk({'reasoning_content': PRIVATE})) + 'data: ' + raw + '\n\n'
    seen = transport(monkeypatch, [reply(wire)])
    with pytest.raises(ModelError) as caught:
        MiMoClient('synthetic-only').complete_with_progress(**PARAMS)
    error = caught.value
    diagnostic = error.response_diagnostic
    assert diagnostic['format'] == 'chat_completion_stream'
    assert diagnostic['field'] == field and diagnostic['code'] == code
    assert diagnostic['frame_index'] == 1 and diagnostic['frame_bytes'] == len(raw.encode())
    assert diagnostic['frame_sha256'] == hashlib.sha256(raw.encode()).hexdigest()
    assert diagnostic == safe_response_diagnostic(diagnostic)
    assert PRIVATE not in str(error) + str(error.public_message) + json.dumps(diagnostic)
    assert len(seen) == 1 and error.usage_status == 'unknown'


def test_invalid_json_has_position_and_fingerprint_without_raw_fragment(monkeypatch):
    raw = '{"secret":"' + PRIVATE + '"'
    transport(monkeypatch, [reply('data: ' + raw + '\n\n')])
    with pytest.raises(ModelError) as caught:
        MiMoClient('synthetic-only').complete_with_progress(**PARAMS)
    issue = caught.value.response_diagnostic
    assert issue['field'] == 'response' and issue['code'] == 'invalid_chunk_json'
    assert issue['json_position'] > 0 and issue['frame_index'] == 0
    assert PRIVATE not in str(caught.value) + json.dumps(issue)


@pytest.mark.parametrize('wire', [
    frame(chunk({'content': PRIVATE}, finish='stop', usage=USAGE)),
    frame(chunk({'content': PRIVATE}, usage=USAGE)) + 'data: [DONE]\n\n',
    frame({'choices': [], 'usage': USAGE}) + 'data: [DONE]\n\n',
])
def test_incomplete_stream_does_not_confirm_usage_or_deliver_content(monkeypatch, wire):
    seen = transport(monkeypatch, [reply(wire)])
    with pytest.raises(ModelError) as caught:
        MiMoClient('synthetic-only').complete_with_progress(**PARAMS)
    error = caught.value
    assert error.usage_status == 'partial' and error.response_diagnostic['code'] == 'incomplete_stream'
    assert (error.usage.input_tokens, error.usage.output_tokens) == (12, 18)
    assert PRIVATE not in str(error) + json.dumps(error.response_diagnostic)
    assert len(seen) == 1


def test_null_error_is_not_a_failure_and_invalid_usage_is_not_hidden(monkeypatch):
    data = {**chunk({'content': 'Observed.'}, finish='stop', usage=USAGE), 'error': None}
    valid = reply(frame(data) + 'data: [DONE]\n\n')
    data['usage'] = []
    transport(monkeypatch, [valid, reply(frame(data) + 'data: [DONE]\n\n')])
    assert MiMoClient('synthetic-only').complete_with_progress(**PARAMS).text == 'Observed.'
    with pytest.raises(ModelError) as caught:
        MiMoClient('synthetic-only').complete_with_progress(**PARAMS)
    issue = caught.value.response_diagnostic
    assert issue['provider'] == 'mimo' and issue['field'] == 'usage'


@pytest.mark.parametrize('error_body', [False, True])
def test_http_200_json_body_is_checked_in_the_same_call(monkeypatch, error_body):
    data = ({'error': {'message': PRIVATE}} if error_body else
        {'choices': [{'finish_reason': 'stop', 'message': {'content': 'Observed.'}}], 'usage': USAGE})
    seen = transport(monkeypatch, [httpx.Response(200, json=data)])
    if error_body:
        with pytest.raises(ModelError) as caught:
            MiMoClient('synthetic-only').complete_with_progress(**PARAMS)
        assert caught.value.response_diagnostic['code'] == 'provider_error'
        assert PRIVATE not in str(caught.value) + json.dumps(caught.value.response_diagnostic)
    else:
        result = MiMoClient('synthetic-only').complete_with_progress(**PARAMS)
        assert result.text == 'Observed.' and result.usage.output_tokens == 18
    assert len(seen) == 1


@pytest.mark.parametrize('with_usage', [False, True])
def test_stream_diagnostic_is_bound_to_the_original_ledger_call(monkeypatch, tmp_path, with_usage):
    from studio.agents.budget import BudgetedClient
    from studio.config import Settings
    from studio.ledger import Ledger
    from studio.store import Store

    store = Store(tmp_path)
    store.create_run('stream-fixture', 'Synthetic', 'Synthetic')
    ledger = Ledger(store, Settings(tmp_path, ''))
    wire = frame({'choices': [], 'usage': USAGE}) if with_usage else ''
    wire += frame({'error': {'message': PRIVATE}})
    transport(monkeypatch, [reply(wire)])
    client = BudgetedClient(MiMoClient('synthetic-only'), ledger, run_id='stream-fixture',
                            role='material', stage='video_analysis')
    with pytest.raises(ModelError):
        client.complete(**PARAMS)
    rows = ledger.entries('stream-fixture')
    issue = next(e for e in store.read_events('stream-fixture') if e['kind'] == 'agent_response_rejected')
    assert len(rows) == 1 and issue['call_id'] == rows[0].call_id
    assert issue['diagnostic']['provider'] == 'mimo' and issue['diagnostic']['code'] == 'provider_error'
    assert rows[0].usage_status == ('partial' if with_usage else 'unknown') and rows[0].ok is False
    assert (rows[0].input_tokens, rows[0].output_tokens) == ((12, 18) if with_usage else (None, None))
    assert PRIVATE not in json.dumps(issue) + rows[0].error


@pytest.mark.parametrize('cause', ['timeout', 'stop', 'disconnect'])
def test_interrupted_request_keeps_partial_usage_and_closes_transport(monkeypatch, cause):
    cancel = threading.Event()
    first = frame({'choices': [], 'usage': USAGE}).encode()

    class BrokenBody(WaitingBody):
        async def __aiter__(self):
            yield self.first
            raise httpx.ReadError(PRIVATE)

    body = (BrokenBody(first) if cause == 'disconnect' else
            WaitingBody(first, cancel=cancel if cause == 'stop' else None))

    async def handler(_):
        return httpx.Response(200, stream=body)

    seen = install_transport(monkeypatch, handler)
    client = MiMoClient('synthetic-only', cancel_event=cancel)
    client.request_timeout, client.progress_interval = .12, .02
    with pytest.raises(JobCancelled if cause == 'stop' else ModelError) as caught:
        client.complete_with_progress(**PARAMS)
    error = caught.value
    assert (error.usage.input_tokens, error.usage.output_tokens) == (12, 18)
    if cause == 'stop':
        assert not error.usage_final
    else:
        assert error.usage_status == 'partial'
    assert len(seen) == 1 and body.closed
    assert PRIVATE not in str(error)


@pytest.mark.parametrize('usage,expected', [
    ({'prompt_tokens': True, 'completion_tokens': -1}, (None, None)),
    ({'prompt_tokens': 12, 'completion_tokens': '18'}, (12, None)),
    ({'completion_tokens': 18}, (None, 18)),
])
def test_failed_stream_preserves_only_valid_partial_counters(monkeypatch, usage, expected):
    wire = frame({'choices': [], 'usage': usage}) + frame({'error': {'message': PRIVATE}})
    transport(monkeypatch, [reply(wire)])
    with pytest.raises(ModelError) as caught:
        MiMoClient('synthetic-only').complete_with_progress(**PARAMS)
    error = caught.value
    assert (error.usage.input_tokens, error.usage.output_tokens) == expected
    assert error.usage_status == ('unknown' if expected == (None, None) else 'partial')


def test_diagnostics_reject_arbitrary_provider_labels_status_and_frame_data():
    assert safe_response_diagnostic({'provider': PRIVATE, 'provider_status': 0,
        'frame_index': -1, 'frame_bytes': True, 'frame_sha256': PRIVATE, 'raw': PRIVATE}) == {}
    assert safe_response_diagnostic({'provider': 'mimo', 'provider_status': 503}) == {
        'provider': 'mimo', 'provider_status': 503}


@pytest.mark.parametrize('failed', [False, True])
def test_native_video_route_saves_only_a_valid_completed_candidate(monkeypatch, scene, failed):
    from test_image_analysis import wait_job
    from test_mimo_video import observation, start
    from test_server import RUN

    # Exercise the real observation/ledger/HTTP route with a fake wire peer.
    wire = (frame({'error': {'message': PRIVATE, 'code': 503}}) if failed else
        frame(chunk({'content': observation().text}, finish='stop'))
        + frame({'choices': [], 'usage': USAGE}) + 'data: [DONE]\n\n')
    seen = transport(monkeypatch, [reply(wire)])
    monkeypatch.setattr(scene[2], 'complete_with_progress',
        lambda **params: MiMoClient('synthetic-only').complete_with_progress(**params), raising=False)
    http, store = scene[:2]
    result, payload, _ = start(scene)
    assert result.status_code == 200, result.text
    job = wait_job(http, result.json())
    candidates = http.get(f'/api/runs/{RUN}/image-analysis').json()['candidates']
    if failed:
        assert job['status'] == 'failed' and candidates == []
        issue = next(e for e in store.read_events(RUN) if e['kind'] == 'agent_response_rejected')
        assert issue['diagnostic']['provider_status'] == 503
        assert PRIVATE not in json.dumps(issue)
    else:
        assert job['status'] == 'succeeded' and len(candidates) == 1
        assert candidates[0]['audiovisual']['audio_sent']
        assert candidates[0]['provider'] == 'mimo'
        assert http.post(f'/api/runs/{RUN}/video-analysis', json=payload).json()['job_id'] == job['job_id']
    assert len(seen) == 1
