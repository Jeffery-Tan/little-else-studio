"""Frozen production turns stop on real prerequisites; all runs/models are fake."""
from copy import deepcopy
import json
import time

import pytest

from fakes import ScriptedModel, final_turn, tool_turn, fixture_task_interpretation
from local_http import TestClient
from studio.agents.client import ModelError
from studio.agents.orchestrator import Orchestrator
from studio.materials import accept_upload
from studio.models import STAGES
from studio.production import _read_state_without_service, continuation_blocker, digest
from studio.server import build_app
from studio.tools.workspace import Workspace
from test_production import png
from test_revision_scope import env, RUN, write, approve, image_inputs, production_route


def route(goal='production_input', text='继续'):
    return production_route(intent='continue', deliverable=goal,
        task_interpretation=fixture_task_interpretation(text, intent='continue', deliverable=goal))


def missing_design(env):
    store, pipe, _, _ = env
    story = deepcopy(store.latest_artifact(RUN, 'writing').structured)
    material = deepcopy(store.latest_artifact(RUN, 'material_plan').structured)
    material.pop('visual_design')
    write(pipe, 'material_plan', material)
    for segment in story['segments']:
        segment.pop('character_ids', None)
        segment.pop('scene_id', None)
    write(pipe, 'writing', story)
    approve(pipe, 'writing')


def main(env, turns):
    store, pipe, settings, ledger = env
    model = ScriptedModel(turns, legacy_task_contracts=False)
    return Orchestrator(store, pipe, settings, model, ledger, RUN), model


def originals(store):
    return [store.latest_artifact(RUN, stage) for stage in STAGES], store.list_approvals(RUN)


def unknown_original(env):
    store, _, _, _ = env
    _, drafts = image_inputs(env)
    old = deepcopy(next(event['draft'] for event in store.read_events(RUN)
                        if event['kind'] == 'production_draft'))
    old.update(draft_id='obsolete-input', source_snapshot='0' * 64, shot_id='shot-99')
    old['draft_sha256'] = digest({key: value for key, value in old.items() if key != 'draft_sha256'})
    store.append_event(RUN, 'production_draft', draft=old)
    store.append_event(RUN, 'production_attempt', attempt_id='original-unknown',
        draft_id=old['draft_id'], draft_sha256=old['draft_sha256'], media_kind='image',
        status='unknown', task_id='original-provider-task')
    return drafts


def test_missing_design_ends_first_round_and_skips_same_batch_upstream_tools(env):
    store, _, _, ledger = env
    missing_design(env)
    before = originals(store)
    turn = route()
    for extra in [tool_turn('dispatch', 'dispatch', {'stage': 'material_plan', 'brief': 'Wrong upstream'}),
                  tool_turn('revise', 'plan_revision', {'stage': 'material_plan'})]:
        turn.tool_calls.extend(extra.tool_calls)
        turn.raw_blocks.extend(extra.raw_blocks)
    producer, model = main(env, [turn])
    result = producer.send('继续')
    assert len(model.calls) == result.rounds == 1
    assert result.stopped_because == 'done'
    assert result.deliverable['status'] == 'incomplete'
    assert result.deliverable['blocker']['code'] == 'visual_design_missing'
    assert '共同人物' in result.text and '补齐制作设计，保留原脚本' in result.text
    assert all(record.get('skipped') for record in result.tool_log[1:])
    assert originals(store) == before
    assert not any(event['kind'] == 'production_draft' for event in store.read_events(RUN))
    assert len(ledger.entries(RUN)) == 1 and ledger.entries(RUN)[0].usage_status == 'known'
    assert store.read_chat(RUN)[-1]['text'] == result.text


@pytest.mark.parametrize('obstruction', ['design', 'original'])
def test_real_job_projects_specific_blocker_without_false_success(env, obstruction):
    store, _, settings, _ = env
    if obstruction == 'design':
        missing_design(env)
    else:
        unknown_original(env)
    before = originals(store)
    model = ScriptedModel([route('production_media')], legacy_task_contracts=False)
    with TestClient(build_app(settings, client_factory=lambda: model)) as http:
        started = http.post(f'/api/runs/{RUN}/jobs', json={'text': '继续', 'request_id': 'fake-continue'})
        assert started.status_code == 200, started.text
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = http.get(f'/api/runs/{RUN}/jobs/{started.json()["job_id"]}').json()
            if job['status'] in {'succeeded', 'failed', 'blocked'}:
                break
            time.sleep(.01)
        assert job['status'] == 'blocked', job
        assert len(model.calls) == 1
        note = store.read_chat(RUN)[-1]['text']
        assert job['reason'] == note and note
        assert '共同人物' in note if obstruction == 'design' else '原图片任务' in note
        delivery = next(event['deliverable'] for event in store.read_events(RUN)
                        if event['kind'] == 'job_deliverable')
        if obstruction == 'original':
            assert delivery['status'] == 'unknown'
            assert delivery['next_action']['attempt_id'] == 'original-unknown'
            assert delivery['next_action']['task_id'] == 'original-provider-task'
    assert originals(store) == before


def test_unknown_obsolete_task_does_not_block_input_preparation(env):
    unknown_original(env)
    state = _read_state_without_service(env[0], env[1], RUN)
    assert state['shots']['image']['state'] == 'input_ready'
    assert continuation_blocker(state, 'production_input') is None
    producer, model = main(env, [route(), final_turn('Inputs ready')])
    result = producer.send('继续')
    assert len(model.calls) == 2 and result.deliverable['status'] == 'complete'


def test_valid_design_missing_inputs_keeps_preparing_all_shots(env):
    store, _, _, _ = env
    source = store.latest_artifact(RUN, 'writing')
    before = originals(store)
    producer, model = main(env, [route(), *[tool_turn(f'input-{index}', 'prepare_production', {
        'kind': 'image', 'request_id': f'new-{index}', 'shot_id': f'shot-{index:02d}',
        'source_version': source.version, 'source_sha256': source.sha256,
        'model': 'codex-image', 'prompt': 'Synthetic input'}) for index in (1, 2)],
        final_turn('Inputs ready')])
    result = producer.send('继续')
    assert len(model.calls) == 4 and result.deliverable['status'] == 'complete'
    assert originals(store) == before
    assert result.deliverable['target_shots'] == 2


@pytest.mark.parametrize('decision', [None, 'returned'])
def test_pending_or_returned_script_has_accurate_next_action(env, decision):
    store, pipe, _, _ = env
    write(pipe, 'writing', deepcopy(store.latest_artifact(RUN, 'writing').structured))
    if decision:
        pipe.record_approval(RUN, 'writing', nonce=pipe.issue_nonce(RUN, 'writing'),
                             decision=decision, note='Change this fixture')
    producer, model = main(env, [route()])
    result = producer.send('继续')
    assert len(model.calls) == 1
    assert result.deliverable['blocker']['code'] == ('script_returned' if decision else 'script_awaiting_decision')
    assert '退回' in result.text
    if decision:
        assert '新版' in result.text and '采用当前' not in result.text


def test_evidence_hash_returning_to_original_does_not_restore_production(env):
    from studio.evidence import evidence_fingerprint
    store, pipe, _, _ = env
    before_hash = evidence_fingerprint(store, RUN)
    material = accept_upload(store, RUN, 'temporary-source.png', png())
    assert evidence_fingerprint(store, RUN) != before_hash
    (store.run_dir(RUN) / 'materials' / material['name']).unlink()
    assert evidence_fingerprint(store, RUN) == before_hash
    assert pipe.stage_state(RUN, 'writing').stale
    producer, model = main(env, [route()])
    result = producer.send('继续')
    assert len(model.calls) == 1 and result.deliverable['blocker']['code'] == 'script_stale'


@pytest.mark.parametrize('unavailable_tool', [False, True])
def test_source_change_during_model_response_stops_before_mutating_tool(env, unavailable_tool):
    store, pipe, _, ledger = env
    source = store.latest_artifact(RUN, 'writing')
    before = originals(store)
    class ChangingModel(ScriptedModel):
        def complete(self, **kwargs):
            if self.calls:
                accept_upload(store, RUN, 'changed-source.png', png())
            return super().complete(**kwargs)
    pending = tool_turn('input', 'submit_stage' if unavailable_tool else 'prepare_production',
        {} if unavailable_tool else {
        'kind': 'image', 'request_id': 'late-input', 'shot_id': 'shot-01',
        'source_version': source.version, 'source_sha256': source.sha256,
        'model': 'codex-image', 'prompt': 'Should not save'})
    later = tool_turn('later', 'read_work_plan', {})
    pending.tool_calls.extend(later.tool_calls)
    pending.raw_blocks.extend(later.raw_blocks)
    model = ChangingModel([route(), pending], legacy_task_contracts=False)
    producer = Orchestrator(store, pipe, env[2], model, ledger, RUN)
    dispatched = []
    execute = producer._execute
    def tracked(name, args, workspace):
        dispatched.append(name)
        return execute(name, args, workspace)
    producer._execute = tracked
    if unavailable_tool:
        # The frozen manifest rejects the whole malformed batch before any
        # handler, including the source-staleness guard, can run.
        with pytest.raises(ModelError) as rejected:
            producer.send('继续')
        diagnostic = rejected.value.response_diagnostic
        assert diagnostic['schema_rule'] == 'unavailable_tool'
        assert diagnostic['code'] == 'invalid_arguments_schema'
        assert rejected.value.retryable is False
        assert '未执行返回工具' in rejected.value.public_message
        assert 'submit_stage' not in dispatched
    else:
        result = producer.send('继续')
        assert result.deliverable['blocker']['code'] == 'script_stale'
        assert result.text
        assert result.tool_log[-1].get('skipped')
    assert len(model.calls) == 2
    assert 'read_work_plan' not in dispatched
    assert originals(store) == before
    assert any(event['kind'] == 'material_uploaded' and event.get('name') == 'changed-source.png'
               for event in store.read_events(RUN)), 'the source did change during the response'
    assert not any(event['kind'] == 'production_draft' for event in store.read_events(RUN))


def test_work_plan_in_production_scope_does_not_advertise_upstream_dispatch(env):
    missing_design(env)
    producer, _ = main(env, [route()])
    producer.send('继续')
    plan = json.loads(producer._execute('read_work_plan', {}, Workspace(env[0], env[1], RUN, 'orchestrator')))
    assert all(row['action'] == 'read_only' for row in plan['stages'])
    assert plan['next_step'] == plan['production_next_step']
    assert plan['production_next_step']['kind'] == 'design_upgrade'


def test_missing_shot_list_is_distinct_from_missing_design(env):
    store, pipe, _, _ = env
    missing_design(env)
    # Import an already-existing legacy record; current full submissions must
    # still reject an empty shot list rather than manufacture this fixture.
    prior = store.latest_artifact(RUN, 'writing')
    store.write_artifact(RUN, 'writing', store.next_version(RUN, 'writing'),
        'Synthetic prose without a shot list', summary='Historical prose import',
        author_role='writer', attempt=prior.attempt, inputs=prior.inputs, structured={})
    approve(pipe, 'writing')
    producer, model = main(env, [route()])
    result = producer.send('继续')
    assert len(model.calls) == 1 and result.deliverable['blocker']['code'] == 'shot_list_missing'
    assert '镜头清单' in result.text and '补齐制作设计' not in result.text


@pytest.mark.parametrize('stage', ['intake', 'direction', 'material_plan'])
def test_any_open_upstream_task_blocks_without_misdiagnosing_missing_design(env, stage):
    store, pipe, _, _ = env
    before = originals(store)
    attempt = pipe.start_attempt(RUN, stage)
    producer, model = main(env, [route()])
    result = producer.send('继续')
    assert len(model.calls) == 1
    assert result.deliverable['blocker']['code'] == 'stage_task_in_progress'
    assert result.deliverable['next_action'] == {'action': 'check_stage_task',
        'stage': stage, 'attempt_number': attempt.number}
    assert '未结束' in result.text and '设计补充' not in result.text
    assert originals(store) == before


@pytest.mark.parametrize('phase', ['running', 'unknown'])
def test_original_media_task_precedes_design_and_keeps_actual_phase(env, phase):
    store, pipe, _, _ = env
    unknown_original(env)
    store.append_event(RUN, 'production_attempt', attempt_id='original-unknown',
        draft_id='obsolete-input', status=phase, task_id='original-provider-task')
    missing_design(env)
    state = _read_state_without_service(store, pipe, RUN, execution_active=(phase == 'running'))
    blocker = continuation_blocker(state, 'production_media')
    assert blocker['code'] == 'original_media_unresolved'
    assert blocker['status'] == ('in_progress' if phase == 'running' else 'unknown')
    assert blocker['next_action']['attempt_id'] == 'original-unknown'


def test_single_shot_feedback_does_not_require_other_inputs(env):
    store, pipe, settings, ledger = env
    source = store.latest_artifact(RUN, 'writing')
    from studio.production import DraftIn, prepare_draft
    payload = {'kind': 'image', 'model': 'codex-image', 'prompt': 'Original synthetic',
               'source_version': source.version, 'source_sha256': source.sha256}
    selected = prepare_draft(store, pipe, RUN, DraftIn(**payload, request_id='only-one', shot_id='shot-01'), actor='agent')
    text = '只修改这张图'
    model = ScriptedModel([production_route(task_interpretation=fixture_task_interpretation(
        text, intent='edit', deliverable='production_input')), tool_turn('replace', 'prepare_production', {
        **payload, 'prompt': 'Changed synthetic', 'request_id': 'replacement',
        'shot_id': 'shot-01', 'supersedes': selected['draft_id']}), final_turn('Saved')],
        legacy_task_contracts=False)
    target = {'kind': 'production_draft', 'run_id': RUN, 'draft_id': selected['draft_id'],
              'draft_sha256': selected['draft_sha256']}
    result = Orchestrator(store, pipe, settings, model, ledger, RUN).send(text, feedback_context=target)
    assert result.deliverable['status'] == 'complete' and result.deliverable['target_shots'] == 1, result
    assert _read_state_without_service(store, pipe, RUN)['shots']['image']['shots'][1]['stage'] == 'unprepared'


def test_new_read_only_turn_does_not_inherit_production_obstruction(env):
    missing_design(env)
    producer, model = main(env, [route(), tool_turn('status', 'prepare_creative_context', {
        'intent': 'status', 'format': 'video', 'deliverable': 'follow_up',
        'task_interpretation': fixture_task_interpretation('看看状态', intent='status', deliverable='follow_up')}),
        final_turn('Read only status')])
    producer.send('继续')
    result = producer.send('看看状态')
    assert len(model.calls) == 3 and result.deliverable['status'] == 'informational'
    plan = json.loads(producer._execute('read_work_plan', {}, Workspace(env[0], env[1], RUN, 'orchestrator')))
    assert plan['production_next_step']['stage'] == 'material_plan'
    assert plan['production_next_step']['action'] == 'update'
