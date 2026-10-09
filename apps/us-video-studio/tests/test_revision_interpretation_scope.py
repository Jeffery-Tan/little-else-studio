"""Exact input-bound editing limits; real temporary journals, no providers."""
import copy

import pytest

from fakes import fixture_task_interpretation
from studio.conversation_input import (accept, interpreted_task_constraints, interpretation_for,
    receive, record_task_interpretation, start_execution)
from studio.models import PipelineError
from studio.revision_scope import plan_revision
from test_revision_scope import RUN, env, plan_args


TEXT = '第二句只需要换个说法，整套方案照旧。'
PATH = ['segments', 1, 'line']


def source(store, text=TEXT, *, target=None):
    receipt = receive(store, RUN, text, f'edit-{len(store.read_events(RUN))}', feedback_target=target)
    accept(store, RUN, receipt)
    start_execution(store, RUN, receipt)
    return receipt


def meaning(receipt, *, mode='local', paths=None, stage='writing'):
    proposal = fixture_task_interpretation(receipt['text'], intent='edit', deliverable='revision')
    proposal['task']['allowed_stages'] = [stage]
    proposal['constraints'] = [{'quote': receipt['text'], 'key': 'content.edit_scope', 'value': mode,
        'operation': 'add', 'polarity': 'require', 'intent': 'instruction',
        'scope': {'lifetime': 'turn', 'stages': [stage], 'paths': paths if paths is not None else [PATH],
                  'quote': receipt['text']},
        'supersedes': [], 'conflict_with': [], 'ambiguity': ''}]
    return proposal


def plan(store, pipe, receipt, limits=None, **kwargs):
    return plan_revision(store, pipe, RUN, plan_args(store, quote=receipt['text'], **kwargs),
        turn_id='exact-edit-turn', user_message=receipt['text'], constraints=limits)


def test_unbound_local_feedback_does_not_authorize_unrestricted_rework(env):
    store, pipe, _, _ = env
    receipt = source(store)
    before = store.read_events(RUN)
    with pytest.raises(PipelineError, match='编辑范围'):
        plan(store, pipe, receipt, mode='rework')
    assert store.read_events(RUN) == before


def test_interpreted_local_scope_cannot_expand_on_first_revision_plan(env):
    store, pipe, _, _ = env
    receipt = source(store)
    saved = record_task_interpretation(store, RUN, receipt, meaning(receipt))
    limits = interpreted_task_constraints(receipt, saved)
    assert limits['local_only'] is True
    with pytest.raises(PipelineError, match='局部|扩大'):
        plan(store, pipe, receipt, limits, mode='rework')
    with pytest.raises(PipelineError, match='编辑范围'):
        plan(store, pipe, receipt, limits, path=['segments', 0, 'line'])
    good = plan(store, pipe, receipt, limits, path=PATH)
    assert good['mode'] == 'local' and good['fields'][0]['path'] == PATH
    assert store.latest_artifact(RUN, 'writing').version == 1
    assert good['source_message_id'] == receipt['message_id']


def test_missing_scope_is_correctable_before_first_binding_without_new_user_input(env):
    store, _, _, _ = env
    receipt = source(store)
    proposal = meaning(receipt)
    proposal['constraints'] = []
    before = store.read_events(RUN)
    with pytest.raises(PipelineError, match='content.edit_scope'):
        record_task_interpretation(store, RUN, receipt, proposal)
    assert interpretation_for(store, RUN, receipt['message_id']) is None
    assert store.read_events(RUN) == before
    saved = record_task_interpretation(store, RUN, receipt, meaning(receipt))
    count = len(store.read_events(RUN))
    assert record_task_interpretation(store, RUN, receipt, meaning(receipt))['id'] == saved['id']
    assert len(store.read_events(RUN)) == count


def test_content_paths_do_not_grant_editing_authority(env):
    store, _, _, _ = env
    receipt = source(store)
    proposal = meaning(receipt)
    proposal['constraints'][0].update(key='voice.register', value='conversational')
    with pytest.raises(PipelineError, match='content.edit_scope'):
        record_task_interpretation(store, RUN, receipt, proposal)
    proposal['task'].update(mode='produce', deliverable='writing')
    saved = record_task_interpretation(store, RUN, receipt, proposal)
    assert interpreted_task_constraints(receipt, saved)['local_only'] is False


@pytest.mark.parametrize('change', ['quote', 'scope_quote', 'path', 'project', 'mode',
                                  'prefer', 'reported', 'ambiguity', 'stage', 'duplicate'])
def test_invalid_edit_limits_never_write_an_interpretation(env, change):
    store, _, _, _ = env
    receipt = source(store)
    proposal = meaning(receipt)
    row = proposal['constraints'][0]
    if change == 'quote': row['quote'] = 'invented source'
    elif change == 'scope_quote': row['scope']['quote'] = ''
    elif change == 'path': row['scope']['paths'] = [['segments', 99, 'line']]
    elif change == 'project': row['scope']['lifetime'] = 'project'
    elif change == 'mode': proposal['task']['mode'] = 'produce'
    elif change == 'prefer': row['polarity'] = 'prefer'
    elif change == 'reported': row['intent'] = 'reported'
    elif change == 'ambiguity': row['ambiguity'] = 'Which line?'
    elif change == 'stage': row['scope']['stages'] = ['direction']
    else: proposal['constraints'].append(copy.deepcopy(row))
    before = store.read_events(RUN)
    with pytest.raises(PipelineError):
        record_task_interpretation(store, RUN, receipt, proposal)
    assert store.read_events(RUN) == before


def test_full_rework_is_frozen_once_without_new_approval(env):
    store, pipe, _, _ = env
    receipt = source(store, '重新构思整个脚本。')
    proposal = meaning(receipt, mode='rework', paths=[])
    approvals = store.list_approvals(RUN)
    saved = record_task_interpretation(store, RUN, receipt, proposal)
    limits = interpreted_task_constraints(receipt, saved)
    scope = plan(store, pipe, receipt, limits, mode='rework')
    assert scope['mode'] == 'rework' and scope['version'] == 1
    assert store.list_approvals(RUN) == approvals
    assert len([e for e in store.read_events(RUN) if e['kind'] == 'user_message_received']) == 1


def test_edit_modes_are_bound_to_their_own_stage_without_global_local_permission(env):
    store, pipe, _, _ = env
    receipt = source(store, '重新构思方向，脚本第二句换个说法。')
    proposal = meaning(receipt)
    proposal['task']['allowed_stages'] = ['direction', 'writing']
    proposal['constraints'].append(meaning(receipt, mode='rework', paths=[], stage='direction')['constraints'][0])
    saved = record_task_interpretation(store, RUN, receipt, proposal)
    limits = interpreted_task_constraints(receipt, saved)
    assert limits['local_only'] is False
    direction = plan(store, pipe, receipt, limits, stage='direction', mode='rework')
    writing = plan(store, pipe, receipt, limits, path=PATH)
    assert direction['mode'] == 'rework' and direction['fields'] == []
    assert writing['mode'] == 'local' and writing['fields'][0]['path'] == PATH
    with pytest.raises(PipelineError, match='扩大'):
        plan(store, pipe, receipt, limits, mode='rework')
    assert len(saved['edit_scopes']) == 2


@pytest.mark.parametrize('change', ['version', 'source', 'limits'])
def test_stale_or_forged_scope_projection_cannot_start_revision(env, change):
    store, pipe, _, _ = env
    receipt = source(store)
    saved = record_task_interpretation(store, RUN, receipt, meaning(receipt))
    limits = interpreted_task_constraints(receipt, saved)
    if change == 'version':
        from test_revision_scope import write
        write(pipe, 'writing', store.latest_artifact(RUN, 'writing').structured)
    elif change == 'source': limits['message_id'] = 'other-source'
    else: limits['edit_scopes'][0]['paths'] = [['segments', 0, 'line']]
    before = store.read_events(RUN)
    with pytest.raises(PipelineError):
        plan(store, pipe, receipt, limits, path=PATH)
    assert store.read_events(RUN) == before


def test_exact_ui_selected_field_reuses_existing_binding(env):
    store, pipe, _, _ = env
    base = store.latest_artifact(RUN, 'writing')
    target = {'stage': 'writing', 'version': base.version, 'artifact_sha256': base.sha256, 'path': PATH}
    receipt = source(store, '换个说法。', target=target)
    proposal = meaning(receipt)
    proposal['constraints'] = []
    saved = record_task_interpretation(store, RUN, receipt, proposal)
    good = plan(store, pipe, receipt, interpreted_task_constraints(receipt, saved), path=PATH)
    assert good['fields'][0]['path'] == PATH
    with pytest.raises(PipelineError):
        plan(store, pipe, receipt, interpreted_task_constraints(receipt, saved), mode='rework')


def test_reconstructed_scope_requires_original_source_and_condition_fingerprint(env):
    store, pipe, _, _ = env
    receipt = source(store)
    saved = record_task_interpretation(store, RUN, receipt, meaning(receipt))
    # A syntactically readable legacy/changed record cannot fabricate a broader
    # saved scope by changing its derived metadata while keeping the condition.
    changed = copy.deepcopy(saved)
    changed['edit_scopes'][0]['paths'] = [['segments', 0, 'line']]
    store.append_event(RUN, 'user_task_interpretation_recorded',
                       source_message_id=receipt['message_id'], interpretation=changed)
    before = store.read_events(RUN)
    with pytest.raises(PipelineError, match='首次原话条件'):
        plan(store, pipe, receipt, interpreted_task_constraints(receipt, changed), path=['segments', 0, 'line'])
    assert store.read_events(RUN) == before


def test_local_interpretation_is_preserved_as_read_only_after_base_changes(env):
    store, pipe, _, _ = env
    receipt = source(store)
    saved = record_task_interpretation(store, RUN, receipt, meaning(receipt))
    from test_revision_scope import write
    write(pipe, 'writing', store.latest_artifact(RUN, 'writing').structured)
    assert interpretation_for(store, RUN, receipt['message_id'])['edit_scopes'] == saved['edit_scopes']
    count = len(store.read_events(RUN))
    assert record_task_interpretation(store, RUN, receipt, meaning(receipt))['id'] == saved['id']
    assert len(store.read_events(RUN)) == count
    with pytest.raises(PipelineError, match='基线'):
        plan(store, pipe, receipt, interpreted_task_constraints(receipt, saved), path=PATH)


def test_main_loop_reads_binds_and_preserves_exact_local_scope_through_dispatch(env):
    from fakes import final_turn, tool_turn
    from studio.agents.orchestrator import Orchestrator
    from test_revision_scope import PlannedModel, latest_plan, patch
    store, pipe, settings, ledger = env
    originals = {p: p.read_bytes() for p in (store.run_dir(RUN) / 'artifacts').glob('*-v1.*')}
    correct = meaning({'text': TEXT})
    incomplete = copy.deepcopy(correct)
    incomplete['constraints'] = []
    model = PlannedModel([
        tool_turn('read-exact', 'read_artifact', {'stage': 'writing', 'version': 1}),
        tool_turn('missing-scope', 'propose_task_interpretation', incomplete),
        tool_turn('bind-scope', 'propose_task_interpretation', correct),
        tool_turn('choose', 'prepare_creative_context',
                  {'intent': 'edit', 'format': 'video', 'deliverable': 'revision'}),
        tool_turn('too-wide', 'plan_revision', plan_args(store, mode='rework', quote=TEXT)),
        tool_turn('exact-plan', 'plan_revision', plan_args(store, path=PATH, quote=TEXT)),
        lambda: tool_turn('dispatch', 'dispatch',
                         {'stage': 'writing', 'brief': TEXT, 'plan_id': latest_plan(store)}),
        tool_turn('local-save', 'submit_patch', patch(PATH, 'A natural second line')),
        final_turn('Second line revised; the rest is preserved.'),
    ], legacy_task_contracts=False)
    result = Orchestrator(store, pipe, settings, model, ledger, RUN).send(TEXT)
    assert [failure['tool'] for failure in result.failed_tools] == ['propose_task_interpretation', 'plan_revision']
    assert result.deliverable['status'] == 'complete'
    after = store.latest_artifact(RUN, 'writing')
    before = store.read_artifact(RUN, 'writing', 1)
    expected = copy.deepcopy(before.structured)
    expected['segments'][1]['line'] = 'A natural second line'
    assert after.structured == expected
    assert all(p.read_bytes() == data for p, data in originals.items())
    assert len([e for e in store.read_events(RUN) if e['kind'] == 'user_task_interpretation_recorded']) == 1
    assert len([e for e in store.read_events(RUN) if e['kind'] == 'user_message_received']) == 1


def test_main_loop_can_narrow_frozen_rework_to_an_exact_local_patch(env):
    from fakes import final_turn, tool_turn
    from studio.agents.orchestrator import Orchestrator
    from test_revision_scope import PlannedModel, latest_plan, patch
    store, pipe, settings, ledger = env
    text = '重新构思整个脚本。'
    original = store.latest_artifact(RUN, 'writing')
    originals = {p: p.read_bytes() for p in (store.run_dir(RUN) / 'artifacts').glob('*-v1.*')}
    approvals = store.list_approvals(RUN)
    model = PlannedModel([
        tool_turn('read-exact', 'read_artifact', {'stage': 'writing', 'version': 1}),
        tool_turn('bind-rework', 'propose_task_interpretation',
                  meaning({'text': text}, mode='rework', paths=[])),
        tool_turn('choose', 'prepare_creative_context',
                  {'intent': 'edit', 'format': 'video', 'deliverable': 'revision'}),
        tool_turn('narrow-plan', 'plan_revision', plan_args(store, path=PATH, quote=text)),
        lambda: tool_turn('dispatch', 'dispatch',
                         {'stage': 'writing', 'brief': text, 'plan_id': latest_plan(store)}),
        tool_turn('local-save', 'submit_patch', patch(PATH, 'Narrowed authorized revision')),
        final_turn('The exact local revision is saved.'),
        final_turn('The incomplete synthetic task is stopped.'),
        final_turn('The incomplete synthetic task remains stopped.'),
    ], legacy_task_contracts=False)
    result = Orchestrator(store, pipe, settings, model, ledger, RUN).send(text)
    assert result.failed_tools == []
    assert result.deliverable['status'] == 'complete'
    expected = copy.deepcopy(original.structured)
    expected['segments'][1]['line'] = 'Narrowed authorized revision'
    assert store.latest_artifact(RUN, 'writing').structured == expected
    assert store.latest_artifact(RUN, 'writing').version == original.version + 1
    assert all(p.read_bytes() == data for p, data in originals.items())
    assert store.list_approvals(RUN) == approvals
    events = store.read_events(RUN)
    interpreted = [e['interpretation'] for e in events if e['kind'] == 'user_task_interpretation_recorded']
    planned = [e['scope'] for e in events if e['kind'] == 'agent_revision_planned']
    assert len(interpreted) == len(planned) == 1
    assert interpreted[0]['edit_scopes'][0]['mode'] == 'rework'
    assert planned[0]['mode'] == 'local' and planned[0]['fields'][0]['path'] == PATH
    assert planned[0]['source_message_id'] == interpreted[0]['source_message_id']
    writer_call = next(call for call in model.calls if 'submit_patch' in call['tool_names'])
    assert 'submit_stage' not in writer_call['tool_names']
