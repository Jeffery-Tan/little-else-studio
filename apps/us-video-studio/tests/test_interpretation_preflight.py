"""Bounded first-binding feedback; all data and HTTP responses are synthetic."""
import copy
import json

import pytest

from studio import conversation_input as ingress
from studio.agents.openai_compat import OpenAICompatClient
from studio.ledger import Ledger
from studio.models import PipelineError, STAGES
from studio.tools import registry
from test_context_projection import RUN, env, executing, clause, proposal
from test_deepseek_client import transport
from test_finishing_phase import reply
from test_image_analysis import scene, wait_job
from test_reply_conditions import declared, reply_row
from test_server import RUN as HTTP_RUN


TEXT = '仅查看两张图片。用两行表，全文不超过400字，不含表头。不要创作。'


def invalid_proposal(text=TEXT):
    value = declared({'text': text},
        reply_row('用两行表', 'reply.format', 'PRIVATE_MODEL_FORMAT'),
        reply_row('不含表头', 'reply.table_rows', '2'))
    value['task']['quote'] = '仅查看两张图片。全文不超过400字'
    return value


def detail(error):
    text = str(error)
    return json.loads(text[text.index('{'):])


def assert_empty(store, run_id):
    assert ingress.interpretation_for(store, run_id, next(
        event['message_id'] for event in reversed(store.read_events(run_id))
        if event['kind'] == 'user_message_received')) is None
    assert not store.list_approvals(run_id)
    assert all(store.latest_artifact(run_id, stage) is None for stage in STAGES)


def test_first_binding_reports_three_independent_fields_with_no_mutation_or_write(env):
    store, _ = env
    receipt = executing(env, TEXT)
    value = invalid_proposal()
    original = copy.deepcopy(value)
    events, chat = store.read_events(RUN), store.read_chat(RUN)
    with pytest.raises(PipelineError) as caught:
        ingress.record_task_interpretation(store, RUN, receipt, value)
    report = detail(caught.value)
    assert report['code'] == 'interpretation_field_errors' and report['remaining_errors'] == 0
    assert [(item['path'], item['code']) for item in report['issues']] == [
        (['task', 'quote'], 'quote_not_contiguous'),
        (['constraints', 0, 'value'], 'reply_format_invalid'),
        (['constraints', 1, 'quote'], 'reply_numeric_witness_missing')]
    assert 'PRIVATE_MODEL_FORMAT' not in str(caught.value) and TEXT not in str(caught.value)
    assert value == original and store.read_events(RUN) == events and store.read_chat(RUN) == chat
    assert_empty(store, RUN)


def test_fixed_feedback_is_bounded_and_never_echoes_unknown_model_text(env):
    store, _ = env
    receipt = executing(env, TEXT)
    rows = [reply_row('PRIVATE_MODEL_QUOTE', 'reply.format', 'PRIVATE_MODEL_VALUE') for _ in range(32)]
    value = declared(receipt, *rows)
    events = store.read_events(RUN)
    with pytest.raises(PipelineError) as caught:
        ingress.record_task_interpretation(store, RUN, receipt, value)
    report = detail(caught.value)
    assert len(report['issues']) == 6 and report['remaining_errors'] == 90
    assert all(set(item) == {'path', 'code', 'message'} for item in report['issues'])
    assert 'PRIVATE_MODEL' not in str(caught.value) and len(str(caught.value).encode()) < 2000
    assert store.read_events(RUN) == events


@pytest.mark.parametrize('mutation', [
    lambda value: value['task'].pop('pause_after'),
    lambda value: value['constraints'][0].pop('ambiguity'),
    lambda value: value['constraints'][0].update(PRIVATE_MODEL_KEY='PRIVATE_MODEL_VALUE'),
    lambda value: value['constraints'][0].update(value={'PRIVATE_MODEL_KEY': 'PRIVATE_MODEL_VALUE'}),
    lambda value: value['constraints'][0].update(quote=17),
    lambda value: value.update(video_use={'source': 'synthetic-only'}),
    lambda value: value.update(constraints=value['constraints'] * 17),
    lambda value: value.update(constraints='PRIVATE_MODEL_VALUE'),
    lambda value: value['constraints'][0]['scope'].update(paths=[['x'] * 2049]),
])
def test_incomplete_or_unsafe_shape_never_enters_field_aggregation(mutation):
    value = invalid_proposal()
    mutation(value)
    original = copy.deepcopy(value)
    assert not ingress._complete_interpretation_shape(value)
    assert ingress._preflight_interpretation_fields(TEXT, value) is None
    assert value == original


@pytest.mark.parametrize('untrusted', ['changed_text', 'not_executing', 'already_bound'])
def test_trusted_source_execution_and_immutable_binding_take_priority(env, untrusted):
    store, _ = env
    if untrusted == 'not_executing':
        receipt = ingress.receive(store, RUN, TEXT, 'not-started')
        ingress.accept(store, RUN, receipt)
    else:
        receipt = executing(env, TEXT)
    if untrusted == 'changed_text':
        receipt = {**receipt, 'text': 'MODEL_REPLACED_SOURCE'}
    if untrusted == 'already_bound':
        ingress.record_task_interpretation(store, RUN, receipt, declared(receipt))
    events = store.read_events(RUN)
    expected = {'changed_text': '不能替换', 'not_executing': '排队消息', 'already_bound': '已经绑定'}
    with pytest.raises(PipelineError, match=expected[untrusted]) as caught:
        ingress.record_task_interpretation(store, RUN, receipt, invalid_proposal())
    assert 'interpretation_field_errors' not in str(caught.value)
    assert store.read_events(RUN) == events


def test_original_bound_replay_bypasses_new_preflight(env, monkeypatch):
    store, _ = env
    receipt = executing(env, TEXT)
    value = declared(receipt, reply_row('全文不超过400字'))
    first = ingress.record_task_interpretation(store, RUN, receipt, value)
    events = store.read_events(RUN)
    def forbidden(*_):
        pytest.fail('a previously accepted interpretation must not be re-preflighted')
    monkeypatch.setattr(ingress, '_preflight_interpretation_fields', forbidden)
    assert ingress.record_task_interpretation(store, RUN, receipt, value) == first
    assert store.read_events(RUN) == events


@pytest.mark.parametrize('mutation, expected', [
    (lambda row: row.update(value='x' * 1001), '内容、操作'),
    (lambda row: row['scope'].update(paths=[['segments', -1]]), '范围无效'),
    (lambda row: row['scope'].update(stages=['writing', 'writing']), '范围无效'),
    (lambda row: row.update(supersedes=['not-a-real-condition'], operation='replace'), '现存且有效'),
])
def test_preflight_does_not_replace_remaining_business_validation(env, mutation, expected):
    store, _ = env
    receipt = executing(env, TEXT)
    row = reply_row('全文不超过400字', 'answer.detail', 'short')
    mutation(row)
    value = declared(receipt, row)
    assert ingress._preflight_interpretation_fields(TEXT, value) is None
    events = store.read_events(RUN)
    with pytest.raises(PipelineError, match=expected):
        ingress.record_task_interpretation(store, RUN, receipt, value)
    assert store.read_events(RUN) == events


def test_too_many_conditions_keeps_original_rejection_and_zero_write(env):
    store, _ = env
    receipt = executing(env, TEXT)
    value = declared(receipt, *(reply_row('全文不超过400字') for _ in range(33)))
    assert ingress._preflight_interpretation_fields(TEXT, value) is None
    events = store.read_events(RUN)
    with pytest.raises(PipelineError, match='条件列表无效'):
        ingress.record_task_interpretation(store, RUN, receipt, value)
    assert store.read_events(RUN) == events


@pytest.mark.parametrize('failure', ['creative_stages', 'protected_key', 'typed_scope'])
def test_valid_field_preflight_never_grants_task_or_content_permission(env, failure):
    store, _ = env
    receipt = executing(env, TEXT)
    row = reply_row('全文不超过400字')
    value = declared(receipt, row)
    if failure == 'creative_stages':
        value['task']['allowed_stages'] = ['writing']
    elif failure == 'protected_key':
        row['key'] = 'authorization.generate'
    else:
        row['scope']['stages'] = ['writing']
    assert ingress._preflight_interpretation_fields(TEXT, value) is None
    events = store.read_events(RUN)
    with pytest.raises(PipelineError):
        ingress.record_task_interpretation(store, RUN, receipt, value)
    assert store.read_events(RUN) == events


def test_shared_first_call_guidance_changes_no_strict_schema_or_sentinel_decoding(monkeypatch):
    tools = registry.orchestrator_profile_tools('initial')
    schemas = {tool['name']: copy.deepcopy(tool['input_schema']) for tool in tools}
    for tool in tools:
        if tool['name'] in {'propose_task_interpretation', 'observe_images'}:
            assert '连续片段' in tool['description'] and '不含表头' in tool['description']
            assert 'value 只能为 markdown_table 或 text' in tool['description']
    old = copy.deepcopy(registry.TASK_INTERPRETATION_TOOL)
    old['description'] = 'Previous shared wording'
    monkeypatch.setattr(registry, 'TASK_INTERPRETATION_TOOL', old)
    assert {tool['name']: tool['input_schema'] for tool in registry.orchestrator_profile_tools('initial')} == schemas
    assert len(registry.orchestrator_profile_tools('image_analysis')) == 5
    value = declared({'text': TEXT})
    wire = {**value, 'reuse_artifact': 'not_applicable', 'video_use': 'not_applicable', 'image_use': 'not_applicable'}
    assert registry.strict_writer_execution_arguments('propose_task_interpretation', wire,
                                                     schemas['propose_task_interpretation']) == value
    assert wire['image_use'] == 'not_applicable'


def test_combined_entry_exposes_all_errors_to_mock_http_without_observing_or_creating(scene, monkeypatch):
    http, store, root_model, _, _, settings = scene
    value = invalid_proposal()
    wire = {**value, 'reuse_artifact': 'not_applicable', 'video_use': 'not_applicable', 'image_use': 'not_applicable'}
    replies = [reply(('observe_images', {'images': [{'name': 'fixture.png', 'purpose': 'product'}],
                                        'task_interpretation': wire})), reply(text='任务解释仍待纠正。')]
    seen = transport(monkeypatch, replies)
    producer = OpenAICompatClient('https://example.invalid/v1', 'synthetic-only')
    monkeypatch.setattr(root_model, 'complete', producer.complete)
    displayed = http.get(f'/api/runs/{HTTP_RUN}/timeline')
    assert displayed.status_code == 200, displayed.text
    response = http.post(f'/api/runs/{HTTP_RUN}/jobs', json={'text': TEXT, 'request_id': 'three-fields',
                         'display_context_id': displayed.json()['display_context_id']})
    assert response.status_code == 200, response.text
    job = wait_job(http, response.json())
    # The rejected proposal never selected a deliverable. Execution termination
    # is not an image-analysis completion claim; retain that existing boundary.
    assert job['status'] == 'succeeded'
    assert job['deliverable']['goal'] is None and job['deliverable']['status'] == 'unspecified'
    assert len(seen) == 2 and not replies and root_model.calls == []
    assert {tool['function']['name'] for tool in json.loads(seen[0].content)['tools']} == {
        'read_user_actions', 'read_capabilities', 'read_project_state', 'list_materials',
        'read_artifact', 'ask_user', 'propose_task_interpretation', 'observe_images'}
    error = next(item['content'] for item in json.loads(seen[1].content)['messages'] if item['role'] == 'tool')
    report = detail(error)
    assert [item['code'] for item in report['issues']] == [
        'quote_not_contiguous', 'reply_format_invalid', 'reply_numeric_witness_missing']
    assert 'PRIVATE_MODEL_FORMAT' not in error
    assert not any(event['kind'] in {'user_task_interpretation_recorded', 'user_constraint_recorded',
        'image_observation_recorded', 'artifact_submitted', 'production_draft_prepared', 'execution_authorized'}
        for event in store.read_events(HTTP_RUN))
    entries = Ledger(store, settings).entries(HTTP_RUN)
    assert len(entries) == 2 and all(row.role == 'orchestrator' and row.usage_status == 'known' for row in entries)
    assert_empty(store, HTTP_RUN)
    projected = http.get(f'/api/runs/{HTTP_RUN}/timeline')
    assert projected.status_code == 200, projected.text
    brief = projected.json()['brief']
    assert brief['phase'] != 'analysis_complete' and 'analysis' not in brief
    assert brief['action']['kind'] != 'analysis'


CREATIVE_TEXT = ('For this project create three photo pages with natural English copy. '
                 'Give three distinct creative directions first; keep all these conditions for later pages.')


def creative_scope_proposal(receipt, count=9):
    rows = [clause(CREATIVE_TEXT, key=f'content.rule{index}',
                   value=f'synthetic_content_{index}', scope_quote=CREATIVE_TEXT)
            for index in range(count)]
    for row in rows:
        row['scope']['stages'] = []
    return proposal(receipt, *rows, mode='produce', deliverable='direction',
                    format='photo', allowed_stages=['intake', 'direction'], pause_after=True)


def test_all_project_scope_errors_are_reported_once_without_losing_conditions(env):
    store, _ = env
    receipt = executing(env, CREATIVE_TEXT)
    value = creative_scope_proposal(receipt)
    original = copy.deepcopy(value)
    before = store.read_events(RUN)
    with pytest.raises(PipelineError) as caught:
        ingress.record_task_interpretation(store, RUN, receipt, value)
    report = detail(caught.value)
    assert report == {'code': 'constraint_scope_errors', 'issues': [
        {'path': ['constraints', index, 'scope'], 'code': 'empty_stage_scope_invalid'}
        for index in range(9)]}
    assert '不表示全部创作阶段' in str(caught.value)
    assert 'task.allowed_stages' in str(caught.value)
    assert 'synthetic_content_' not in str(caught.value) and CREATIVE_TEXT not in str(caught.value)
    assert value == original and store.read_events(RUN) == before
    assert_empty(store, RUN)

    corrected = copy.deepcopy(value)
    for row in corrected['constraints']:
        row['scope']['stages'] = list(STAGES)
    saved = ingress.record_task_interpretation(store, RUN, receipt, corrected)
    assert len(saved['project_constraints']) == 9
    assert not ingress.reply_constraints_for_input(store, RUN, receipt['message_id'])
    assert saved['task']['allowed_stages'] == ['intake', 'direction']
    assert all(row['scope']['stages'] == list(STAGES) for row in saved['constraints'])
    assert all(row['value'] == original['constraints'][index]['value']
               and row['quote'] == original['constraints'][index]['quote']
               for index, row in enumerate(saved['constraints']))
    assert not store.list_approvals(RUN)
    assert all(store.latest_artifact(RUN, stage) is None for stage in STAGES)


def test_reply_scope_feedback_is_bounded_by_original_condition_count(env):
    store, _ = env
    receipt = executing(env, CREATIVE_TEXT)
    with pytest.raises(PipelineError) as caught:
        ingress.record_task_interpretation(store, RUN, receipt, creative_scope_proposal(receipt, 32))
    report = detail(caught.value)
    assert len(report['issues']) == 32 and len(str(caught.value).encode()) < 4096
    assert all(set(issue) == {'path', 'code'} for issue in report['issues'])
    assert_empty(store, RUN)


@pytest.mark.parametrize('malformed', ['duplicate_stages', 'missing_row_field', 'bad_scope_path'])
def test_unsafe_other_row_does_not_get_aggregated_or_change_first_rejection(env, malformed):
    store, _ = env
    receipt = executing(env, CREATIVE_TEXT)
    value = creative_scope_proposal(receipt, 2)
    if malformed == 'duplicate_stages':
        value['constraints'][1]['scope']['stages'] = ['writing', 'writing']
    elif malformed == 'missing_row_field':
        value['constraints'][1].pop('ambiguity')
    else:
        value['constraints'][1]['scope']['paths'] = [['segments', -1]]
    original = copy.deepcopy(value)
    before = store.read_events(RUN)
    with pytest.raises(PipelineError) as caught:
        ingress.record_task_interpretation(store, RUN, receipt, value)
    assert detail(caught.value)['issues'] == [
        {'path': ['constraints', 0, 'scope'], 'code': 'empty_stage_scope_invalid'}]
    assert value == original and store.read_events(RUN) == before


def test_creative_scope_failure_does_not_preempt_source_quote_or_protected_key(env):
    store, _ = env
    receipt = executing(env, CREATIVE_TEXT)
    value = creative_scope_proposal(receipt)
    value['constraints'][0]['quote'] = 'SYNTHETIC_PRIVATE_BAD_QUOTE'
    with pytest.raises(PipelineError) as caught:
        ingress.record_task_interpretation(store, RUN, receipt, value)
    assert detail(caught.value)['code'] == 'interpretation_field_errors'
    assert 'constraint_scope_errors' not in str(caught.value)
    value = creative_scope_proposal(receipt)
    value['constraints'][0]['key'] = 'authorization.generate'
    with pytest.raises(PipelineError, match='执行权限') as caught:
        ingress.record_task_interpretation(store, RUN, receipt, value)
    assert 'constraint_scope_errors' not in str(caught.value)
    assert_empty(store, RUN)


def test_creative_scope_guidance_is_on_actual_initial_wire_without_schema_change(monkeypatch):
    tools = registry.orchestrator_profile_tools('initial')
    schema = next(tool['input_schema'] for tool in tools if tool['name'] == 'propose_task_interpretation')
    before = copy.deepcopy(schema)
    description = next(tool['description'] for tool in tools if tool['name'] == 'propose_task_interpretation')
    assert ingress.CONSTRAINT_SCOPE_GUIDANCE in description
    assert all(stage in description for stage in STAGES)
    old = copy.deepcopy(registry.TASK_INTERPRETATION_TOOL)
    old['description'] = 'Previous shared wording'
    monkeypatch.setattr(registry, 'TASK_INTERPRETATION_TOOL', old)
    assert next(tool['input_schema'] for tool in registry.orchestrator_profile_tools('initial')
                if tool['name'] == 'propose_task_interpretation') == before


def test_mock_http_receives_all_scope_paths_then_binds_one_corrected_proposal(scene, monkeypatch):
    http, store, root_model, _, _, settings = scene
    invalid = creative_scope_proposal({'text': CREATIVE_TEXT})
    corrected = copy.deepcopy(invalid)
    for row in corrected['constraints']:
        row['scope']['stages'] = list(STAGES)
    def wire(value):
        return {**value, 'reuse_artifact': 'not_applicable', 'video_use': 'not_applicable',
                'image_use': 'not_applicable'}
    replies = [reply(('propose_task_interpretation', wire(invalid))),
               reply(('propose_task_interpretation', wire(corrected))),
               reply(text='Creative scope is bound; this offline test does not deliver directions.'),
               reply(text='Directions remain undelivered in this offline test.')]
    seen = transport(monkeypatch, replies)
    producer = OpenAICompatClient('https://example.invalid/v1', 'synthetic-only')
    monkeypatch.setattr(root_model, 'complete', producer.complete)
    displayed = http.get(f'/api/runs/{HTTP_RUN}/timeline').json()
    response = http.post(f'/api/runs/{HTTP_RUN}/jobs', json={
        'text': CREATIVE_TEXT, 'request_id': 'scope-fields',
        'display_context_id': displayed['display_context_id']})
    assert response.status_code == 200, response.text
    job = wait_job(http, response.json())
    assert job['status'] == 'blocked' and job['deliverable']['goal'] == 'direction'
    assert len(seen) == 4 and not replies
    reminder = json.loads(seen[-1].content)['messages'][-1]['content']
    assert '任务已绑定' in reminder and 'prepare_creative_context' in reminder
    error = next(item['content'] for item in json.loads(seen[1].content)['messages']
                 if item['role'] == 'tool')
    assert len(detail(error)['issues']) == 9 and 'synthetic_content_' not in error
    events = store.read_events(HTTP_RUN)
    interpretations = [event for event in events if event['kind'] == 'user_task_interpretation_recorded']
    assert len(interpretations) == 1
    source = next(event['message_id'] for event in events if event['kind'] == 'user_message_received')
    saved = ingress.interpretation_for(store, HTTP_RUN, source)
    assert len(saved['constraints']) == 9
    assert [row['quote'] for row in saved['constraints']] == [row['quote'] for row in invalid['constraints']]
    assert all(row['scope']['stages'] == list(STAGES) for row in saved['constraints'])
    assert not any(event['kind'] in {'image_observation_recorded', 'production_draft_prepared',
                                   'execution_authorized'} for event in events)
    assert not store.list_approvals(HTTP_RUN)
    assert all(store.latest_artifact(HTTP_RUN, stage) is None for stage in STAGES)
    assert len(Ledger(store, settings).entries(HTTP_RUN)) == 4
