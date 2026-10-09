"""Real HTTP feedback boundaries with isolated stores and scripted models."""
import hashlib

import pytest

from fakes import ScriptedModel, final_turn, tool_turn, fixture_task_interpretation
from local_http import TestClient
from studio.agents.orchestrator import Orchestrator
from studio.materials import accept_upload
from studio.models import PipelineError, STAGES
from studio.production import DraftIn, draft_records, prepare_draft
from studio.production_feedback import bind_target
from studio.revision_scope import plan_revision
from studio.server import build_app
from test_production import png
from test_revision_scope import env, RUN, PlannedModel, image_inputs, plan_args, patch, production_route, write, approve
from test_role_input_boundaries import wait_job


TEXT = '只修改这张分镜的手部，其余内容保留，方向和脚本不动'


def selected_target(draft):
    return {'kind': 'production_draft', 'run_id': RUN,
            'draft_id': draft['draft_id'], 'draft_sha256': draft['draft_sha256']}


def saved_media(store, draft):
    name = f'{draft["draft_id"]}-output.png'
    raw = png()
    accept_upload(store, RUN, name, raw)
    sha = hashlib.sha256(raw).hexdigest()
    attempt_id = f'{draft["draft_id"]}-attempt'
    store.append_event(RUN, 'production_attempt', attempt_id=attempt_id,
                       draft_id=draft['draft_id'], draft_sha256=draft['draft_sha256'],
                       media_kind=draft['kind'], status='succeeded')
    store.append_event(RUN, 'production_output', attempt_id=attempt_id,
                       output={'name': name, 'sha256': sha})
    return {'kind': 'media', 'run_id': RUN, 'attempt_id': attempt_id, 'output_sha256': sha}


def run_http(env, model, *, target=None, text=TEXT, request_id='feedback-test'):
    with TestClient(build_app(env[2], client_factory=lambda: model)) as http:
        payload = {'text': text, 'request_id': request_id}
        if target:
            payload['feedback_target'] = target
        response = http.post(f'/api/runs/{RUN}/jobs', json=payload)
        assert response.status_code == 200, response.text
        return wait_job(http, response.json()['job_id'])


def originals(store):
    return ({stage: store.latest_artifact(RUN, stage) for stage in STAGES}, store.list_approvals(RUN))


def assert_preserved(store, original):
    assert originals(store) == original
    assert not any(event['kind'] in {'execution_authorized', 'agent_task_prepared', 'agent_revision_planned'}
                   for event in store.read_events(RUN))


@pytest.mark.parametrize('target_kind', ['production_draft', 'media'])
@pytest.mark.parametrize('goal', ['revision', 'intake', 'direction', 'writing'])
def test_selected_media_feedback_cannot_start_with_an_upstream_route(env, target_kind, goal):
    store, _, _, _ = env
    _, drafts = image_inputs(env)
    target = selected_target(drafts[0]) if target_kind == 'production_draft' else saved_media(store, drafts[0])
    before, before_drafts = originals(store), draft_records(store, RUN)
    model = ScriptedModel([
        tool_turn('wrong-route', 'prepare_creative_context', {
            'intent': 'edit', 'format': 'video', 'deliverable': goal,
            'task_interpretation': fixture_task_interpretation(TEXT, intent='edit', deliverable=goal)}),
        tool_turn('upstream-plan', 'plan_revision', plan_args(store, path=['segments', 0, 'picture'], quote=TEXT)),
        tool_turn('upstream-dispatch', 'dispatch', {'stage': 'writing', 'brief': TEXT}),
        tool_turn('upstream-patch', 'submit_patch', patch(['segments', 0, 'picture'], 'Must never be saved')),
        final_turn('已停止越界要求')], legacy_task_contracts=False)
    job = run_http(env, model, target=target)
    assert job['deliverable']['status'] != 'complete'
    assert draft_records(store, RUN) == before_drafts
    assert_preserved(store, before)
    assert not any(event['kind'] == 'user_task_interpretation_recorded' for event in store.read_events(RUN))
    assert '不能改成需求、方向或脚本' in model.requests_text()


@pytest.mark.parametrize('change', [
    {'shot_id': 'shot-02', 'supersedes': 'image-2'},
    {'shot_id': 'shot-02'}, {'shot_id': None}, {'supersedes': None},
    {'kind': 'video'}, {'source_version': 2}, {'source_sha256': 'a' * 64},
])
def test_selected_input_rejects_another_shot_or_unbound_new_input(env, change):
    store, _, _, _ = env
    payload, drafts = image_inputs(env)
    before, before_drafts = originals(store), draft_records(store, RUN)
    model = ScriptedModel([
        production_route(),
        tool_turn('wrong-input', 'prepare_production', {
            **payload, 'request_id': 'wrong-replacement', 'shot_id': 'shot-01',
            'supersedes': drafts[0]['draft_id'], **change}),
        *[final_turn('修改未保存，请重新核对') for _ in range(12)]])
    job = run_http(env, model, target=selected_target(drafts[0]))
    assert job['deliverable']['status'] != 'complete'
    assert draft_records(store, RUN) == before_drafts
    assert_preserved(store, before)


@pytest.mark.parametrize('target_kind', ['production_draft', 'media'])
def test_legitimate_selected_replacement_is_idempotent_and_preserves_other_work(env, target_kind):
    store, _, _, _ = env
    payload, drafts = image_inputs(env)
    target = selected_target(drafts[0]) if target_kind == 'production_draft' else saved_media(store, drafts[0])
    before, other = originals(store), draft_records(store, RUN)[drafts[1]['draft_id']]
    replacement = {**payload, 'request_id': 'selected-fix', 'shot_id': drafts[0]['shot_id'],
                   'supersedes': drafts[0]['draft_id'], 'prompt': 'Only correct this hand pose'}
    model = ScriptedModel([production_route(), tool_turn('save', 'prepare_production', replacement),
        tool_turn('same-save', 'prepare_production', replacement), final_turn('已保存当前镜头的新输入')])
    job = run_http(env, model, target=target, text='调整手部姿势')
    assert job['deliverable']['status'] == 'complete'
    records = draft_records(store, RUN)
    assert len(records) == 3
    assert records[drafts[0]['draft_id']]['superseded_by'] == 'selected-fix'
    assert records['selected-fix']['supersedes'] == drafts[0]['draft_id']
    assert records[drafts[1]['draft_id']] == other
    assert_preserved(store, before)


@pytest.mark.parametrize('target_kind', ['production_draft', 'media'])
@pytest.mark.parametrize('change', ['superseded', 'source_changed', 'bad_fingerprint'])
def test_stale_feedback_is_rejected_before_model_invocation(env, target_kind, change):
    store, pipe, settings, ledger = env
    payload, drafts = image_inputs(env)
    target = selected_target(drafts[0]) if target_kind == 'production_draft' else saved_media(store, drafts[0])
    if change == 'superseded':
        prepare_draft(store, pipe, RUN, DraftIn(**payload, request_id='newer-input',
            shot_id=drafts[0]['shot_id'], supersedes=drafts[0]['draft_id']), actor='user')
    elif change == 'source_changed':
        accept_upload(store, RUN, 'new-source.png', png())
    else:
        target['draft_sha256' if target_kind == 'production_draft' else 'output_sha256'] = 'b' * 64
    before, before_drafts = originals(store), draft_records(store, RUN)
    model = ScriptedModel([])
    with pytest.raises(PipelineError, match='变化|替换|过期'):
        bind_target(store, RUN, target)
    with pytest.raises(PipelineError, match='变化|替换|过期'):
        Orchestrator(store, pipe, settings, model, ledger, RUN).send(TEXT, feedback_context=target)
    assert not model.calls
    with TestClient(build_app(settings, client_factory=lambda: model)) as http:
        response = http.post(f'/api/runs/{RUN}/jobs', json={
            'text': TEXT, 'request_id': 'stale-feedback-http', 'feedback_target': target})
        assert response.status_code == 409, response.text
    assert not model.calls
    assert draft_records(store, RUN) == before_drafts
    assert_preserved(store, before)


def test_target_changed_after_route_is_rechecked_at_save(env):
    store, pipe, _, _ = env
    payload, drafts = image_inputs(env)
    before = originals(store)
    def concurrent_replacement():
        prepare_draft(store, pipe, RUN, DraftIn(**payload, request_id='user-newer-input',
            shot_id=drafts[0]['shot_id'], supersedes=drafts[0]['draft_id']), actor='user')
        return tool_turn('outdated-save', 'prepare_production', {**payload, 'request_id': 'model-outdated-input',
            'shot_id': drafts[0]['shot_id'], 'supersedes': drafts[0]['draft_id']})
    model = PlannedModel([production_route(), concurrent_replacement,
                         *[final_turn('目标已经变化') for _ in range(12)]])
    job = run_http(env, model, target=selected_target(drafts[0]))
    assert job['deliverable']['status'] != 'complete'
    assert 'model-outdated-input' not in draft_records(store, RUN)
    assert draft_records(store, RUN)[drafts[0]['draft_id']]['superseded_by'] == 'user-newer-input'
    assert_preserved(store, before)


def test_source_bytes_returning_to_original_still_rejects_feedback_before_model(env):
    from studio.production import source_snapshot
    store, pipe, settings, _ = env
    original = png()
    name = 'roundtrip-source.png'
    accept_upload(store, RUN, name, original)
    for stage in STAGES:
        write(pipe, stage, store.latest_artifact(RUN, stage).structured)
        if stage in {'direction', 'writing'}:
            approve(pipe, stage, 'A' if stage == 'direction' else None)
    _, drafts = image_inputs(env)
    baseline = source_snapshot(store, RUN)
    path = store.run_dir(RUN) / 'materials' / name
    path.write_bytes(b'Synthetic changed source bytes')
    store.append_event(RUN, 'material_uploaded', name=name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    path.write_bytes(original)
    store.append_event(RUN, 'material_uploaded', name=name, sha256=hashlib.sha256(original).hexdigest())
    assert source_snapshot(store, RUN) == baseline
    assert pipe.stage_state(RUN, 'writing').stale
    model = ScriptedModel([])
    with TestClient(build_app(settings, client_factory=lambda: model)) as http:
        response = http.post(f'/api/runs/{RUN}/jobs', json={
            'text': TEXT, 'request_id': 'roundtrip-feedback', 'feedback_target': selected_target(drafts[0])})
        assert response.status_code == 409, response.text
    assert not model.calls


def test_readonly_feedback_does_not_start_a_replacement(env):
    store, _, _, _ = env
    _, drafts = image_inputs(env)
    before, before_drafts = originals(store), draft_records(store, RUN)
    text = '查看当前进度'
    model = ScriptedModel([tool_turn('status-route', 'prepare_creative_context', {
        'intent': 'status', 'format': 'none', 'deliverable': 'follow_up',
        'task_interpretation': fixture_task_interpretation(text, intent='status', format='none', deliverable='follow_up')}),
        final_turn('当前镜头等待核对')], legacy_task_contracts=False)
    job = run_http(env, model, target=selected_target(drafts[0]), text=text)
    assert job['deliverable']['status'] == 'informational'
    assert draft_records(store, RUN) == before_drafts
    assert_preserved(store, before)


def test_free_dialogue_without_selected_target_can_update_the_whole_set(env):
    store, _, _, _ = env
    payload, drafts = image_inputs(env)
    before = originals(store)
    model = ScriptedModel([production_route(), *[
        tool_turn(f'save-{index}', 'prepare_production', {
            **payload, 'request_id': f'all-fix-{index}', 'shot_id': draft['shot_id'],
            'supersedes': draft['draft_id'], 'prompt': 'Update this shot in the requested full set'})
        for index, draft in enumerate(drafts, 1)], final_turn('整套输入已保存')])
    job = run_http(env, model, text='修改整套分镜的手部姿势')
    assert job['deliverable']['status'] == 'complete'
    records = draft_records(store, RUN)
    assert [records[draft['draft_id']]['superseded_by'] for draft in drafts] == ['all-fix-1', 'all-fix-2']
    assert_preserved(store, before)


def test_selected_shot_completion_does_not_require_preparing_other_shots(env):
    from studio.production import _read_state_without_service
    store, pipe, _, _ = env
    source = store.latest_artifact(RUN, 'writing')
    payload = {'kind': 'image', 'model': 'codex-image', 'prompt': 'Synthetic first shot',
               'source_version': source.version, 'source_sha256': source.sha256}
    first = prepare_draft(store, pipe, RUN, DraftIn(**payload, request_id='only-first', shot_id='shot-01'), actor='agent')
    before = originals(store)
    model = ScriptedModel([production_route(), tool_turn('save', 'prepare_production', {
        **payload, 'request_id': 'only-first-fixed', 'shot_id': 'shot-01', 'supersedes': first['draft_id'],
        'prompt': 'Correct the selected hand'}), final_turn('所选镜头已修改')])
    job = run_http(env, model, target=selected_target(first))
    assert job['deliverable']['status'] == 'complete'
    assert job['deliverable']['target_shots'] == 1
    assert len(draft_records(store, RUN)) == 2
    shots = _read_state_without_service(store, pipe, RUN)['shots']['image']['shots']
    assert shots[1]['draft_id'] is None and shots[1]['stage'] == 'unprepared'
    assert_preserved(store, before)


def test_direct_revision_plan_cannot_ignore_a_production_target(env):
    store, pipe, _, _ = env
    _, drafts = image_inputs(env)
    with pytest.raises(PipelineError, match='制作反馈不能重写'):
        plan_revision(store, pipe, RUN, plan_args(store, quote=TEXT), turn_id='bad-route', user_message=TEXT,
                      constraints={'feedback_target': selected_target(drafts[0])})
    assert not any(event['kind'] == 'agent_revision_planned' for event in store.read_events(RUN))


@pytest.mark.parametrize('target_kind', ['production_draft', 'media'])
def test_selected_reference_only_feedback_uses_server_copy_and_preserves_other_work(env, target_kind):
    store, _, _, _ = env
    payload, drafts = image_inputs(env)
    media = saved_media(store, drafts[0])
    target = selected_target(drafts[0]) if target_kind == 'production_draft' else media
    before, other = originals(store), draft_records(store, RUN)[drafts[1]['draft_id']]
    replacement = {**payload, 'request_id': 'selected-reference-only-fix', 'shot_id': drafts[0]['shot_id'],
                   'supersedes': drafts[0]['draft_id'], 'prompt': None,
                   'preserve_prompt_from': {'draft_id': drafts[0]['draft_id'],
                                            'expected_sha256': drafts[0]['draft_sha256']},
                   'references': [{'name': f'{drafts[0]["draft_id"]}-output.png', 'sha256': media['output_sha256'],
                                   'purpose': 'scene'}]}
    model = ScriptedModel([production_route(), tool_turn('save', 'prepare_production', replacement),
        tool_turn('same-save', 'prepare_production', replacement), final_turn('已保留原描述并更新场景参考')])
    job = run_http(env, model, target=target, text='只加场景参考图，原描述逐字保留')
    assert job['deliverable']['status'] == 'complete', job
    records = draft_records(store, RUN)
    assert len(records) == 3
    assert records[replacement['request_id']]['shot_prompt'] == drafts[0]['shot_prompt']
    assert records[replacement['request_id']]['preserve_prompt_from'] == replacement['preserve_prompt_from']
    assert records[drafts[1]['draft_id']] == other
    assert_preserved(store, before)
