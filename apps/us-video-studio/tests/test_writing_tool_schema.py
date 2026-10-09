"""Complete-script protocol checks with synthetic text and Mock HTTP only."""
import copy
import json

import httpx
import pytest

from studio.agents.client import ModelError, NonRetryableStageError, run_agent_loop
from studio.agents.deepseek_client import (
    DeepSeekClient, STRICT_ENDPOINT, _check_strict_schema, _matches_strict_schema,
)
from studio.config import Settings
from studio.ledger import Ledger
from studio.pipeline import Pipeline
from studio.store import Store
from studio.structured import SHAPES, canonical_document, normalize, validate_canonical_input
from studio.tools.dispatch import Dispatcher
from studio.tools.registry import REGISTRY, tools_for
from test_deepseek_client import transport


def submission(format='video'):
    picture = (('手握包口，连续把食品放入包中。保持真实灰色布面与提手，不出现参数字幕。'
                'Camera follows the action; keep the "bag" visible.\nNo voiceover.\\path is synthetic. ') * 16).strip()
    segments = [dict(tc=f'{n * 3}–{(n + 1) * 3}', picture=picture, line='',
                     sound='Fabric rustles; room tone. No speech.', duration='约 3 秒', file='',
                     shot_id='', character_ids='', scene_id='packing_table')
                for n in range(8)]
    data = dict(segments=segments, total_duration='约 24 秒，需按实际动作复核',
                caption='Pack a little room for your day. "Ready?"\nKeep it simple.',
                assumptions=['Timing is an assumption; no new product facts.'], open_questions=[])
    if format == 'photo':
        data.pop('total_duration')
        for n, row in enumerate(segments):
            row.pop('duration')
            row['page_id'] = ''
            row['tc'] = f'Page {n + 1}'
    return dict(content='', summary='Eight synthetic shots', evidence=['fixture'],
                limitations=['Actual generated images have not been inspected.'], structured=data)


def wire(args, *, leading=False):
    calls = [{'id': 'script', 'type': 'function', 'function': {
        'name': 'submit_stage', 'arguments': json.dumps(args, ensure_ascii=False)}}]
    if leading:
        calls.insert(0, {'id': 'read', 'type': 'function', 'function': {
            'name': 'list_materials', 'arguments': '{}'}})
    return {'model': 'deepseek-flash', 'usage': {'prompt_tokens': 44976, 'completion_tokens': 13602},
            'choices': [{'finish_reason': 'tool_calls', 'message': {
                'content': '', 'reasoning_content': 'SYNTHETIC_PRIVATE_REASONING', 'tool_calls': calls}}]}


def stream_reply(data, *, width=37):
    """Split inside JSON escapes as well as ordinary characters."""
    events = []
    def emit(delta, finish=None, usage=None):
        chunk = {'model': data['model'], 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
        if usage is not None:
            chunk['usage'] = usage
        events.append('data: ' + json.dumps(chunk))
    emit({'reasoning_content': 'SYNTHETIC_PRIVATE_REASONING'})
    for n, call in enumerate(data['choices'][0]['message']['tool_calls']):
        args = call['function']['arguments']
        emit({'tool_calls': [{'index': n, 'id': call['id'], 'type': 'function', 'function': {
            'name': call['function']['name'], 'arguments': ''}}]})
        for pos in range(0, len(args), width):
            emit({'tool_calls': [{'index': n, 'function': {'arguments': args[pos:pos + width]}}]})
    emit({}, 'tool_calls', data['usage'])
    return httpx.Response(200, text='\n\n'.join([*events, 'data: [DONE]']) + '\n\n',
                          headers={'content-type': 'text/event-stream'})


@pytest.mark.parametrize('format', ['video', 'photo'])
def test_closed_writer_schema_covers_existing_fields_without_changing_other_roles(format):
    original = copy.deepcopy(REGISTRY['submit_stage'].input_schema)
    tools = tools_for('writer', format=format)
    assert {t['name'] for t in tools} == {'read_capabilities', 'read_method_document',
                                        'read_artifact', 'list_materials', 'submit_stage',
                                        'read_role_history', 'propose_task_interpretation'}
    for tool in tools:
        assert tool['strict'] is True
        _check_strict_schema(tool['input_schema'])
    schema = next(t['input_schema'] for t in tools if t['name'] == 'submit_stage')
    fields = set(schema['properties']['structured']['properties'])
    assert fields == {f.name for f in SHAPES['writing'].fields} - ({'total_duration'} if format == 'photo' else set())
    assert _matches_strict_schema(submission(format), schema)
    assert REGISTRY['submit_stage'].input_schema == original
    assert all('strict' not in t for t in tools_for('director'))
    assert all('strict' not in t for t in tools_for('writer', local_revision=True))


@pytest.mark.parametrize('format', ['video', 'photo'])
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('reasoning_effort', ['none', 'high'])
def test_long_script_quotes_newlines_and_chinese_survive_strict_http_and_stream(monkeypatch, format, streaming, reasoning_effort):
    args = submission(format)
    data = wire(args)
    assert len(data['choices'][0]['message']['tool_calls'][0]['function']['arguments'].encode('utf-8')) > 18017
    seen = transport(monkeypatch, [stream_reply(data) if streaming else data])
    client = DeepSeekClient('synthetic-only', reasoning_effort=reasoning_effort)
    progress = []
    params = dict(model='deepseek-flash', system='Synthetic only', messages=[], tools=tools_for('writer', format=format))
    turn = (client.complete_with_progress(**params, on_progress=progress.append) if streaming else client.complete(**params))
    assert len(seen) == 1 and str(seen[0].url) == STRICT_ENDPOINT
    payload = json.loads(seen[0].content)
    assert all(t['function']['strict'] is True for t in payload['tools'])
    assert payload['thinking']['type'] == ('disabled' if reasoning_effort == 'none' else 'enabled')
    assert 'response_format' not in payload and 'tool_choice' not in payload
    assert turn.tool_calls[0].input == args
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (44976, 13602)
    assert all(p['type'] == 'agent_activity' for p in progress)
    assert 'SYNTHETIC_PRIVATE_REASONING' not in str(progress) and 'picture' not in str(progress)
    validate_canonical_input('writing', args['structured'])
    normalized = normalize('writing', args['structured'])
    assert normalized.dropped == 0
    document = canonical_document('writing', normalized.data)
    assert args['structured']['segments'][0]['picture'] in document
    assert args['structured']['caption'] in document
    if format == 'photo':
        assert 'total_duration' not in normalized.data
        assert all(row['duration'] == '' for row in normalized.data['segments'])


@pytest.mark.parametrize('change', ['missing', 'extra', 'wrong_type', 'prose', 'unknown_tool'])
def test_valid_json_outside_strict_schema_rejects_entire_response_before_any_tool(monkeypatch, change):
    args = submission()
    if change == 'missing':
        args['structured']['segments'][0].pop('sound')
    elif change == 'extra':
        args['structured']['segments'][0]['invented'] = 'SENSITIVE_SYNTHETIC_TEXT'
    elif change == 'wrong_type':
        args['structured']['segments'][0]['picture'] = ['SENSITIVE_SYNTHETIC_TEXT']
    elif change == 'prose':
        args['content'] = 'SENSITIVE_SYNTHETIC_TEXT'
    data = wire(args, leading=True)
    if change == 'unknown_tool':
        data['choices'][0]['message']['tool_calls'][1]['function']['name'] = 'approve_stage'
    seen = transport(monkeypatch, [data])
    executed = []
    with pytest.raises(ModelError) as failure:
        run_agent_loop(DeepSeekClient('synthetic-only'), model='deepseek-flash', system='Synthetic only',
                       messages=[], tools=tools_for('writer'),
                       execute_tool=lambda *args: executed.append(args), max_rounds=3)
    error = failure.value
    assert len(seen) == 1 and executed == []
    assert error.response_diagnostic['code'] == 'invalid_arguments_schema'
    assert error.response_diagnostic['tool_call_index'] == 1
    expected = {
        'missing': (['structured', 'segments', 0, 'sound'], 'missing_field'),
        'extra': (['structured', 'segments', 0], 'extra_field'),
        'wrong_type': (['structured', 'segments', 0, 'picture'], 'wrong_type'),
        'prose': (['content'], 'enum_mismatch'),
        'unknown_tool': (None, 'unavailable_tool'),
    }
    path, rule = expected[change]
    assert error.response_diagnostic.get('schema_path') == path
    assert error.response_diagnostic['schema_rule'] == rule
    assert '具体问题' in error.public_message
    assert 'invented' not in error.public_message + json.dumps(error.response_diagnostic)
    assert error.usage_status == 'known' and error.usage.output_tokens == 13602
    assert 'SENSITIVE_SYNTHETIC_TEXT' not in str(error) + json.dumps(error.response_diagnostic)
    assert 'SYNTHETIC_PRIVATE_REASONING' not in str(error) + json.dumps(error.response_diagnostic)


def test_invalid_json_in_stream_still_stops_all_tools_and_retains_received_usage(monkeypatch):
    data = wire(submission(), leading=True)
    data['choices'][0]['message']['tool_calls'][1]['function']['arguments'] = '{"summary":"bad unescaped "quote""}'
    seen = transport(monkeypatch, [stream_reply(data)])
    with pytest.raises(ModelError) as failure:
        DeepSeekClient('synthetic-only').complete_with_progress(
            model='deepseek-flash', system='Synthetic only', messages=[], tools=tools_for('writer'))
    assert len(seen) == 1 and failure.value.response_diagnostic['code'] == 'invalid_arguments_json'
    assert failure.value.usage_status == 'known'


@pytest.mark.parametrize('change', ['broad_object', 'optional_field', 'open_object', 'unsupported_keyword', 'mixed'])
def test_incompatible_strict_schema_fails_before_http_without_silent_downgrade(monkeypatch, change):
    tools = tools_for('writer')
    submission_tool = next(t for t in tools if t['name'] == 'submit_stage')
    schema = submission_tool['input_schema']
    if change == 'broad_object':
        submission_tool['input_schema'] = {'type': 'object'}
    elif change == 'optional_field':
        schema['required'].pop()
    elif change == 'open_object':
        schema['additionalProperties'] = True
    elif change == 'unsupported_keyword':
        schema['properties']['summary']['maxLength'] = 4000
    elif change == 'mixed':
        tools[0].pop('strict')
    seen = transport(monkeypatch, [])
    with pytest.raises(ModelError) as failure:
        DeepSeekClient('synthetic-only').complete(model='deepseek-flash', system='', messages=[], tools=tools)
    assert seen == [] and failure.value.provider_invoked is False
    assert failure.value.usage_status == 'known' and not failure.value.retryable


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('status', [400, 429, 500])
def test_strict_endpoint_rejection_has_no_second_request_or_fallback(monkeypatch, streaming, status):
    seen = transport(monkeypatch, [httpx.Response(status, text='SYNTHETIC_PRIVATE_PROVIDER_ERROR')])
    client = DeepSeekClient('synthetic-only')
    with pytest.raises(ModelError) as failure:
        method = client.complete_with_progress if streaming else client.complete
        method(model='deepseek-flash', system='', messages=[], tools=tools_for('writer'))
    assert len(seen) == 1 and str(seen[0].url) == STRICT_ENDPOINT
    assert failure.value.status == status and not failure.value.retryable
    assert failure.value.usage_status == 'unknown'
    assert 'SYNTHETIC_PRIVATE_PROVIDER_ERROR' not in str(failure.value)


def seed(tmp_path, monkeypatch):
    store = Store(tmp_path)
    run_id = 'strict-writer-fixture'
    store.create_run(run_id, 'Synthetic only', 'Write a script using direction A')
    pipe = Pipeline(store)
    for stage, structured in [('intake', {'facts': [{'label': 'Product', 'value': 'Bag', 'source': 'fixture'}]}),
                              ('direction', {'options': [{'key': 'A', 'claim': 'Packing', 'hook': 'Open bag',
                                                          'satisfies': 'goal.primary'}]}),
                              ('material_plan', {'items': [{'shot': 'Pack', 'status': '需要生成'}]})]:
        attempt = pipe.start_attempt(run_id, stage)
        pipe.submit(run_id, stage, attempt.token, content='', summary=stage, structured=structured)
        if stage == 'direction':
            pipe.record_approval(run_id, stage, nonce=pipe.issue_nonce(run_id, stage),
                                 decision='approved', selected_option_id='A')
    monkeypatch.setattr('studio.learning.LearningService.prepare_context', lambda *a, **k: {
        'text': 'FIXTURE_METHOD', 'snapshot_sha256': 'f' * 64, 'evidence_ref': 'fixture-method'})
    settings = Settings(tmp_path, 'https://example.invalid')
    return store, pipe, settings, Ledger(store, settings), run_id


@pytest.mark.parametrize('failure', [None, 'schema', 'rejected'])
def test_real_dispatcher_saves_only_valid_script_and_preserves_upstreams_and_ledger(tmp_path, monkeypatch, failure):
    store, pipe, settings, ledger, run_id = seed(tmp_path, monkeypatch)
    before = {stage: store.latest_artifact(run_id, stage).sha256 for stage in ('intake', 'direction', 'material_plan')}
    args = submission()
    if failure == 'schema':
        args['structured']['segments'][0]['sound'] = 12
    data = wire(args)
    seen = transport(monkeypatch, [httpx.Response(400) if failure == 'rejected' else stream_reply(data)])
    dispatcher = Dispatcher(store, pipe, settings, DeepSeekClient('synthetic-only'), ledger, run_id)
    if failure:
        with pytest.raises(NonRetryableStageError):
            dispatcher.dispatch('writing', 'Only write the current script')
        assert store.latest_artifact(run_id, 'writing') is None
    else:
        result = dispatcher.dispatch('writing', 'Only write the current script')
        assert result.submitted == 'writing-v1' and result.rounds == 1
        artifact = store.latest_artifact(run_id, 'writing')
        assert len(artifact.structured['segments']) == 8
        assert args['structured']['segments'][0]['picture'] in store.read_artifact_text(run_id, 'writing', 1)
    assert len(seen) == 1
    assert pipe.stage_state(run_id, 'writing').open_attempt is None
    assert before == {stage: store.latest_artifact(run_id, stage).sha256 for stage in before}
    assert pipe.stage_state(run_id, 'direction').approval.selected_option_id == 'A'
    entries = ledger.entries(run_id)
    assert len(entries) == 1
    assert entries[0].usage_status == ('unknown' if failure == 'rejected' else 'known')
    if failure == 'schema':
        rejected = next(e for e in store.read_events(run_id) if e['kind'] == 'agent_response_rejected')
        assert rejected['diagnostic']['code'] == 'invalid_arguments_schema'
        assert rejected['call_id'] == entries[0].call_id
        assert rejected['diagnostic']['schema_path'] == ['structured', 'segments', 0, 'sound']
        assert rejected['diagnostic']['schema_rule'] == 'wrong_type'


@pytest.mark.parametrize('version, rule, actual', [(0, 'minimum_mismatch', 'integer'),
                                                   (True, 'wrong_type', 'boolean'),
                                                   (None, 'wrong_type', 'null')])
def test_read_version_diagnostic_has_safe_types_and_no_invented_values(version, rule, actual):
    from studio.agents.deepseek_client import _strict_schema_issue
    schema = next(t['input_schema'] for t in tools_for('writer') if t['name'] == 'read_artifact')
    assert _strict_schema_issue({'stage': 'intake', 'version': version}, schema) == {
        'schema_path': ['version'], 'schema_rule': rule, 'expected_type': 'integer', 'actual_type': actual}


@pytest.mark.parametrize('format', ['video', 'photo'])
def test_full_writer_prompt_does_not_offer_legacy_prose_against_current_schema(tmp_path, monkeypatch, format):
    from studio.agents.roles import ROLES
    store, _, _, _, run_id = seed(tmp_path, monkeypatch)
    system = ROLES['writer'].system(store.read_run(run_id), format=format)
    assert '可以使用非空 content 正文' not in system
    assert 'content 必须为空' in system
    local = ROLES['writer'].system(store.read_run(run_id), format=format, local_revision=True)
    assert '完整交付按本次 submit_stage' not in local
    assert '完整稿只写 structured' not in local
    assert '# 一份内容，多种展示' not in local
