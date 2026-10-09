"""Offline regressions for merged task, role and storyboard boundaries."""
import pytest

from fakes import ScriptedModel, final_turn, tool_turn, fixture_task_interpretation
from studio.agent_runtime import AgentRuntime
from studio.agent_sessions import AgentSessions
from studio.agents.client import ModelError, NonRetryableStageError, Usage
from studio.agents.orchestrator import Orchestrator
from studio.models import STAGES
from studio.production import draft_records
from studio.tools.dispatch import Dispatcher
from studio.tools.workspace import ToolError, Workspace
from test_revision_scope import env, RUN, QUOTE, image_inputs, plan, production_route


def selected_main(env, text, goal='production_input', **kwargs):
    store, pipe, settings, ledger = env
    main = Orchestrator(store, pipe, settings, ScriptedModel([final_turn('Offline context only')]),
                        ledger, RUN)
    main.send(text, **kwargs)
    workspace = Workspace(store, pipe, RUN, 'orchestrator')
    main._execute('prepare_creative_context', {
        'intent': 'edit', 'format': 'video', 'deliverable': goal, 'production_kind': 'image',
        'task_interpretation': fixture_task_interpretation(text, intent='edit', deliverable=goal),
    }, workspace)
    return main, workspace


@pytest.mark.parametrize('stage', ['direction', 'material_plan', 'writing'])
def test_production_scope_blocks_async_upstream_role_before_session_creation(env, stage):
    store, _, _, _ = env
    main, workspace = selected_main(env, '修当前分镜')
    before = store.read_events(RUN)
    with pytest.raises(ToolError, match='分镜制作'):
        main._execute('start_role', {'stage': stage, 'brief': 'Rewrite upstream'}, workspace)
    assert store.read_events(RUN) == before
    assert not AgentSessions(store, RUN).sessions()


def test_production_scope_cannot_resume_a_waiting_upstream_role(env):
    store, pipe, settings, ledger = env
    old = Dispatcher(store, pipe, settings, ScriptedModel([final_turn('Waiting')]), ledger, RUN).dispatch(
        'writing', QUOTE, revision_scope=plan(env))
    assert old.session_id and AgentSessions(store, RUN).snapshot(old.session_id)['turn'] == 1
    main, workspace = selected_main(env, '修当前分镜')
    before = store.read_events(RUN)
    with pytest.raises(ToolError, match='不能恢复上游'):
        main._execute('resume_role', {'session_id': old.session_id, 'quote': '修当前分镜'}, workspace)
    assert AgentSessions(store, RUN).snapshot(old.session_id)['turn'] == 1
    assert store.read_events(RUN) == before
    assert store.latest_artifact(RUN, 'writing').version == 1


@pytest.mark.parametrize('tool', ['dispatch', 'start_role'])
def test_async_direction_still_requires_real_image_observation(env, tool):
    from studio.materials import accept_upload
    from test_production import png
    store, pipe, settings, ledger = env
    accept_upload(store, RUN, 'synthetic-product.png', png())
    text = '重新创作当前方向'
    main = Orchestrator(store, pipe, settings, ScriptedModel([final_turn('Offline context only')]), ledger, RUN)
    main.send(text)
    workspace = Workspace(store, pipe, RUN, 'orchestrator')
    main._execute('prepare_creative_context', {
        'intent': 'new', 'format': 'video', 'deliverable': 'direction',
        'task_interpretation': fixture_task_interpretation(text, intent='new', deliverable='direction'),
    }, workspace)
    before = store.read_events(RUN)
    with pytest.raises(ToolError, match='observe_images'):
        main._execute(tool, {'stage': 'direction', 'brief': text}, workspace)
    assert store.read_events(RUN) == before
    assert not AgentSessions(store, RUN).sessions()


def test_explicit_local_image_feedback_can_replace_its_input_without_upstream_revision(env):
    store, pipe, settings, ledger = env
    payload, drafts = image_inputs(env)
    text = '只修改这张分镜的手部，其余内容保留'
    before = {stage: store.latest_artifact(RUN, stage) for stage in STAGES}
    approvals = store.list_approvals(RUN)
    target = {'kind': 'production_draft', 'run_id': RUN,
              'draft_id': drafts[0]['draft_id'], 'draft_sha256': drafts[0]['draft_sha256']}
    model = ScriptedModel([
        production_route(),
        tool_turn('replacement', 'prepare_production', {
            **payload, 'request_id': 'local-image-fix', 'shot_id': 'shot-01',
            'supersedes': drafts[0]['draft_id'], 'prompt': 'Correct only the synthetic hand pose'}),
        final_turn('已保存修改后的制作输入')])
    result = Orchestrator(store, pipe, settings, model, ledger, RUN).send(text, feedback_context=target)
    assert not result.failed_tools
    assert result.deliverable['status'] == 'complete'
    assert {stage: store.latest_artifact(RUN, stage) for stage in STAGES} == before
    assert store.list_approvals(RUN) == approvals
    records = draft_records(store, RUN)
    assert records['local-image-fix']['supersedes'] == drafts[0]['draft_id']
    assert records['local-image-fix']['design_binding'] == drafts[0]['design_binding']
    assert records[drafts[1]['draft_id']] == drafts[1]
    assert not any(e['kind'] in {'agent_revision_planned', 'agent_task_prepared', 'execution_authorized'}
                   for e in store.read_events(RUN))


def test_a_selected_upstream_field_cannot_be_reclassified_as_production_feedback(env):
    store, pipe, settings, ledger = env
    source = store.latest_artifact(RUN, 'writing')
    text = '只修改这句，其他内容保留'
    main = Orchestrator(store, pipe, settings, ScriptedModel([final_turn('Offline context only')]), ledger, RUN)
    main.send(text, feedback_context={'kind': 'writing', 'run_id': RUN, 'stage': 'writing',
        'version': source.version, 'artifact_sha256': source.sha256,
        'path': ['segments', 0, 'line'], 'quote': source.structured['segments'][0]['line']})
    with pytest.raises(ToolError, match='局部修改'):
        main._execute('prepare_creative_context', {
            'intent': 'edit', 'format': 'video', 'deliverable': 'production_input', 'production_kind': 'image',
            'task_interpretation': fixture_task_interpretation(text, intent='edit', deliverable='production_input'),
        }, Workspace(store, pipe, RUN, 'orchestrator'))
    assert not main._scope_selected
    assert store.latest_artifact(RUN, 'writing') == source


@pytest.mark.parametrize('goal', ['production_input', 'production_media'])
def test_production_kind_is_frozen_for_input_and_media_delivery(env, goal):
    store, _, _, _ = env
    main, workspace = selected_main(env, '修当前分镜', goal)
    before = store.read_events(RUN)
    with pytest.raises(ToolError, match='媒体类型'):
        main._execute('prepare_production', {'kind': 'video', 'request_id': 'wrong-medium'}, workspace)
    assert store.read_events(RUN) == before


@pytest.mark.parametrize('diagnostic', [None, {'error_category': 'tool_arguments_json'}])
def test_nonretryable_paid_role_failure_closes_runtime_and_pipeline_before_propagating(env, diagnostic):
    store, pipe, settings, ledger = env

    class RejectedResponse:
        calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            raise ModelError(200, 'Synthetic rejected response', usage=Usage(77, 11), usage_final=True,
                             retryable=False, response_diagnostic=diagnostic,
                             public_message='Synthetic response rejected; no automatic retry')

    model = RejectedResponse()
    before = store.latest_artifact(RUN, 'writing')
    with pytest.raises(NonRetryableStageError, match='no automatic retry'):
        Dispatcher(store, pipe, settings, model, ledger, RUN).dispatch('writing', QUOTE, revision_scope=plan(env))
    assert model.calls == 1
    assert pipe.stage_state(RUN, 'writing').open_attempt is None
    assert store.latest_artifact(RUN, 'writing') == before
    tasks = AgentRuntime(store, RUN).snapshot()['tasks']
    assert len(tasks) == 1 and tasks[0]['status'] == 'failed'
    sessions = AgentSessions(store, RUN).sessions()
    assert len(sessions) == 1 and sessions[0]['status'] == 'failed'
    entries = ledger.entries(RUN)
    assert len(entries) == 1 and entries[0].input_tokens == 77 and entries[0].output_tokens == 11
