"""Model indexes preserve exact outcomes, with original inputs available on demand."""
import json
from copy import deepcopy

import pytest

from studio.agent_runtime import bind_task, reset_task
from studio.production import agent_draft_projection, agent_production_state
from test_agent_runtime import child, project
from test_production import RUN, workspace
from test_visual_production import save, visual_scene


def test_long_input_index_keeps_every_identity_and_unknown_task(monkeypatch):
    original = 'Original description.\n准确英文原话与空行\n\n' * 2000
    drafts = [{
        'draft_id': f'input-{i}', 'draft_sha256': f'{i:064x}',
        'shot_id': f'shot-{i + 1:02}', 'shot_prompt': original,
        'compiled_prompt': original + 'Exact output contract.',
        'prompt': original + 'Exact output contract.',
        'request_payload': {'prompt': original}, 'source_snapshot': {'writing': 'a' * 64},
        'source_version': i + 1, 'source_ready': False, 'is_current': i == 19,
        'can_generate': False, 'reason': 'Original task result is unknown.',
        'references': [{'name': f'original-{i}.png', 'sha256': 'b' * 64, 'purpose': 'product'}],
        'attempts': [{'attempt_id': f'task-{i}', 'status': 'unknown',
                      'failure': {'code': 'timeout', 'message': 'Query this exact task.'},
                      'endpoint': 'https://provider.example/tasks?private=omitted'}],
        'design_binding': {'scene': 'exact-scene'},
    } for i in range(20)]
    state = {key: [] for key in ('targets', 'shots', 'unresolved_attempts', 'sources',
        'materials', 'video_sequence', 'edit_plan', 'assemblies', 'latest_approved_image',
        'visual_design_status', 'preparation_blocker', 'open_stage_tasks',
        'design_upgrade_action', 'reuse_candidates')}
    state.update(baseline={'writing': 'a' * 64}, drafts=drafts,
                 unresolved_attempts=[{'attempt_id': 'task-19', 'status': 'unknown'}])
    before = deepcopy(state)
    monkeypatch.setattr('studio.production._read_state_without_service', lambda *a, **k: state)
    indexed = agent_production_state(None, None, 'offline')
    assert len(indexed['drafts']) == 20
    assert indexed['unresolved_attempts'] == before['unresolved_attempts']
    assert len(json.dumps(indexed)) < len(json.dumps(state)) / 50
    for old, row in zip(before['drafts'], indexed['drafts']):
        for key in ('draft_id', 'draft_sha256', 'shot_id', 'source_snapshot', 'source_version',
                    'source_ready', 'is_current', 'can_generate', 'reason', 'references'):
            assert row[key] == old[key]
        assert row['attempts'][0]['failure'] == old['attempts'][0]['failure']
        assert row['attempts'][0]['endpoint_host'] == 'provider.example'
        assert not {'prompt', 'shot_prompt', 'compiled_prompt', 'request_payload'} & row.keys()
    exact = agent_production_state(None, None, 'offline', **{
        k: v for k, v in indexed['drafts'][2]['read_input'].items() if k != 'tool'})
    assert len(exact['drafts']) == 1 and exact['drafts'][0]['input_details_included']
    assert exact['drafts'][0]['shot_prompt'] == original
    assert exact['drafts'][0]['compiled_prompt'] == original + 'Exact output contract.'
    assert exact['drafts'][0]['design_binding'] == before['drafts'][2]['design_binding']
    assert exact['drafts'][0]['is_current'] is False
    assert exact['unresolved_attempts'] == before['unresolved_attempts']
    assert exact['source_shot']['available'] is False
    assert 'materials' not in exact and 'reuse_candidates' not in exact
    assert state == before


@pytest.mark.parametrize('query', [
    {'draft_id': 'input-1'}, {'expected_sha256': 'a' * 64},
    {'draft_id': 'missing', 'expected_sha256': 'a' * 64},
    {'draft_id': 'input-1', 'expected_sha256': 'b' * 64},
])
def test_exact_input_read_never_substitutes_latest(monkeypatch, query):
    monkeypatch.setattr('studio.production._read_state_without_service', lambda *a, **k: {
        'drafts': [{'draft_id': 'input-1', 'draft_sha256': 'a' * 64}]})
    with pytest.raises(ValueError):
        agent_production_state(None, None, 'offline', **query)


@pytest.mark.parametrize('visual_scene', ['photo'], indirect=True)
def test_model_read_keeps_full_http_input_and_never_changes_authority(visual_scene):
    scene = visual_scene
    response = save(scene, prompt='Exact original.\nSecond line.\n\nFinal line.')
    assert response.status_code == 200, response.text
    raw = response.json()
    public_before = workspace(scene)
    events = scene[1].read_events(RUN)
    indexed = agent_production_state(scene[1], scene[2], RUN)
    row = next(d for d in indexed['drafts'] if d['draft_id'] == raw['draft_id'])
    assert row['read_input']['expected_sha256'] == raw['draft_sha256']
    assert 'compiled_prompt' not in row
    detail = agent_production_state(scene[1], scene[2], RUN, draft_id=raw['draft_id'],
                                    expected_sha256=raw['draft_sha256'])
    exact = next(d for d in detail['drafts'] if d['draft_id'] == raw['draft_id'])
    assert exact['shot_prompt'] == raw['shot_prompt']
    assert exact['compiled_prompt'] == raw['compiled_prompt']
    assert 'prompt' not in exact and 'request_payload' not in exact
    assert detail['targets'] == public_before['targets']
    assert detail['shots'] == public_before['shots']
    source = detail['source_shot']
    writing = scene[1].latest_artifact(RUN, 'writing')
    assert source['available'] and source['is_current_source']
    assert source['sha256'] == writing.sha256 and source['version'] == writing.version
    assert source['segment'] == writing.structured['segments'][0]
    assert workspace(scene) == public_before
    assert scene[1].read_events(RUN) == events
    assert not scene[4].calls and not scene[5].calls
    receipt = agent_draft_projection(raw)
    assert receipt['references'] == raw['references']
    assert receipt['read_input'] == row['read_input']
    from fakes import ScriptedModel
    from studio.agents.orchestrator import Orchestrator
    from studio.ledger import Ledger
    from studio.tools.workspace import Workspace
    main = Orchestrator(scene[1], scene[2], scene[6], ScriptedModel([]),
                        Ledger(scene[1], scene[6]), RUN)
    returned = json.loads(main._execute('read_production', {
        'draft_id': raw['draft_id'], 'expected_sha256': raw['draft_sha256']},
        Workspace(scene[1], scene[2], RUN, 'orchestrator')))
    assert next(d for d in returned['drafts'] if d['draft_id'] == raw['draft_id']) == exact
    assert scene[1].read_events(RUN) == events


@pytest.mark.parametrize('case', ['historical', 'legacy_identity', 'ambiguous_identity',
                                 'legacy_metadata', 'wrong_hash', 'tampered'])
def test_exact_detail_uses_verified_historical_source_without_latest_substitution(tmp_path, monkeypatch, case):
    from studio.store import Store
    from studio.pipeline import Pipeline
    from studio.production import target_shots
    store = Store(tmp_path)
    store.create_run('source-check', 'Source check', 'Read only.')
    pipe = Pipeline(store)
    first = store.write_artifact('source-check', 'writing', 1, content='Original source.', summary='Old',
        author_role='writer', attempt='old-source',
        structured={'segments': [{'shot_id': 'bound-shot', 'page_id': 'bound-page',
                                 'picture': 'Exact older picture.\n\nKeep all lines.',
                                 'line': 'Exact older copy.\nSecond line.'}]})
    second = store.write_artifact('source-check', 'writing', 2, content='New source.', summary='New',
        author_role='writer', attempt='new-source',
        structured={'segments': [{'shot_id': 'bound-shot', 'picture': 'Latest must not replace old.',
                                 'line': 'Different latest copy.'}]})
    identity = 'bound-shot' if case != 'legacy_identity' else 'unresolvable-legacy-id'
    if case == 'ambiguous_identity':
        first = store.write_artifact('source-check', 'writing', 3, content='Duplicate ids.', summary='Duplicate',
            author_role='writer', attempt='duplicate-source',
            structured={'segments': [{'shot_id': 'bound-shot', 'line': 'One'},
                                     {'shot_id': 'bound-shot', 'line': 'Two'}]})
    if case == 'legacy_metadata':
        from studio.store import sha256_text
        _, meta_path = store._artifact_paths('source-check', 'writing', first.version)
        metadata = json.loads(meta_path.read_text(encoding='utf-8'))
        metadata['digest_algorithm'] = 'body-v1'
        metadata['sha256'] = sha256_text(store.read_artifact_text('source-check', 'writing', first.version))
        metadata['structured']['segments'][0]['line'] = 'Not protected by the legacy body fingerprint.'
        meta_path.write_text(json.dumps(metadata), encoding='utf-8')
        first = store.read_artifact('source-check', 'writing', first.version)
        assert store.verify_artifact('source-check', 'writing', first.version)
    draft = {'draft_id': 'exact-old', 'draft_sha256': 'b' * 64, 'kind': 'image',
             'source_version': first.version, 'source_sha256': first.sha256,
             'shot_id': identity, 'shot_prompt': 'Exact original input.', 'is_current': False}
    state = {'drafts': [draft], 'sources': [{'version': first.version, 'artifact_sha256': first.sha256}],
             'baseline': {'writing': second.sha256}, 'targets': {'image': target_shots(store, 'source-check')},
             'shots': [], 'unresolved_attempts': [{'attempt_id': 'other-shot-unknown', 'status': 'unknown'}],
             'preparation_blocker': {'note': 'Keep original obstruction.'}, 'open_stage_tasks': []}
    if case == 'wrong_hash':
        draft['source_sha256'] = state['sources'][0]['artifact_sha256'] = 'c' * 64
    if case == 'tampered':
        path = store.run_dir('source-check') / 'artifacts' / f'writing-v{first.version}.md'
        path.write_text('Changed after saving.', encoding='utf-8')
    monkeypatch.setattr('studio.production._read_state_without_service', lambda *a, **k: state)
    events_before = store.read_events('source-check')
    if case in {'wrong_hash', 'tampered'}:
        with pytest.raises(ValueError, match='原方案指纹'):
            agent_production_state(store, pipe, 'source-check', draft_id='exact-old', expected_sha256='b' * 64)
    else:
        result = agent_production_state(store, pipe, 'source-check', draft_id='exact-old', expected_sha256='b' * 64)
        assert result['drafts'][0]['shot_prompt'] == draft['shot_prompt']
        assert result['unresolved_attempts'] == state['unresolved_attempts']
        assert result['preparation_blocker'] == state['preparation_blocker']
        if case == 'historical':
            assert result['source_shot']['segment'] == first.structured['segments'][0]
            assert not result['source_shot']['is_current_source']
            assert result['source_shot']['sha256'] == first.sha256
        else:
            assert not result['source_shot']['available'] and 'segment' not in result['source_shot']
            if case == 'legacy_metadata':
                assert '未覆盖结构化段落' in result['source_shot']['reason']
    assert store.read_events('source-check') == events_before


def test_task_index_keeps_history_stop_failure_and_current_capabilities(project):
    store, _, runtime = project
    runtime.create_task('old-job', role='orchestrator', parent_job_id='old-job',
                        allowed_tools=['read_work_plan'],
                        context_refs={'sources': [{'version': i, 'sha256': 'a' * 64}
                                                  for i in range(100)]})
    runtime.transition('old-job', 'running')
    runtime.request_stop('old-job', reason='Exact stop request.')
    runtime.transition('old-job', 'failed', reason='Exact failure reason.')
    runtime.post_message('job-1', 'progress-1', 'Check status only.')
    before = runtime.snapshot()
    events = store.read_events('runtime-test')
    token = bind_task('job-1')
    try:
        projected = runtime.agent_snapshot()
    finally:
        reset_task(token)
    for raw, row in zip(before['tasks'], projected['tasks']):
        for key in ('task_id', 'status', 'reason', 'stop_requested', 'stop_reason',
                    'artifact_receipt', 'pending_message_count', 'parent_job_id'):
            assert row.get(key) == raw.get(key)
    current = next(t for t in projected['tasks'] if t['task_id'] == 'job-1')
    historical = next(t for t in projected['tasks'] if t['task_id'] == 'old-job')
    assert current['allowed_tools'] == ['dispatch']
    assert 'allowed_tools' not in historical and 'context_refs' not in historical
    assert len(json.dumps(projected)) < len(json.dumps(before)) / 3
    current['allowed_tools'].append('cannot-expand')
    assert runtime.snapshot() == before
    assert store.read_events('runtime-test') == events
    unbound = runtime.agent_snapshot()
    assert unbound['current_task_id'] is None
    assert all('allowed_tools' not in row for row in unbound['tasks'])


def test_task_index_retains_owned_role_contract_and_exact_completed_receipt(project):
    _, pipe, runtime = project
    attempt = child(project)
    runtime.transition(attempt.key, 'running')
    artifact = pipe.submit('runtime-test', 'direction', attempt.token,
                           content='Exact saved options.', summary='Options.')
    runtime.transition(attempt.key, 'completed', artifact_receipt={
        'stage': 'direction', 'version': artifact.version, 'sha256': artifact.sha256})
    raw = runtime.task(attempt.key)
    token = bind_task('job-1')
    try:
        rows = runtime.agent_snapshot()['tasks']
    finally:
        reset_task(token)
    owned = next(row for row in rows if row['task_id'] == attempt.key)
    for key in ('artifact_receipt', 'allowed_tools', 'context_refs', 'expected_result'):
        assert owned[key] == raw[key]
    assert owned['artifact_receipt']['sha256'] == artifact.sha256
    assert owned['status'] == 'completed'


@pytest.mark.parametrize('visual_scene', ['photo'], indirect=True)
def test_model_read_without_service_keeps_original_submitted_task_and_usage(visual_scene):
    from studio.ledger import Ledger
    scene = visual_scene
    draft = save(scene).json()
    ledger = scene[0].app.state.jobs.ledger
    ledger.record(RUN, role='image', stage='production_image', model='codex-image',
                   input_tokens=None, output_tokens=None, ok=False, usage_status='unknown',
                   call_id='exact-original-task', call_status='pending')
    scene[1].append_event(RUN, 'production_attempt', attempt_id='exact-original-task',
        draft_id=draft['draft_id'], draft_sha256=draft['draft_sha256'],
        media_kind='image', status='submitting')
    original_events = scene[1].read_events(RUN)
    projected = agent_production_state(scene[1], scene[2], RUN)
    assert projected['unresolved_attempts'][0]['attempt_id'] == 'exact-original-task'
    row = next(d for d in projected['drafts'] if d['draft_id'] == draft['draft_id'])['attempts'][0]
    assert row['status'] == row['usage_status'] == 'unknown'
    assert projected['shots'] == workspace(scene)['shots']
    assert Ledger.project_entries(scene[1], RUN) == ledger.entries(RUN)
    assert scene[1].read_events(RUN) == original_events
    ledger.settle_call(RUN, 'exact-original-task', input_tokens=7, output_tokens=3,
                       usage_status='known', ok=False)
    revised = agent_production_state(scene[1], scene[2], RUN)
    changed = next(d for d in revised['drafts'] if d['draft_id'] == draft['draft_id'])['attempts'][0]
    assert changed['usage_status'] == 'known' and changed['status'] == 'unknown'
    assert Ledger.project_entries(scene[1], RUN) == ledger.entries(RUN)
    assert not scene[4].calls and not scene[5].calls
