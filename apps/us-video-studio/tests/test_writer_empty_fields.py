"""Exercise optional-field omissions through real adapters and dispatcher, offline."""
import copy
import json

import pytest

from studio.agents.client import ModelError, SafePreflightError, run_agent_loop
from studio.agents.deepseek_client import DeepSeekClient, _matches_strict_schema
from studio.tools.dispatch import Dispatcher
from studio.tools.registry import tools_for
from test_deepseek_client import transport
from test_writing_tool_schema import seed, stream_reply, submission, wire


@pytest.mark.parametrize('format', ['video', 'photo'])
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('omission', ['questions', 'all_auxiliary'])
def test_empty_fields_preserve_actual_replay_and_all_script_content(monkeypatch, format, streaming, omission):
    args = submission(format)
    args['structured'].pop('open_questions')
    if omission == 'all_auxiliary':
        for field in ('content', 'summary', 'evidence', 'limitations'):
            args.pop(field)
        args['structured'].pop('assumptions')
        for row in args['structured']['segments']:
            row.pop('file')
    original = copy.deepcopy(args)
    data = wire(args)
    seen = transport(monkeypatch, [stream_reply(data) if streaming else data])
    client = DeepSeekClient('synthetic-only')
    method = client.complete_with_progress if streaming else client.complete
    turn = method(model='deepseek-flash', system='Synthetic only', messages=[], tools=tools_for('writer', format=format))
    assert len(seen) == 1
    completed = turn.tool_calls[0].input
    assert completed['structured']['segments'][0]['picture'] == original['structured']['segments'][0]['picture']
    assert completed['structured']['caption'] == original['structured']['caption']
    assert completed['structured']['open_questions'] == []
    assert '模型未提供' in completed['limitations'][0] and '不代表已确认' in completed['limitations'][0]
    schema = next(t['input_schema'] for t in tools_for('writer', format=format) if t['name'] == 'submit_stage')
    assert _matches_strict_schema(completed, schema)
    raw_call = next(b for b in turn.raw_blocks if b['type'] == 'tool_use')
    assert raw_call['input'] == original and args == original
    assert raw_call['input'] is not completed
    assert ['structured', 'open_questions'] in turn.response_normalizations[0]['defaulted_paths']
    assert 'picture' not in json.dumps(turn.response_normalizations)
    assert 'SYNTHETIC_PRIVATE_REASONING' not in json.dumps(turn.response_normalizations)


@pytest.mark.parametrize('field', ['tc', 'picture', 'line', 'sound', 'duration', 'caption', 'total_duration',
                                    'shot_id', 'character_ids', 'scene_id'])
def test_optional_omission_does_not_hide_missing_core_script_fields(monkeypatch, field):
    args = submission()
    args['structured'].pop('open_questions')
    if field in {'caption', 'total_duration'}:
        args['structured'].pop(field)
    else:
        args['structured']['segments'][0].pop(field)
    seen = transport(monkeypatch, [wire(args, leading=True)])
    executed = []
    with pytest.raises(ModelError) as failure:
        run_agent_loop(DeepSeekClient('synthetic-only'), model='deepseek-flash', system='Synthetic only',
                       messages=[], tools=tools_for('writer'), execute_tool=lambda *a: executed.append(a), max_rounds=3)
    assert len(seen) == 1 and executed == []
    assert failure.value.response_diagnostic['schema_path'][-1] == field


@pytest.mark.parametrize('change', ['null', 'wrong_type', 'extra', 'invalid_owner', 'missing_reason', 'unknown_tool'])
def test_completion_keeps_type_question_and_capability_checks(monkeypatch, change):
    args = submission()
    args['structured'].pop('open_questions')
    if change == 'null':
        args['structured']['open_questions'] = None
    elif change == 'wrong_type':
        args['structured']['assumptions'] = 'PRIVATE_VALUE'
    elif change == 'extra':
        args['structured']['PRIVATE_FIELD'] = 'PRIVATE_VALUE'
    elif change in {'invalid_owner', 'missing_reason'}:
        args['structured']['open_questions'] = [dict(question='Question', blocks='Adoption',
            owner='PRIVATE_OWNER' if change == 'invalid_owner' else 'team', timing='now', reason='Check')]
        if change == 'missing_reason':
            args['structured']['open_questions'][0].pop('reason')
    data = wire(args, leading=True)
    if change == 'unknown_tool':
        data['choices'][0]['message']['tool_calls'].append({'id': 'private-call', 'type': 'function',
            'function': {'name': 'approve_stage', 'arguments': '{}'}})
    seen = transport(monkeypatch, [data])
    executed = []
    with pytest.raises(ModelError) as failure:
        run_agent_loop(DeepSeekClient('synthetic-only'), model='deepseek-flash', system='Synthetic only',
                       messages=[], tools=tools_for('writer'), execute_tool=lambda *a: executed.append(a), max_rounds=3)
    assert len(seen) == 1 and executed == []
    assert 'PRIVATE' not in json.dumps(failure.value.response_diagnostic) + failure.value.public_message


@pytest.mark.parametrize('audit_failure', [False, True])
def test_dispatcher_saves_missing_questions_once_with_visible_gap_and_safe_accounting(tmp_path, monkeypatch, audit_failure):
    store, pipe, settings, ledger, run_id = seed(tmp_path, monkeypatch)
    before = {s: store.latest_artifact(run_id, s).sha256 for s in ('intake', 'direction', 'material_plan')}
    args = submission()
    args['structured'].pop('open_questions')
    seen = transport(monkeypatch, [stream_reply(wire(args))])
    if audit_failure:
        original_append = store.append_event
        def append(run_id, kind, **fields):
            if kind == 'agent_response_normalized':
                raise OSError('PRIVATE_OS_ERROR')
            return original_append(run_id, kind, **fields)
        monkeypatch.setattr(store, 'append_event', append)
    progress = []
    dispatcher = Dispatcher(store, pipe, settings, DeepSeekClient('synthetic-only'), ledger, run_id,
                            on_event=progress.append)
    if audit_failure:
        with pytest.raises(SafePreflightError, match='格式处理记录'):
            dispatcher.dispatch('writing', 'Only write the current script')
        assert store.latest_artifact(run_id, 'writing') is None
    else:
        result = dispatcher.dispatch('writing', 'Only write the current script')
        assert result.submitted == 'writing-v1'
        artifact = store.latest_artifact(run_id, 'writing')
        assert [{k: v for k, v in row.items() if k != 'shot_id'}
                for row in artifact.structured['segments']] == [
                    {k: v for k, v in row.items() if k != 'shot_id'} for row in args['structured']['segments']]
        assert all(row['shot_id'] for row in artifact.structured['segments'])
        assert artifact.structured['caption'] == args['structured']['caption']
        assert '模型未提供未决事项清单' in artifact.limitations[0]
        assert pipe.stage_state(run_id, 'writing').approval is None
        normalized = [e for e in store.read_events(run_id) if e['kind'] == 'agent_response_normalized']
        assert len(normalized) == 1 and normalized[0]['call_id'] == ledger.entries(run_id)[0].call_id
        assert normalized[0]['diagnostic']['defaulted_paths'] == [['structured', 'open_questions']]
        assert any(e['type'] == 'agent_submission_normalized' for e in progress)
        assert not [e for e in store.read_events(run_id) if e['kind'] == 'agent_response_rejected']
    assert len(seen) == 1 and len(ledger.entries(run_id)) == 1
    assert ledger.entries(run_id)[0].usage_status == 'known'
    assert ledger.entries(run_id)[0].output_tokens == 13602
    assert before == {s: store.latest_artifact(run_id, s).sha256 for s in before}
    assert pipe.stage_state(run_id, 'direction').approval.selected_option_id == 'A'
    assert pipe.stage_state(run_id, 'writing').open_attempt is None
