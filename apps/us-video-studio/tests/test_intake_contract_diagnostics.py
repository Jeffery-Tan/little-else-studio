"""Existing intake acceptance, bounded feedback and synthetic native HTTP only."""
import copy
import json

import pytest

from studio import conversation_input, structured
from studio.agent_images import observe, saved_observations
from studio.agents.deepseek_client import DeepSeekClient
from studio.agents.orchestrator import Orchestrator
from studio.ledger import Ledger
from studio.models import PipelineError, STAGES
from studio.pipeline import Pipeline
from studio.store import Store
from studio.tools import registry
from test_deepseek_client import transport
from test_finishing_phase import reply
from test_image_analysis import scene
from test_image_understanding import configure, frozen, answer, observer_reply
from test_server import RUN as HTTP_RUN


RUN = 'intake-contract'


def valid_intake():
    return {'facts': [{'label': 'Visible product', 'value': 'Grey bag', 'source': 'Synthetic observation'}],
            'constraints': ['Preserve the supplied appearance'],
            'open_questions': [{'question': 'Check final layout', 'blocks': 'Presentation only',
                                'owner': 'team', 'timing': 'later', 'reason': 'Internal layout check',
                                'kind': 'local_design'}]}


def invalid_intake():
    value = valid_intake()
    value['constraints'] = [{'PRIVATE_UNKNOWN_KEY': 'PRIVATE_UNKNOWN_VALUE'}]
    value['open_questions'][0]['blocks'] = ['PRIVATE_AFFECTED_WORK']
    return value


def detail(error):
    text = str(error)
    return json.loads(text[text.index('{'):])


@pytest.fixture
def pipe(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Synthetic intake', 'Synthetic product request')
    return Pipeline(store)


def submit(pipe, value, *, content=''):
    attempt = pipe.stage_state(RUN, 'intake').open_attempt or pipe.start_attempt(RUN, 'intake')
    return pipe.submit(RUN, 'intake', attempt.token, content=content, summary='Synthetic intake', structured=value)


def test_wrong_constraints_and_blocks_are_reported_together_without_saving_or_mutating(pipe):
    old = submit(pipe, valid_intake())
    original_bytes = pipe.store.read_artifact_text(RUN, 'intake', old.version).encode()
    value = invalid_intake()
    original = copy.deepcopy(value)
    with pytest.raises(PipelineError) as caught:
        submit(pipe, value)
    report = detail(caught.value)
    assert report == {'code': 'canonical_field_errors', 'issues': [
        {'path': ['structured', 'constraints', 0], 'rule': 'wrong_type',
         'expected_type': 'string', 'actual_type': 'object'},
        {'path': ['structured', 'open_questions', 0, 'blocks'], 'rule': 'wrong_type',
         'expected_type': 'string', 'actual_type': 'array'}], 'remaining_errors': 0, 'scan_capped': False}
    assert 'PRIVATE_' not in str(caught.value)
    assert value == original and pipe.store.latest_artifact(RUN, 'intake') == old
    assert pipe.store.read_artifact_text(RUN, 'intake', old.version).encode() == original_bytes
    assert len(pipe.store.list_artifacts(RUN, 'intake')) == 1


def test_eight_wrong_constraints_do_not_hide_the_independent_blocks_error(pipe):
    value = invalid_intake()
    value['constraints'] = [{'PRIVATE_UNKNOWN_KEY': 'PRIVATE_UNKNOWN_VALUE'} for _ in range(8)]
    with pytest.raises(PipelineError) as caught:
        submit(pipe, value)
    report = detail(caught.value)
    assert [issue['path'] for issue in report['issues']] == [
        *[['structured', 'constraints', index] for index in range(8)],
        ['structured', 'open_questions', 0, 'blocks']]
    assert all(issue['rule'] == 'wrong_type' for issue in report['issues'])
    assert report['remaining_errors'] == 0 and not report['scan_capped']
    assert 'PRIVATE_' not in str(caught.value)
    assert pipe.store.latest_artifact(RUN, 'intake') is None


@pytest.mark.parametrize('length, accepted', [(4000, True), (4001, False)])
def test_capacity_boundary_uses_trimmed_unicode_length_and_never_truncates(pipe, length, accepted):
    value = valid_intake()
    value['facts'][0]['value'] = '  ' + '图' * length + '\n'
    original = copy.deepcopy(value)
    if accepted:
        assert submit(pipe, value).structured['facts'][0]['value'] == '图' * length
    else:
        with pytest.raises(PipelineError) as caught:
            submit(pipe, value)
        issue = detail(caught.value)['issues'][0]
        assert issue == {'path': ['structured', 'facts', 0, 'value'], 'rule': 'max_chars',
                         'expected_type': 'string', 'actual_type': 'string',
                         'actual_chars': 4001, 'max_chars': structured.MAX_CELL}
        assert pipe.store.latest_artifact(RUN, 'intake') is None
    assert value == original


def test_existing_null_cells_and_omitted_or_null_lists_remain_accepted(pipe):
    value = valid_intake()
    value['facts'][0]['source'] = None
    value['constraints'] = None
    value['open_questions'][0]['blocks'] = None
    value['open_questions'][0]['kind'] = None
    assert structured.canonical_field_diagnostic('intake', value) is None
    saved = submit(pipe, value)
    assert saved.structured['facts'][0]['source'] == ''
    assert saved.structured['open_questions'][0]['blocks'] == ''


@pytest.mark.parametrize('field', ['facts', 'constraints'])
def test_null_list_item_remains_rejected(pipe, field):
    value = valid_intake()
    value[field].append(None)
    with pytest.raises(PipelineError) as caught:
        submit(pipe, value)
    issue = detail(caught.value)['issues'][0]
    assert issue['path'] == ['structured', field, len(value[field]) - 1]
    assert issue['rule'] == 'wrong_type' and issue['actual_type'] == 'null'
    assert pipe.store.latest_artifact(RUN, 'intake') is None


def test_extra_names_and_values_are_omitted_and_feedback_is_bounded(pipe):
    value = valid_intake()
    value['PRIVATE_TOP_KEY'] = 'PRIVATE_TOP_VALUE'
    value['facts'][0]['PRIVATE_COLUMN'] = 'PRIVATE_CELL'
    value['constraints'] = [{'PRIVATE_KEY': 'PRIVATE_VALUE'}] * 3000
    with pytest.raises(PipelineError) as caught:
        submit(pipe, value)
    report = detail(caught.value)
    assert len(report['issues']) == structured.MAX_CANONICAL_FIELD_ISSUES == 16 and report['scan_capped']
    assert report['remaining_errors'] > 0
    assert 'PRIVATE_' not in str(caught.value) and len(str(caught.value).encode()) < 4096
    assert all(part in {'structured', 'facts', 'constraints', 'open_questions', 'label', 'value',
                       'source', *structured.QUESTION_COLUMNS} or type(part) is int
               for issue in report['issues'] for part in issue['path'])
    assert pipe.store.latest_artifact(RUN, 'intake') is None


@pytest.mark.parametrize('row_extension', [False, True])
def test_unknown_field_after_scan_limit_is_rejected_without_echo_or_saving(pipe, row_extension):
    old = submit(pipe, valid_intake())
    original_bytes = pipe.store.read_artifact_text(RUN, 'intake', old.version).encode()
    value = valid_intake()
    target = value['facts'][0] if row_extension else value
    target.update({f'PRIVATE_EMPTY_{index}': None for index in range(2050)})
    target['PRIVATE_TAIL_KEY'] = 'PRIVATE_TAIL_VALUE'
    original = copy.deepcopy(value)
    assert structured.normalize('intake', value).dropped == 0
    assert structured.canonical_field_diagnostic('intake', value) is None
    with pytest.raises(PipelineError) as caught:
        submit(pipe, value)
    assert detail(caught.value) == {'code': 'canonical_field_errors', 'issues': [{
        'path': ['structured', 'facts', 0] if row_extension else ['structured'],
        'rule': 'extra_field', 'expected_type': 'defined_field', 'actual_type': 'string'}]}
    assert 'PRIVATE_' not in str(caught.value) and len(str(caught.value).encode()) < 512
    assert value == original and pipe.store.latest_artifact(RUN, 'intake') == old
    assert pipe.store.read_artifact_text(RUN, 'intake', old.version).encode() == original_bytes
    assert len(pipe.store.list_artifacts(RUN, 'intake')) == 1


@pytest.mark.parametrize('row_extension', [False, True])
def test_empty_extensions_after_scan_limit_keep_original_acceptance(pipe, row_extension):
    value = valid_intake()
    target = value['facts'][0] if row_extension else value
    target.update({f'PRIVATE_EMPTY_{index}': None for index in range(2050)})
    original = copy.deepcopy(value)
    normalized = structured.normalize('intake', value)
    assert normalized.dropped == 0
    assert structured.canonical_field_diagnostic('intake', value) is None
    structured.validate_canonical_input('intake', value)
    assert submit(pipe, value).structured == normalized.data
    assert value == original and len(pipe.store.list_artifacts(RUN, 'intake')) == 1


def test_unknown_final_question_column_after_normal_capacity_scan_has_exact_safe_parent(pipe):
    value = valid_intake()
    value = {'facts': [copy.deepcopy(value['facts'][0]) for _ in range(structured.MAX_ROWS)],
             'constraints': value['constraints'] * structured.MAX_ROWS,
             'open_questions': [copy.deepcopy(value['open_questions'][0]) for _ in range(structured.MAX_ROWS)]}
    value['open_questions'][-1]['PRIVATE_TAIL_COLUMN'] = 'PRIVATE_TAIL_VALUE'
    original = copy.deepcopy(value)
    assert structured.normalize('intake', value).dropped == 0
    assert structured.canonical_field_diagnostic('intake', value) is None
    with pytest.raises(PipelineError) as caught:
        submit(pipe, value)
    assert detail(caught.value) == {'code': 'canonical_field_errors', 'issues': [{
        'path': ['structured', 'open_questions', structured.MAX_ROWS - 1], 'rule': 'extra_field',
        'expected_type': 'defined_field', 'actual_type': 'string'}]}
    assert 'PRIVATE_' not in str(caught.value) and value == original
    assert pipe.store.latest_artifact(RUN, 'intake') is None


def test_legacy_prose_keeps_original_optional_normalization_compatibility(pipe):
    value = invalid_intake()
    saved = submit(pipe, value, content='Actual legacy prose is retained exactly.')
    assert pipe.store.read_artifact_text(RUN, 'intake', saved.version) == 'Actual legacy prose is retained exactly.'
    assert 'constraints' not in saved.structured


@pytest.mark.parametrize('change', [
    lambda value: value.update(constraints=None),
    lambda value: value['facts'][0].update(source=None),
    lambda value: value['facts'][0].update(options={}),  # Existing empty-map compatibility.
    lambda value: value['facts'][0].update(PRIVATE_UNUSED=None),
    lambda value: value.update(PRIVATE_UNUSED=[]),
    lambda value: value.update(constraints=[None]),
    lambda value: value.update(facts={}),
    lambda value: value['facts'][0].update(value=False),
    lambda value: value['facts'][0].update(value='x' * 4001),
    lambda value: value.update(constraints=['same'] * 201),
    lambda value: value['facts'][0].update(PRIVATE_EXTRA='private'),
])
def test_feedback_does_not_change_the_original_intake_acceptance_set(pipe, change):
    value = valid_intake()
    change(value)
    normalized = structured.normalize('intake', value)
    try:
        if normalized.dropped:
            raise ValueError('Original dropped input')
        structured.validate_canonical_input('intake', value)
        structured.canonical_document('intake', normalized.data)
        accepted = True
    except ValueError:
        accepted = False
    if accepted:
        assert structured.canonical_field_diagnostic('intake', value) is None
        assert submit(pipe, value).structured == normalized.data
    else:
        with pytest.raises(PipelineError):
            submit(pipe, value)
        assert pipe.store.latest_artifact(RUN, 'intake') is None


def test_contract_descriptions_use_source_types_without_changing_wire_schemas():
    description = registry.REGISTRY['write_stage'].description
    assert structured.intake_submission_contract() in description
    assert structured.intake_submission_contract() in structured.brief('intake')
    assert 'constraints is an array of strings' in description and 'including blocks, are strings' in description
    assert str(structured.MAX_CELL) in description
    assert ', '.join(structured.QUESTION_COLUMNS) in description
    assert registry.REGISTRY['write_stage'].input_schema['properties']['structured']['type'] == 'object'
    assert 'properties' not in registry.REGISTRY['write_stage'].input_schema['properties']['structured']
    first = registry.orchestrator_profile_tools('initial')
    bind = next(tool for tool in first if tool['name'] == 'propose_task_interpretation')
    assert 'Creative produce/revise requests use image_use=not_applicable' in bind['description']
    assert bind['input_schema']['properties']['image_use']['anyOf'][1]['enum'] == ['not_applicable']
    assert 'omit task_interpretation' in registry.REGISTRY['observe_images'].description
    clause = registry.REGISTRY['submit_stage'].description
    assert 'satisfies/violates/amend_clause' in clause and 'User project condition key or id is a different namespace' in clause


def test_mock_http_bound_creative_observation_and_single_feedback_then_legal_save(scene, monkeypatch):
    store, pipeline, settings = scene[1], Pipeline(scene[1]), scene[5]
    connections, _ = configure(scene)
    image_route = frozen(scene, connections)
    text = '整理当前商品图片的需求与可见事实，仅保存需求，不出方向或媒体。'
    interpretation = {'task': {'quote': text, 'mode': 'produce', 'deliverable': 'intake',
                              'format': 'photo', 'allowed_stages': ['intake'], 'pause_after': False},
                      'constraints': []}
    wire = {**copy.deepcopy(interpretation), 'image_use': 'not_applicable',
            'video_use': 'not_applicable', 'reuse_artifact': 'not_applicable'}
    value = valid_intake()
    bad = invalid_intake()
    replies = [reply(('propose_task_interpretation', wire)),
               reply(('prepare_creative_context', {'intent': 'new', 'format': 'photo', 'deliverable': 'intake'})),
               reply(('observe_images', {'images': [{'name': 'fixture.png', 'purpose': 'product'}]})),
               observer_reply(answer(first=1), image_route[1].progress_stream),
               reply(('write_stage', {'stage': 'intake', 'content': '', 'summary': 'First attempt', 'structured': bad})),
               reply(('write_stage', {'stage': 'intake', 'content': '', 'summary': 'Corrected input', 'structured': value})),
               reply(text='需求已保存，尚未创作方向或生成媒体。')]
    seen = transport(monkeypatch, replies)
    context = conversation_input.display_context(store, pipeline, HTTP_RUN,
        {'pending': None, 'questions': [], 'interaction': None})
    receipt = conversation_input.receive(store, HTTP_RUN, text, 'intake-native-contract', context)
    activity, visual_calls, original_binding = [], [], []
    def image_observer(items, emit, cancel):
        visual_calls.append(copy.deepcopy(items))
        return observe(store, settings, HTTP_RUN, items, endpoint=None, emit=emit,
                       check_cancel=cancel, freeze_route=lambda: image_route)
    def emit(event):
        activity.append(event)
        if event['type'] == 'agent_tool' and event.get('tool') == 'propose_task_interpretation' and event.get('ok'):
            original_binding.extend(line for line in (store.run_dir(HTTP_RUN) / 'events.jsonl').read_bytes().splitlines()
                                    if json.loads(line).get('kind') == 'user_task_interpretation_recorded')
    orch = Orchestrator(store, pipeline, settings,
        DeepSeekClient('synthetic-only', reasoning_effort='low', progress_stream=False),
        Ledger(store, settings), HTTP_RUN, image_observer=image_observer, on_event=emit)
    outcome = orch.send(text, user_input_id=receipt['message_id'])
    assert outcome.stopped_because == 'done' and outcome.deliverable['status'] == 'complete'
    assert len(seen) == 7 and not replies  # Six explicit root responses, one independent image response.
    payloads = [json.loads(request.content) for request in seen]
    assert all(tool['function']['strict'] for tool in payloads[0]['tools'])
    creative_observe = next(tool['function'] for tool in payloads[2]['tools'] if tool['function']['name'] == 'observe_images')
    assert 'strict' not in creative_observe and creative_observe['parameters']['required'] == ['images']
    observation_arguments = [json.loads(call['function']['arguments'])
                             for message in payloads[4]['messages'] for call in message.get('tool_calls', [])
                             if call['function']['name'] == 'observe_images']
    assert observation_arguments == [{'images': [{'name': 'fixture.png', 'purpose': 'product'}]}]
    write_results = [event for event in activity if event['type'] == 'agent_tool' and event.get('tool') == 'write_stage']
    assert [event['ok'] for event in write_results] == [False, True]
    report = detail(write_results[0]['result'])
    assert [issue['path'] for issue in report['issues']] == [
        ['structured', 'constraints', 0], ['structured', 'open_questions', 0, 'blocks']]
    next_request_errors = [message['content'] for message in payloads[5]['messages']
                           if message['role'] == 'tool' and 'canonical_field_errors' in message['content']]
    assert len(next_request_errors) == 1 and detail(next_request_errors[0]) == report
    assert len(original_binding) == 1
    current_bindings = [line for line in (store.run_dir(HTTP_RUN) / 'events.jsonl').read_bytes().splitlines()
                        if json.loads(line).get('kind') == 'user_task_interpretation_recorded']
    assert current_bindings == original_binding
    assert conversation_input.interpretation_for(store, HTTP_RUN, receipt['message_id'])['task'] == interpretation['task']
    assert len(visual_calls) == len(saved_observations(store, HTTP_RUN)) == 1
    assert len(store.list_artifacts(HTTP_RUN, 'intake')) == 1
    assert store.latest_artifact(HTTP_RUN, 'intake').structured == value
    assert not store.list_approvals(HTTP_RUN)
    assert all(store.latest_artifact(HTTP_RUN, stage) is None for stage in STAGES if stage != 'intake')
    assert len(Ledger(store, settings).entries(HTTP_RUN)) == 7
