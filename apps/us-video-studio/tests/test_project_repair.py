"""Recovery preserves adopted bytes, money, history and shot revisions; offline."""
from dataclasses import replace

import pytest
from local_http import TestClient

from studio.models import PipelineError, STAGES
from studio.project_repair import RepairPlan, RepairAction, preview_repair, apply_repair
from studio.production import DraftIn, prepare_draft, source_snapshot
from studio.project_brief import latest_execution, project_brief, durable_questions
from studio.server import build_app
from test_revision_scope import env, RUN, write, image_inputs

FEEDBACK = '图2的手和网袋需要修改，图4的网袋也有问题，请核对道具放取连续性'


@pytest.fixture
def damaged(env):
    store, pipe, settings, ledger = env
    payload, drafts = image_inputs(env)
    corrected = prepare_draft(store, pipe, RUN, DraftIn(**{**payload, 'request_id': 'image-2-revised',
        'shot_id': 'shot-02', 'supersedes': drafts[1]['draft_id'],
        'prompt': 'Correct mesh and continuity'}), actor='agent')
    store.append_chat(RUN, 'user', FEEDBACK)
    ledger.record(RUN, role='orchestrator', stage='intake', model='synthetic', input_tokens=10, output_tokens=5)
    baseline = source_snapshot(store, RUN)
    good = {s: store.latest_artifact(RUN, s) for s in STAGES}
    structured = {**good['intake'].structured, 'constraints': ['Hands must be consistent in generated stills']}
    bad_intake = write(pipe, 'intake', structured)
    bad_direction = write(pipe, 'direction', good['direction'].structured)
    store.append_event(RUN, 'job_created', job_id='bad-job', job_kind='conversation')
    store.append_event(RUN, 'job_status', job_id='bad-job', status='blocked', reason='writing stale')
    plan = RepairPlan(request_id='repair-fixture-01', reason='Accidental upstream rewrite during image feedback',
        feedback_quote=FEEDBACK, artifacts=[{'stage': a.stage, 'version': a.version, 'sha256': a.sha256}
                                           for a in (bad_intake, bad_direction)])
    return env, plan, good, baseline, corrected


def action(plan, preview):
    return RepairAction(**plan.model_dump(), expected_revision=preview['revision'])


def test_repair_preserves_original_adoptions_history_inputs_and_money(damaged):
    (store, pipe, _, _), plan, good, baseline, corrected = damaged
    approvals = (store.run_dir(RUN) / 'approvals.jsonl').read_bytes()
    ledger = (store.run_dir(RUN) / 'ledger.jsonl').read_bytes()
    events = store.read_events(RUN)
    all_versions = store.list_artifacts(RUN)
    preview = preview_repair(store, RUN, plan)
    assert store.read_events(RUN) == events
    assert {s['stage']: s['version'] for s in preview['active_stages']} == {s: a.version for s, a in good.items()}
    assert all(d['source_ready'] for d in preview['current_inputs'])
    assert corrected['draft_id'] in [d['draft_id'] for d in preview['current_inputs']]
    assert apply_repair(store, RUN, action(plan, preview)) == preview
    assert apply_repair(store, RUN, action(plan, preview)) == preview
    assert len(store.read_events(RUN)) == len(events) + 1
    assert store.list_artifacts(RUN) == all_versions
    for stage, artifact in good.items():
        assert store.latest_artifact(RUN, stage) == artifact
        assert pipe.stage_state(RUN, stage).latest == artifact
        assert pipe.is_complete(RUN, stage) and store.verify_artifact(RUN, stage, artifact.version)
    assert store.next_version(RUN, 'intake') == 3 and store.next_version(RUN, 'direction') == 3
    assert source_snapshot(store, RUN) == baseline
    assert (store.run_dir(RUN) / 'approvals.jsonl').read_bytes() == approvals
    assert (store.run_dir(RUN) / 'ledger.jsonl').read_bytes() == ledger
    execution = latest_execution(store, RUN)
    brief = project_brief(pipe.state(RUN).to_dict(), execution, store.read_chat(RUN), store=store, run_id=RUN)
    assert execution['status'] == 'blocked'
    assert brief['phase'] == 'ready' and brief['title'] == '原采用方案已恢复'
    assert brief['execution']['project_restored'] is True
    assert pipe.stage_state(RUN, 'direction').approval.selected_option_id == 'A'
    # A real later source change revokes the recovery presentation, preserving
    # the old job status and the current staleness rather than hiding it.
    store.append_event(RUN, 'material_uploaded', name='source.png', sha256='b' * 64)
    brief = project_brief(pipe.state(RUN).to_dict(), latest_execution(store, RUN),
                          store.read_chat(RUN), store=store, run_id=RUN)
    assert brief['execution']['project_restored'] is False
    assert brief['execution']['status'] == 'blocked'


@pytest.mark.parametrize('change', ['chat', 'source_abba', 'open_attempt', 'tamper', 'decided', 'returned'])
def test_changed_or_unsafe_repair_never_writes_a_withdrawal(damaged, change):
    (store, pipe, _, _), plan, good, _, _ = damaged
    preview = preview_repair(store, RUN, plan)
    if change == 'chat':
        store.append_chat(RUN, 'user', 'New message after preview')
    elif change == 'source_abba':
        store.append_event(RUN, 'material_uploaded', name='source.png', sha256='b' * 64)
        store.append_event(RUN, 'material_uploaded', name='source.png', sha256='a' * 64)
    elif change == 'open_attempt':
        pipe.start_attempt(RUN, 'intake')
    elif change == 'tamper':
        target = plan.artifacts[0]
        (store.run_dir(RUN) / 'artifacts' / f'{target.stage}-v{target.version}.md').write_text('Tampered')
    else:
        target = plan.artifacts[1]
        approved = store.list_approvals(RUN, 'direction')[0]
        store.append_approval(RUN, replace(approved, artifact_version=target.version,
            artifact_sha256=target.sha256, decision='approved' if change == 'decided' else 'returned'))
    before = store.read_events(RUN)
    with pytest.raises(PipelineError):
        apply_repair(store, RUN, action(plan, preview))
    assert store.read_events(RUN) == before
    assert not any(e['kind'] == 'artifact_versions_withdrawn' for e in before)


def test_request_identity_and_user_feedback_are_exact(damaged):
    (store, _, _, _), plan, _, _, _ = damaged
    preview = preview_repair(store, RUN, plan)
    with pytest.raises(PipelineError, match='原话'):
        preview_repair(store, RUN, plan.model_copy(update={'feedback_quote': 'Invented user words'}))
    with pytest.raises(PipelineError, match='当前版本'):
        preview_repair(store, RUN, plan.model_copy(update={'artifacts': [plan.artifacts[0].model_copy(
            update={'sha256': 'a' * 64})]}))
    apply_repair(store, RUN, action(plan, preview))
    with pytest.raises(PipelineError, match='请求 ID'):
        apply_repair(store, RUN, action(plan.model_copy(update={'reason': 'Another action'}), preview))


def test_http_restore_preview_default_editor_and_retired_reader(damaged):
    (store, _, settings, _), plan, good, _, _ = damaged
    app = build_app(settings, client_factory=lambda *a, **k: pytest.fail('repair never calls a model'))
    base = f'/api/runs/{RUN}'
    with TestClient(app) as http:
        preview = http.post(base + '/recovery/artifacts/preview', json=plan.model_dump())
        assert preview.status_code == 200, preview.text
        applied = http.post(base + '/recovery/artifacts/apply', json=action(plan, preview.json()).model_dump())
        assert applied.status_code == 200, applied.text
        state = http.get(base + '/state').json()
        assert [s['latest']['version'] for s in state['stages']] == [1, 1, 1, 1]
        historical = http.get(base + '/artifacts/direction/2').json()
        assert historical['withdrawn'] and '已停用' in historical['version_label']
        # The editing API must default to the active adopted artifact.
        editor = http.get(base + '/edits/direction').json()
        assert editor['version'] == good['direction'].version
        assert not any(e['kind'] in ('execution_authorized', 'production_attempt') for e in store.read_events(RUN))


def test_repair_rejects_a_running_job_before_any_preview(damaged):
    (store, _, settings, _), plan, _, _, _ = damaged
    app = build_app(settings, client_factory=lambda *a, **k: pytest.fail('No model'))
    with TestClient(app) as http:
        jobs = app.state.jobs
        jobs.active = ('other-run', 'active-job')
        try:
            result = http.post(f'/api/runs/{RUN}/recovery/artifacts/preview', json=plan.model_dump())
            assert result.status_code == 409
            assert '仍在执行' in result.text
        finally:
            jobs.active = None


def test_withdrawn_history_cannot_raise_new_product_questions(damaged):
    (store, _, _, _), plan, _, _, _ = damaged
    preview = preview_repair(store, RUN, plan)
    apply_repair(store, RUN, action(plan, preview))
    # Scanning active history has exactly the pre-repair question set; no
    # withdrawn version participates even though all versions remain readable.
    assert all(a.version == 1 for a in store.active_artifacts(RUN))
    assert len(store.list_artifacts(RUN)) == 6
    assert durable_questions(store, RUN) == []
