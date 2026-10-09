"""Writer request assembly regressions; synthetic/Mock HTTP, no real models."""
import copy
import json

import pytest

from studio.agents.deepseek_client import DeepSeekClient, _matches_strict_schema
from studio.agents.roles import ROLES
from studio.models import Run
from studio.tools.dispatch import Dispatcher
from studio.tools.registry import tools_for
from test_deepseek_client import transport
from test_writing_tool_schema import seed, submission, wire


RUN = Run('writer-envelope-fixture', 'Synthetic fixture', 'Write the current script.',
          'US', '2026-10-08T00:00:00Z')
CONTRACT_HEADING = '# 完整写稿的提交层级'


def submission_example(system):
    section = system.split(CONTRACT_HEADING, 1)[1]
    return json.loads(section.split('```json\n', 1)[1].split('\n```', 1)[0])


def submission_schema(tools):
    return next(tool['input_schema'] for tool in tools if tool['name'] == 'submit_stage')


@pytest.mark.parametrize('format', ['video', 'photo'])
def test_full_writer_prompt_envelope_matches_the_actual_tool_schema(format):
    tools = tools_for('writer', format=format)
    before = copy.deepcopy(tools)
    system = ROLES['writer'].system(RUN, format=format)
    example = submission_example(system)
    schema = submission_schema(tools)

    assert _matches_strict_schema(example, schema)
    assert set(example) == set(schema['properties'])
    assert set(example['structured']) == set(schema['properties']['structured']['properties'])
    assert 'summary' in example and 'summary' not in example['structured']
    assert '`summary` 是提交摘要，与 `structured` 同级' in system
    assert '外层提交说明另填' in system
    assert '实际段数遵守本次任务' in system
    assert '不是可提交稿' in system
    row = example['structured']['segments'][0]
    assert row['shot_id'] == '' and row['file'] == ''
    assert '`shot_id=""`' in system and '`file=""`' in system
    assert '省略身份' not in system and '没有就先别填' not in system
    if format == 'photo':
        assert row['page_id'] == '' and '`page_id=""`' in system
        assert 'duration' not in row and 'total_duration' not in example['structured']
    assert tools_for('writer', format=format) == before


@pytest.mark.parametrize('role,local_revision', [
    ('writer', True), ('director', False), ('material', False), ('orchestrator', False),
])
def test_full_submission_example_does_not_expand_other_role_or_local_edit_scope(role, local_revision):
    for format in ('video', 'photo'):
        system = ROLES[role].system(RUN, format=format, local_revision=local_revision)
        assert CONTRACT_HEADING not in system


@pytest.mark.parametrize('format', ['video', 'photo'])
def test_dispatcher_sends_example_at_the_same_levels_as_its_frozen_strict_tool(tmp_path, monkeypatch, format):
    store, pipe, settings, ledger, run_id = seed(tmp_path, monkeypatch)
    store.append_event(run_id, 'creative_context_selected', selection={
        'intent': 'continue', 'format': format, 'tags': [], 'case_ids': None})
    args = submission(format)
    seen = transport(monkeypatch, [wire(args)])
    client = DeepSeekClient('synthetic-only', reasoning_effort='low', progress_stream=False)
    result = Dispatcher(store, pipe, settings, client, ledger, run_id).dispatch(
        'writing', 'Write the complete current script.')

    assert len(seen) == 1 and result.submitted == 'writing-v1'
    payload = json.loads(seen[0].content)
    tool = next(tool['function'] for tool in payload['tools']
                if tool['function']['name'] == 'submit_stage')
    example = submission_example(payload['messages'][0]['content'])
    assert payload['stream'] is False and tool['strict'] is True
    assert _matches_strict_schema(example, tool['parameters'])
    assert 'summary' in example and 'summary' not in example['structured']
    # The outline's single sample row must not set the actual number of shots/pages.
    artifact = store.latest_artifact(run_id, 'writing')
    assert len(artifact.structured['segments']) == len(args['structured']['segments'])
