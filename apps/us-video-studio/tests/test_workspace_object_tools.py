"""A bound read-only question uses exact deterministic objects, no method body."""
import json

import pytest

from fakes import ScriptedModel, final_turn, tool_turn
from studio.models import PipelineError
from studio.tools.registry import orchestrator_profile_tools
from studio.tools.workspace import Workspace
from test_orchestration import env, make_orchestrator, seed_intake, RUN


def test_exact_object_read_and_diff_are_readonly_and_reject_stale_hash(env, tmp_path):
    store, pipe, ledger = env
    old = seed_intake(pipe, 'Original fact')
    new = seed_intake(pipe, 'Changed fact')
    target = lambda item: {'kind': 'artifact', 'run_id': RUN, 'stage': item.stage,
                          'version': item.version, 'artifact_sha256': item.sha256}
    orch = make_orchestrator(store, pipe, ledger, ScriptedModel([]), tmp_path)
    workspace = Workspace(store, pipe, RUN, 'orchestrator')
    rows = store.read_events(RUN)
    result = json.loads(orch._execute('compare_workspace_objects',
        {'left': target(old), 'right': target(new)}, workspace))
    assert 'Original fact' in result['diff'] and 'Changed fact' in result['diff']
    assert result['left']['status'] == 'history' and result['right']['status'] == 'current'
    with pytest.raises(PipelineError):
        orch._execute('read_workspace_object', {'target': {**target(new), 'artifact_sha256': '0' * 64}}, workspace)
    assert store.read_events(RUN) == rows and not ledger.entries(RUN)


def test_readonly_interpretation_exposes_exact_tools_without_creative_method_injection(env, tmp_path, monkeypatch):
    store, pipe, ledger = env
    item = seed_intake(pipe, 'Original fact')
    target = {'kind': 'artifact', 'run_id': RUN, 'stage': item.stage, 'version': item.version,
              'artifact_sha256': item.sha256}
    text = '只读取已保存需求，不改稿。'
    model = ScriptedModel([
        tool_turn('interpret', 'propose_task_interpretation', {'task': {'quote': text, 'mode': 'status',
            'deliverable': 'follow_up', 'format': 'none', 'allowed_stages': [], 'pause_after': True},
            'constraints': [], 'reuse_artifact': 'not_applicable',
            'video_use': 'not_applicable', 'image_use': 'not_applicable'}),
        tool_turn('read', 'read_workspace_object', {'target': target}), final_turn('原需求内容为 Original fact。')],
        legacy_task_contracts=False)
    orch = make_orchestrator(store, pipe, ledger, model, tmp_path)
    monkeypatch.setattr(orch.learning, 'prepare_context', lambda *a, **k: {
        'text': 'FULL_CREATIVE_METHOD_MUST_NOT_ENTER', 'snapshot_sha256': 'f' * 64, 'evidence_ref': 'fixture'})
    from studio.conversation_input import receive, display_context
    shown = display_context(store, pipe, RUN, {'pending': None, 'questions': [], 'interaction': None})
    receipt = receive(store, RUN, text, 'read-exact-object', shown)
    result = orch.send(text, user_input_id=receipt['message_id'])
    assert result.stopped_because == 'done'
    assert len(model.calls) == 3
    assert 'read_workspace_object' in model.calls[1]['tool_names']
    assert 'compare_workspace_objects' in {tool['name'] for tool in orchestrator_profile_tools('read_only')}
    assert all('FULL_CREATIVE_METHOD_MUST_NOT_ENTER' not in call['system'] for call in model.calls)
    assert len(store.list_artifacts(RUN)) == 1


def test_role_receives_each_selected_annotation_location_without_collapsing(env, monkeypatch):
    from studio.conversation_input import receive, accept, start_execution
    from studio.learning import LearningService
    from studio.task_context import assemble_role_task
    store, pipe, _ = env
    seed_intake(pipe)
    receipt = receive(store, RUN, '按两条准确批注处理', 'annotation-fixture')
    accept(store, RUN, receipt)
    start_execution(store, RUN, receipt)
    for identity, second in [('first', 1.5), ('second', 6.2)]:
        store.append_event(RUN, 'workspace_annotation_saved', annotation={
            'annotation_id': identity, 'target': {'kind': 'media', 'run_id': RUN,
                'attempt_id': 'same-fixture-media', 'output_sha256': 'f' * 64},
            'text': identity + ' exact comment', 'at_seconds': second,
            'region': {'x': .1, 'y': .2, 'width': .3, 'height': .4}})
    store.append_event(RUN, 'workspace_annotation_group', message_id=receipt['message_id'],
        annotation_ids=['first', 'second'])
    learning = LearningService(store)
    monkeypatch.setattr(learning, 'skill_catalog', lambda *a, **k: {})
    context = assemble_role_task(store, pipe, learning, RUN,
        Workspace(store, pipe, RUN, 'director', target_stage='direction'), 'direction', '责任角色任务',
        method_context={'text': 'fixture'}, user_input_id=receipt['message_id'])
    assert '"at_seconds": 1.5' in context.seed and '"at_seconds": 6.2' in context.seed
    assert 'first exact comment' in context.seed and 'second exact comment' in context.seed
    annotations = [row for row in context.sources if row['kind'] == 'workspace_annotation']
    assert [row['annotation_id'] for row in annotations] == ['first', 'second']
    assert all(len(row['sha256']) == 64 for row in annotations)
