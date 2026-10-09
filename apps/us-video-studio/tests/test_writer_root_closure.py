"""Real HTTP/SSE adapters and isolated Store, never real models or production data."""
import copy
import hashlib
import json
import threading
from pathlib import Path

import httpx
import pytest

from studio.agent_sessions import AgentSessions
from studio.agents.budget import BudgetedClient
from studio.agents.client import (JobCancelled, ModelError, NonRetryableStageError,
                                  SafePreflightError, prepare_strict_writer_execution, run_agent_loop)
from studio.agents.deepseek_client import DeepSeekClient, _parse
from studio.agents.mimo_client import MiMoClient
from studio.agents.openai_compat import _messages
from studio.models import PipelineError
from studio.structured import MAX_CELL
from studio.tools.dispatch import Dispatcher
from studio.tools.execution import ToolResult
from studio.tools.registry import tools_for
from test_deepseek_client import transport
from test_deepseek_wait import WaitingBody, install_transport
from test_writing_tool_schema import seed, stream_reply, submission, wire


def missing_root(args=None):
    data = wire(submission() if args is None else args)
    function = data['choices'][0]['message']['tool_calls'][0]['function']
    function['arguments'] = function['arguments'][:-1]
    return data


def complete(monkeypatch, data, *, streaming=False, format='video', client=None, tools=None):
    seen = transport(monkeypatch, [stream_reply(data) if streaming else data])
    client = client or DeepSeekClient('synthetic-only')
    method = client.complete_with_progress if streaming else client.complete
    return seen, method(model='deepseek-flash', system='Synthetic only', messages=[],
                        tools=tools if tools is not None else tools_for('writer', format=format))


@pytest.mark.parametrize('format', ['video', 'photo'])
@pytest.mark.parametrize('streaming', [False, True])
def test_exact_source_execution_copy_and_completed_transport(monkeypatch, format, streaming):
    args = submission(format)
    data = missing_root(args)
    before = copy.deepcopy(data)
    original = data['choices'][0]['message']['tool_calls'][0]['function']['arguments']
    seen, turn = complete(monkeypatch, data, streaming=streaming, format=format)
    closure = turn.writer_root_closure
    assert len(seen) == 1 and data == before and turn.tool_calls[0].input == args
    assert closure.original_arguments == original and closure.execution_arguments == original + '}'
    assert closure.completion_source == ('sse_done' if streaming else 'http_body_complete')
    assert not closure.evidence_saved
    assert [d['code'] for d in turn.response_normalizations] == ['root_object_closed']
    replay = _messages('', [{'role': 'assistant', 'content': turn.raw_blocks}])
    assert replay[0]['tool_calls'][0]['function']['arguments'] == original
    block = next(b for b in turn.raw_blocks if b['type'] == 'tool_use')
    assert 'input' not in block and block['execution_input'] == args
    assert block['arguments_origin'] == 'writer_root_close_v1'
    prepare_strict_writer_execution(turn, tools_for('writer', format=format))


def test_bare_parser_has_no_completion_provenance():
    with pytest.raises(ModelError) as error:
        _parse(missing_root(), 'deepseek-flash', tools=tools_for('writer'))
    assert error.value.response_diagnostic['code'] == 'invalid_arguments_json'


@pytest.mark.parametrize('change,rule', [
    ('nested', 'nested_unclosed'), ('string', 'not_eof'),
    ('mismatch', 'not_eof'), ('comma', 'invalid_candidate_json'),
    ('auxiliary', 'schema_invalid'), ('type', 'schema_invalid'),
    ('extra', 'schema_invalid'), ('oversize', 'canonical_invalid'),
])
def test_rejected_framing_has_safe_reason_without_widening_acceptance(monkeypatch, change, rule):
    from studio.agents.requests import safe_response_diagnostic
    args = submission()
    if change == 'auxiliary':
        args['structured'].pop('open_questions')
    elif change == 'type':
        args['evidence'] = 'PRIVATE_VALUE'
    elif change == 'extra':
        args['PRIVATE_KEY'] = 'PRIVATE_VALUE'
    elif change == 'oversize':
        args['structured']['caption'] = 'X' * (MAX_CELL + 1)
    data = missing_root(args)
    malformed = {'nested': '{"PRIVATE_KEY":{"PRIVATE_VALUE":1',
                 'string': '{"PRIVATE_KEY":"PRIVATE_VALUE',
                 'mismatch': '{"PRIVATE_KEY":[1}', 'comma': '{"PRIVATE_KEY":1,'}
    if change in malformed:
        data['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = malformed[change]
    seen = transport(monkeypatch, [data])
    with pytest.raises(ModelError) as caught:
        DeepSeekClient('synthetic-only').complete(model='deepseek-flash', system='Synthetic only',
            messages=[], tools=tools_for('writer'))
    issue = caught.value.response_diagnostic
    assert len(seen) == 1 and caught.value.usage_status == 'known'
    assert issue['code'] == 'invalid_arguments_json' and issue['writer_framing_rule'] == rule
    assert issue == safe_response_diagnostic(issue, tools=tools_for('writer'))
    assert 'PRIVATE_' not in json.dumps(issue) + str(caught.value)
    if change == 'nested':
        assert issue['writer_unclosed_depth'] == 2
    if change == 'auxiliary':
        assert issue['schema_rule'] == 'missing_field'
        assert issue['schema_path'] == ['structured', 'open_questions']
    if change == 'extra':
        assert issue['schema_rule'] == 'extra_field' and issue['schema_path'] == []


def test_framing_reason_whitelist_rejects_unknown_text_and_invalid_depth():
    from studio.agents.requests import safe_response_diagnostic
    assert safe_response_diagnostic({'writer_framing_rule': 'PRIVATE_RAW_ARGS',
                                     'writer_unclosed_depth': True}) == {}
    assert safe_response_diagnostic({'writer_unclosed_depth': -1}) == {}
    assert safe_response_diagnostic({'writer_framing_rule': 'nested_unclosed',
                                     'writer_unclosed_depth': 3}) == {
        'writer_framing_rule': 'nested_unclosed', 'writer_unclosed_depth': 3}


@pytest.mark.parametrize('change', [
    'nested_object', 'nested_array', 'string', 'escape', 'trailing_comma', 'invalid_number',
    'mismatch', 'multiple_documents', 'duplicate_wrapper', 'duplicate_nested', 'nonfinite',
    'missing_auxiliary', 'missing_core', 'wrong_type', 'extra', 'canonical_oversize', 'other_tool', 'batch',
])
def test_all_other_damage_and_incomplete_shapes_still_stop(monkeypatch, change):
    args = submission()
    if change == 'missing_auxiliary':
        args['structured'].pop('open_questions')
    elif change == 'missing_core':
        args['structured']['segments'][0].pop('sound')
    elif change == 'wrong_type':
        args['evidence'] = 'PRIVATE_INVALID_VALUE'
    elif change == 'extra':
        args['PRIVATE_EXTRA_FIELD'] = 'PRIVATE_INVALID_VALUE'
    elif change == 'canonical_oversize':
        args['structured']['caption'] = 'X' * (MAX_CELL + 1)
    data = missing_root(args)
    calls = data['choices'][0]['message']['tool_calls']
    function = calls[0]['function']
    texts = {'nested_object': '{"a":{"b":1', 'nested_array': '{"a":[1',
             'string': '{"a":"unfinished', 'escape': '{"a":"unfinished\\',
             'trailing_comma': '{"a":1,', 'invalid_number': '{"a":1e',
             'mismatch': '{"a":[1}', 'multiple_documents': '{}{}',
             'nonfinite': function['arguments'].replace('"content": ""', '"content": NaN', 1)}
    if change in texts:
        function['arguments'] = texts[change]
    elif change == 'duplicate_wrapper':
        function['arguments'] += ',"summary":"PRIVATE_DUPLICATE"'
    elif change == 'duplicate_nested':
        function['arguments'] = function['arguments'].replace('"caption":', '"caption":"PRIVATE_DUPLICATE","caption":', 1)
    elif change == 'other_tool':
        function['name'] = 'approve_stage'
    elif change == 'batch':
        calls.append({'id': 'read', 'type': 'function', 'function': {'name': 'list_materials', 'arguments': '{}'}})
    seen = transport(monkeypatch, [data])
    with pytest.raises(ModelError) as error:
        DeepSeekClient('synthetic-only').complete(model='deepseek-flash', system='Synthetic only',
            messages=[], tools=tools_for('writer'))
    assert len(seen) == 1 and error.value.usage_status == 'known'
    assert error.value.response_diagnostic['code'] == 'invalid_arguments_json'
    assert 'PRIVATE_' not in json.dumps(error.value.response_diagnostic) + str(error.value)


@pytest.mark.parametrize('finish', ['length', 'stop', 'aborted', 'content_filter'])
def test_wrong_finish_is_never_repaired(monkeypatch, finish):
    data = missing_root()
    data['choices'][0]['finish_reason'] = finish
    with pytest.raises(ModelError):
        complete(monkeypatch, data)


@pytest.mark.parametrize('damage', ['no_done', 'no_finish', 'after_finish', 'broken_body'])
def test_incomplete_http_or_sse_never_reaches_recovery(monkeypatch, damage):
    text = stream_reply(missing_root()).text
    if damage == 'no_done':
        text = text.replace('data: [DONE]', '')
    elif damage == 'no_finish':
        text = text.replace('"finish_reason": "tool_calls"', '"finish_reason": null')
    elif damage == 'after_finish':
        text = text.replace('data: [DONE]', 'data: {"choices":[{"index":0,"delta":{"content":"after"},"finish_reason":null}]}\n\ndata: [DONE]')
    reply = (httpx.Response(200, content=b'{"choices": [', headers={'content-type': 'application/json'})
             if damage == 'broken_body' else
             httpx.Response(200, text=text, headers={'content-type': 'text/event-stream'}))
    seen = transport(monkeypatch, [reply])
    with pytest.raises(ModelError):
        DeepSeekClient('synthetic-only').complete_with_progress(model='deepseek-flash', system='Synthetic only',
            messages=[], tools=tools_for('writer'))
    assert len(seen) == 1


@pytest.mark.parametrize('change', ['strict_off', 'schema_extra', 'wrong_format', 'local_revision'])
def test_application_schema_identity_is_required(monkeypatch, change):
    tools = tools_for('writer')
    schema_tool = next(t for t in tools if t['name'] == 'submit_stage')
    if change == 'strict_off':
        for tool in tools:
            tool.pop('strict')
    elif change == 'schema_extra':
        schema_tool['input_schema']['properties']['synthetic_extra'] = {'type': 'string'}
        schema_tool['input_schema']['required'].append('synthetic_extra')
    elif change == 'wrong_format':
        tools = tools_for('writer', format='photo')
    else:
        tools = tools_for('writer', local_revision=True)
    with pytest.raises(ModelError):
        complete(monkeypatch, missing_root(), tools=tools)


def test_mimo_shared_transport_does_not_gain_deepseek_exception(monkeypatch):
    with pytest.raises(ModelError):
        complete(monkeypatch, missing_root(), client=MiMoClient('synthetic-only'))


@pytest.mark.parametrize('tamper', ['input', 'original', 'candidate', 'schema', 'replay'])
def test_shared_execution_rechecks_candidate_and_source(monkeypatch, tamper):
    _, turn = complete(monkeypatch, missing_root())
    if tamper == 'input':
        turn.tool_calls[0].input['summary'] = 'Changed execution'
    elif tamper == 'original':
        turn.writer_root_closure.original_arguments += ' '
    elif tamper == 'candidate':
        turn.writer_root_closure.execution_arguments += ' '
    elif tamper == 'schema':
        turn.writer_root_closure.schema_sha256 = 'f' * 64
    else:
        next(b for b in turn.raw_blocks if b['type'] == 'tool_use')['raw_arguments'] += ' '
    with pytest.raises(ModelError):
        prepare_strict_writer_execution(turn, tools_for('writer'))


def test_unbudgeted_loop_cannot_execute_without_durable_evidence(monkeypatch):
    seen = transport(monkeypatch, [missing_root()])
    executed = []
    with pytest.raises(SafePreflightError, match='证据'):
        run_agent_loop(DeepSeekClient('synthetic-only'), model='deepseek-flash', system='Synthetic only',
            messages=[], tools=tools_for('writer'), execute_tool=lambda *args: executed.append(args))
    assert len(seen) == 1 and not executed


@pytest.mark.parametrize('thinking', [False, True])
def test_dispatcher_persists_exact_private_pair_once_and_recovery_is_background(tmp_path, monkeypatch, thinking):
    store, pipe, settings, ledger, run_id = seed(tmp_path, monkeypatch)
    before = {stage: store.latest_artifact(run_id, stage).sha256 for stage in ('intake', 'direction', 'material_plan')}
    data = missing_root()
    original = data['choices'][0]['message']['tool_calls'][0]['function']['arguments'].encode('utf-8')
    seen = transport(monkeypatch, [stream_reply(data)])
    progress = []
    dispatcher = Dispatcher(store, pipe, settings, DeepSeekClient('synthetic-only',
        reasoning_effort='low' if thinking else 'none'), ledger, run_id, on_event=progress.append)
    result = dispatcher.dispatch('writing', 'Only write the current script')
    assert len(seen) == 1 and result.submitted == 'writing-v1' and result.rounds == 1
    assert len(ledger.entries(run_id)) == 1
    entry = ledger.entries(run_id)[0]
    assert entry.usage_status == 'known' and entry.output_tokens == 13602
    folder = store.run_dir(run_id) / 'response-evidence' / entry.call_id
    assert (folder / 'writer-original.txt').read_bytes() == original
    assert (folder / 'writer-execution.json').read_bytes() == original + b'}'
    normalized = [e for e in store.read_events(run_id) if e['kind'] == 'agent_response_normalized']
    assert len(normalized) == 1 and normalized[0]['call_id'] == entry.call_id
    diagnostic = normalized[0]['diagnostic']
    assert diagnostic['arguments_sha256'] == hashlib.sha256(original).hexdigest()
    assert diagnostic['execution_arguments_sha256'] == hashlib.sha256(original + b'}').hexdigest()
    assert diagnostic['processing_method'] == 'writer_root_close_v1'
    assert 'defaulted_paths' not in diagnostic and 'SYNTHETIC_PRIVATE_REASONING' not in json.dumps(normalized)
    tool_event = next(event for event in progress if event['type'] == 'agent_tool')
    assert tool_event['args'] == {} and tool_event['processing_method'] == 'writer_root_close_v1'
    assert submission()['structured']['caption'] not in json.dumps(progress, ensure_ascii=False)
    assert before == {stage: store.latest_artifact(run_id, stage).sha256 for stage in before}
    assert pipe.stage_state(run_id, 'writing').approval is None
    sessions = AgentSessions(store, run_id)
    sid = result.session_id
    history = sessions.history(sid)
    response = next(e for e in history if e['kind'] == 'model_response')
    assert response['tool_calls'][0]['input_origin'] == 'writer_root_close_v1'
    block = next(b for b in response['blocks'] if b['type'] == 'tool_use')
    assert 'raw_arguments' not in block and 'input' not in block
    assert block['execution_input'] == submission()
    recovered = sessions.recovery_messages(sid)
    assert len(recovered) == 1 and recovered[0]['role'] == 'user'
    assert '仅背景' in recovered[0]['content'] and '不重放旧工具' in recovered[0]['content']
    assert 'SYNTHETIC_PRIVATE_REASONING' not in json.dumps(history) + json.dumps(recovered)
    assert 'raw_arguments' not in json.dumps(history)


@pytest.mark.parametrize('failure', ['original_write', 'execution_write', 'metadata'])
def test_evidence_failure_retains_usage_but_executes_nothing(tmp_path, monkeypatch, failure):
    store, pipe, settings, ledger, run_id = seed(tmp_path, monkeypatch)
    seen = transport(monkeypatch, [stream_reply(missing_root())])
    if failure == 'metadata':
        original_append = store.append_event
        def append(identity, kind, **fields):
            if kind == 'agent_response_normalized':
                raise OSError('PRIVATE_FAILURE')
            return original_append(identity, kind, **fields)
        monkeypatch.setattr(store, 'append_event', append)
    else:
        original_open = Path.open
        blocked = 'writer-original.txt' if failure == 'original_write' else 'writer-execution.json'
        def open_path(path, *args, **kwargs):
            if path.name == blocked:
                raise OSError('PRIVATE_FAILURE')
            return original_open(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', open_path)
    dispatcher = Dispatcher(store, pipe, settings, DeepSeekClient('synthetic-only'), ledger, run_id)
    with pytest.raises(SafePreflightError, match='格式处理记录'):
        dispatcher.dispatch('writing', 'Only write the current script')
    assert len(seen) == 1 and store.latest_artifact(run_id, 'writing') is None
    assert ledger.entries(run_id)[0].usage_status == 'known'
    assert not [e for e in store.read_events(run_id) if e['kind'] == 'agent_response_normalized']
    assert pipe.stage_state(run_id, 'writing').open_attempt is None


def test_late_cancel_keeps_known_usage_without_evidence_or_tool(tmp_path, monkeypatch):
    store, pipe, settings, ledger, run_id = seed(tmp_path, monkeypatch)
    cancelled = False
    def check_cancel():
        if cancelled:
            raise JobCancelled('Synthetic cancellation')
    client = DeepSeekClient('synthetic-only', check_cancel=check_cancel)
    original_complete = client.complete_with_progress
    def late_cancel(**kwargs):
        nonlocal cancelled
        turn = original_complete(**kwargs)
        cancelled = True
        return turn
    monkeypatch.setattr(client, 'complete_with_progress', late_cancel)
    seen = transport(monkeypatch, [stream_reply(missing_root())])
    budgeted = BudgetedClient(client, ledger, run_id=run_id, role='writer', stage='writing')
    with pytest.raises(JobCancelled):
        budgeted.complete(model='deepseek-flash', system='Synthetic only', messages=[], tools=tools_for('writer'))
    assert len(seen) == 1 and ledger.entries(run_id)[0].usage_status == 'known'
    assert not (store.run_dir(run_id) / 'response-evidence').exists()
    assert store.latest_artifact(run_id, 'writing') is None


def test_cancelled_stream_with_complete_arguments_but_no_done_is_not_repaired(tmp_path, monkeypatch):
    store, pipe, settings, ledger, run_id = seed(tmp_path, monkeypatch)
    cancel = threading.Event()
    first = stream_reply(missing_root()).text.replace('data: [DONE]', '').encode()
    body = WaitingBody(first=first, cancel=cancel)
    async def handle(request):
        return httpx.Response(200, stream=body, headers={'content-type': 'text/event-stream'})
    seen = install_transport(monkeypatch, handle)
    client = BudgetedClient(DeepSeekClient('synthetic-only', cancel_event=cancel,
        request_timeout=1, progress_interval=.02), ledger, run_id=run_id, role='writer', stage='writing')
    with pytest.raises(JobCancelled):
        client.complete(model='deepseek-flash', system='Synthetic only', messages=[], tools=tools_for('writer'))
    assert len(seen) == 1 and body.closed
    assert ledger.entries(run_id)[0].usage_status == 'partial'
    assert not (store.run_dir(run_id) / 'response-evidence').exists()
    assert store.latest_artifact(run_id, 'writing') is None


@pytest.mark.parametrize('damage', ['reasoning', 'identity', 'usage'])
def test_late_protocol_validation_failure_cannot_save_exceptional_evidence(tmp_path, monkeypatch, damage):
    store, pipe, settings, ledger, run_id = seed(tmp_path, monkeypatch)
    data = missing_root()
    message = data['choices'][0]['message']
    if damage == 'reasoning':
        message['reasoning_content'] = 3
    elif damage == 'identity':
        message['tool_calls'][0]['id'] = ''
    else:
        data['usage']['completion_tokens'] = 'PRIVATE_BAD_COUNTER'
    seen = transport(monkeypatch, [data])
    # Deliberately select non-streaming for this complete outer protocol failure.
    class NonStreaming(DeepSeekClient):
        complete_with_progress = None
    budgeted = BudgetedClient(NonStreaming('synthetic-only'), ledger,
        run_id=run_id, role='writer', stage='writing')
    with pytest.raises(ModelError):
        budgeted.complete(model='deepseek-flash', system='Synthetic only', messages=[], tools=tools_for('writer'))
    assert len(seen) == 1 and not (store.run_dir(run_id) / 'response-evidence').exists()
    assert store.latest_artifact(run_id, 'writing') is None


@pytest.mark.parametrize('rejection', ['exception', 'is_error'])
def test_normalized_business_rejection_never_calls_model_again(tmp_path, monkeypatch, rejection):
    store, pipe, settings, ledger, run_id = seed(tmp_path, monkeypatch)
    seen = transport(monkeypatch, [missing_root()])
    client = BudgetedClient(DeepSeekClient('synthetic-only'), ledger, run_id=run_id, role='writer', stage='writing')
    executed = []
    def execute(name, arguments):
        executed.append(name)
        if rejection == 'exception':
            raise PipelineError('stale_attempt', 'Synthetic upstream changed')
        return ToolResult('Synthetic domain rejection', is_error=True)
    with pytest.raises(NonRetryableStageError, match='业务校验'):
        run_agent_loop(client, model='deepseek-flash', system='Synthetic only', messages=[],
                       tools=tools_for('writer'), execute_tool=execute, max_rounds=5)
    assert len(seen) == 1 and executed == ['submit_stage']
    assert ledger.entries(run_id)[0].usage_status == 'known'
    assert store.latest_artifact(run_id, 'writing') is None


def test_real_pipeline_unknown_shot_identity_is_still_fatal(tmp_path, monkeypatch):
    store, pipe, settings, ledger, run_id = seed(tmp_path, monkeypatch)
    args = submission()
    args['structured']['segments'][0]['shot_id'] = 'invented-shot'
    seen = transport(monkeypatch, [stream_reply(missing_root(args))])
    with pytest.raises(NonRetryableStageError, match='业务校验'):
        Dispatcher(store, pipe, settings, DeepSeekClient('synthetic-only'), ledger, run_id).dispatch(
            'writing', 'Only write the current script')
    assert len(seen) == 1 and store.latest_artifact(run_id, 'writing') is None
    assert ledger.entries(run_id)[0].usage_status == 'known'


def test_background_failure_is_durable_owned_wait_is_fatal_and_same_parent_cannot_redispatch(tmp_path, monkeypatch):
    store, pipe, settings, ledger, run_id = seed(tmp_path, monkeypatch)
    args = submission()
    args['structured']['segments'][0]['shot_id'] = 'invented-shot'
    seen = transport(monkeypatch, [stream_reply(missing_root(args)), stream_reply(wire(submission()))])
    dispatcher = Dispatcher(store, pipe, settings, DeepSeekClient('synthetic-only'), ledger, run_id)
    row = dispatcher.start_role('writing', 'Only write the current script', parent_job_id='owned-job')
    sid = row['session_id']
    status = dispatcher.wait_session(sid, timeout_seconds=30)
    assert status['done'] and status['session']['failure_code'] == 'writer_root_closure_rejected'
    assert status['outcome']['retryable'] is False and status['outcome']['status'] == 'failed'
    with pytest.raises(NonRetryableStageError, match='业务校验拒绝'):
        dispatcher.wait_session(sid, propagate_failure=True)
    for resume in (False, True):
        with pytest.raises(NonRetryableStageError, match='不能自动重派'):
            dispatcher.start_role('writing', 'Only write the current script', parent_job_id='owned-job',
                                  session_id=sid if resume else None, _resume=resume)
    assert len(seen) == 1 and pipe.stage_state(run_id, 'writing').open_attempt is None
    # Cold receipt reading does not need an in-memory Future and does not execute anything.
    import studio.tools.dispatch as module
    monkeypatch.setattr(module, '_HANDLES', {key: value for key, value in module._HANDLES.items()
                                           if key != dispatcher._key(sid)})
    recovered = Dispatcher(store, pipe, settings, DeepSeekClient('synthetic-only'), ledger, run_id)
    assert recovered.wait_session(sid)['outcome']['failure_code'] == 'writer_root_closure_rejected'
    with pytest.raises(NonRetryableStageError):
        recovered.wait_session(sid, propagate_failure=True)
    assert len(seen) == 1
    # A distinct explicitly initiated owning task can write a fresh draft normally.
    new = recovered.start_role('writing', 'New explicit task', parent_job_id='new-owned-job')
    new_status = recovered.wait_session(new['session_id'], timeout_seconds=30)
    assert new_status['done'] and new_status['outcome']['artifact']['key'] == 'writing-v1'
    assert len(seen) == 2


def test_ordinary_business_error_keeps_existing_retry_behavior(tmp_path, monkeypatch):
    store, pipe, settings, ledger, run_id = seed(tmp_path, monkeypatch)
    args = submission()
    args['structured']['segments'][0]['shot_id'] = 'invented-shot'
    seen = transport(monkeypatch, [stream_reply(wire(args)), stream_reply(wire(submission()))])
    result = Dispatcher(store, pipe, settings, DeepSeekClient('synthetic-only'), ledger, run_id).dispatch(
        'writing', 'Only write the current script')
    assert result.submitted == 'writing-v1' and result.rounds == 2 and len(seen) == 2
    assert not [e for e in store.read_events(run_id) if e['kind'] == 'agent_response_normalized']
