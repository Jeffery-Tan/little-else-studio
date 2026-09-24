"""Independent learning run boundaries and discussion path; offline only."""
from __future__ import annotations

import shutil
import json
import hashlib
import copy
import time
from pathlib import Path
import pytest

from fakes import ScriptedModel, final_turn, tool_turn
from local_http import TestClient
from studio.agents.orchestrator import Orchestrator
from studio.config import Pricing, Settings
from studio.learning import LearningConflict, LearningService
from studio.learning_assets import LearningAssets, run_material_resource_id
from studio.learning_session import agent_session_context, session_phase, session_snapshot
from studio.material_observations import material_workspace, save_human_observation
from studio.materials import accept_upload
from studio.ledger import Ledger
from studio.method_selection import record_method_usage
from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.server import build_app
from studio.store import Store


ROOT = Path(__file__).resolve().parents[3]


def setup(tmp_path):
    repo = tmp_path / 'repo'
    shutil.copytree(ROOT / '.agents/skills', repo / '.agents/skills')
    target = repo / 'tracks/us/creative/learning/library.json'
    target.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / 'tracks/us/creative/learning/library.json', target)
    store = Store(tmp_path / 'external')
    settings = Settings(data_root=store.root, base_url='https://example.invalid',
                        pricing=Pricing(source='offline test', as_of='2026-09-24',
                                        input_usd_per_mtok=1, output_usd_per_mtok=2))
    learning = LearningService(store, repository_root=repo)
    return repo, store, settings, learning


def test_learning_run_has_no_production_stages_and_discusses_with_narrow_tools(tmp_path):
    _, store, settings, learning = setup(tmp_path)
    old = store.create_run('old-work', 'Old', 'creative brief')
    assert old.kind == 'creative'
    run = store.create_run('learn-one', 'Study', '先研究节奏', kind='learning')
    assert run.kind == 'learning'
    assert not store.list_artifacts('learn-one')
    assert [e['kind'] for e in store.read_events('learn-one')] == ['learning_started']
    with pytest.raises(PipelineError, match='learning runs'):
        Pipeline(store).start_attempt('learn-one', 'intake')
    model = ScriptedModel([final_turn('这段节奏可能有帮助；目前没有足够观察改写方法。')])
    result = Orchestrator(store, Pipeline(store), settings, model,
                          Ledger(store, settings), 'learn-one',
                          learning_service=learning).send('只分析，不入库。')
    assert result.text.startswith('这段节奏')
    assert not store.list_artifacts('learn-one')
    assert 'submit_stage' not in model.calls[0]['tool_names']
    assert 'prepare_creative_context' not in model.calls[0]['tool_names']
    assert 'propose_learning_delta' in model.calls[0]['tool_names']
    assert 'read_learning_method' in model.calls[0]['tool_names']
    assert '收费观察工具' in model.calls[0]['system']
    session = session_snapshot(store, learning, 'learn-one')
    assert session['status'] == 'discussing'
    assert len(session['discussion']) == 2
    assert session['proposals'] == []
    assert any(e['kind'] == 'learning_discussion_checkpointed'
               for e in store.read_events('learn-one'))


def test_no_change_discussion_does_not_claim_library_update(tmp_path):
    _, store, _, learning = setup(tmp_path)
    store.create_run('learn-unchanged', 'Study', '只分析', kind='learning')
    store.append_event('learn-unchanged', 'learning_applied', proposal_type='delta',
                       before_sha256='same', after_sha256='same')
    phase = session_phase(store, learning, 'learn-unchanged')
    assert phase['status'] == 'no_change'
    assert phase['summary'] == '已记录：暂无知识变更'


def test_learning_session_projects_exact_user_findings_without_promoting_coverage(tmp_path):
    _, store, _, learning = setup(tmp_path)
    run_id = 'learn-evidence'
    store.create_run(run_id, 'Observe', 'manual and model evidence', kind='learning')
    accept_upload(store, run_id, 'user.mp4', b'user video fixture')
    accept_upload(store, run_id, 'model.mp4', b'model video fixture')
    workspace = material_workspace(store, run_id)
    user = next(m for m in workspace['materials'] if m['name'] == 'user.mp4')
    save_human_observation(
        store, run_id, name='user.mp4', expected_sha256=user['sha256'],
        expected_manifest_sha256=workspace['expected_manifest_sha256'],
        purpose='reference', level='transcript_only',
        coverage='00:01–00:02 的手写字幕记录；未听音轨',
        findings=['字幕写着今晚收纳', '字幕写着拉上拉链'],
        limitations=['未核对声音', '未查看完整视频'],
    )
    snapshot = session_snapshot(store, learning, run_id)
    notes = [o for o in snapshot['observations'] if o['observer_type'] == 'user']
    assert len(notes) == 2
    assert [o['content'] for o in notes] == ['字幕写着今晚收纳', '字幕写着拉上拉链']
    assert notes[0]['source_ref'].startswith('material-observation:')
    assert notes[0]['id'] == notes[0]['source_ref']
    assert notes[0]['source_ref'].rsplit('#', 1)[0] == notes[1]['source_ref'].rsplit('#', 1)[0]
    assert notes[0]['source_ref'].endswith('#0') and notes[1]['source_ref'].endswith('#1')
    assert all(o['source_name'] == 'user.mp4' and o['media_sha256'] == user['sha256']
               and o['level'] == 'transcript_only' and o['time_range'] is None
               and o['coverage'] == '00:01–00:02 的手写字幕记录；未听音轨'
               and o['observer'] == 'human:workspace-user' and o['review_state'] == 'user_reported'
               and o['observation_kind'] == 'human_material_observation'
               and o['limitations'] == ['未核对声音', '未查看完整视频'] for o in notes)
    assert any(s['name'] == 'user.mp4' and s['media_sha256'] == user['sha256']
               for s in snapshot['sources'])

    model_sha = hashlib.sha256(b'model video fixture').hexdigest()
    store.append_event(run_id, 'reference_video_started', reference_id='ref-model',
                       request={'intent': 'analyze', 'name': 'model.mp4',
                                'expected_sha256': model_sha})
    store.append_event(run_id, 'reference_video_source', reference_id='ref-model',
                       source={'name': 'model.mp4', 'sha256': model_sha})
    store.append_event(run_id, 'video_analysis_candidate', candidate={
        'analysis_id': 'analysis-model', 'name': 'model.mp4', 'sha256': model_sha,
        'purpose': 'reference', 'findings': ['镜头先近后远'],
        'timeline': [{'finding_index': 0, 'start_seconds': 0, 'end_seconds': 2}],
        'sampling': {'frames': [{'id': 'f1', 'time_seconds': 0}]},
    })
    store.append_event(run_id, 'reference_video_ready', reference_id='ref-model',
                       analysis_id='analysis-model', report='sample report')
    both = session_snapshot(store, learning, run_id)
    assert len(both['observations']) == 3
    model = next(o for o in both['observations'] if o['observer_type'] == 'model')
    assert model['source_ref'] == 'analysis:analysis-model'
    assert model['content'] == '镜头先近后远'
    assert model['time_range'] == [0, 2]

    (store.run_dir(run_id) / 'materials' / 'user.mp4').write_bytes(b'changed user fixture')
    changed = session_snapshot(store, learning, run_id)
    assert [o['observer_type'] for o in changed['observations']] == ['model']


def test_agent_can_page_and_target_older_observations_without_reanalysis(tmp_path):
    _, store, _, learning = setup(tmp_path)
    run_id = 'learn-many'
    store.create_run(run_id, 'Many', 'Compare older evidence', kind='learning')
    accept_upload(store, run_id, 'reference.jpg', b'one exact image')
    for start, count in ((0, 30), (30, 11)):
        workspace = material_workspace(store, run_id)
        material = workspace['materials'][0]
        save_human_observation(store, run_id, name=material['name'],
            expected_sha256=material['sha256'],
            expected_manifest_sha256=workspace['expected_manifest_sha256'],
            purpose='reference', level='image_observed', coverage='one image only',
            findings=[f'finding-{index}' for index in range(start, start + count)],
            limitations=['no motion or sound'])
    first = agent_session_context(store, learning, run_id, cursor=0, limit=40)
    assert first['observation_total'] == 41 and first['next_cursor'] == 40
    assert first['observations'][0]['content'] == 'finding-0'
    last = agent_session_context(store, learning, run_id, cursor=40, limit=40)
    assert [row['content'] for row in last['observations']] == ['finding-40']
    exact = agent_session_context(store, learning, run_id,
                                  observation_ids=[first['observations'][0]['id']])
    assert [row['content'] for row in exact['observations']] == ['finding-0']
    with pytest.raises(ValueError, match='Unknown observation ID'):
        agent_session_context(store, learning, run_id, observation_ids=['absent'])


def test_learning_agent_never_receives_migration_holdout_diff(tmp_path):
    _, store, settings, learning = setup(tmp_path)
    canary = 'HOLDOUT-CANARY-DO-NOT-SEND'
    doc = json.loads(learning.library_path.read_text(encoding='utf-8-sig'))
    hidden = copy.deepcopy(doc['cases'][0])
    hidden.update(id='holdout-canary', source_key='fixture:holdout-canary',
                  source_url=None, media=None, comparison_group='holdout-canary',
                  partition='holdout', eligible_for=[])
    hidden['title'] = canary
    doc['cases'].append(hidden)
    learning.library_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    store.create_run('learn-holdout', 'Study', 'Review library', kind='learning')
    proposal = learning.propose_migration('learn-holdout')
    assert canary in proposal['diff']
    assert canary in json.dumps(session_snapshot(store, learning, 'learn-holdout'), ensure_ascii=False)
    model = ScriptedModel([tool_turn('read-one', 'read_learning_session', {'cursor': 0}),
                           final_turn('迁移提案待人工审阅。')])
    Orchestrator(store, Pipeline(store), settings, model, Ledger(store, settings),
                 'learn-holdout', learning_service=learning).send('查看当前学习记录。')
    assert len(model.calls) == 2
    assert canary not in model.requests_text()
    assert 'diff' not in model.requests_text()
    assert proposal['id'] in model.requests_text()


def test_learning_http_reuses_run_but_blocks_production_routes(tmp_path):
    repo, store, settings, _ = setup(tmp_path)
    app = build_app(settings, client_factory=lambda: ScriptedModel([final_turn('离线回复')]),
                    learning_repository_root=repo, learning_media_root=tmp_path / 'media')
    with TestClient(app) as client:
        created = client.post('/api/runs', json={
            'run_id': 'learn-http', 'title': '研究参考', 'brief': '只分析', 'kind': 'learning',
        })
        assert created.status_code == 200, created.text
        assert created.json()['kind'] == 'learning'
        session = client.get('/api/runs/learn-http/learning/session')
        assert session.status_code == 200 and session.json()['status'] == 'collecting'
        assert client.post('/api/runs/learn-http/learning/close', json={}).json()['status'] == 'closed'
        store.append_event('learn-http', 'material_uploaded', name='later.mp4', sha256='0' * 64)
        assert client.get('/api/runs/learn-http/learning/session').json()['status'] == 'collecting'
        assert client.post('/api/runs/learn-http/decision', json={}).status_code == 409
        assert client.post('/api/runs/learn-http/production/drafts', json={}).status_code == 409
        assert client.post('/api/runs/learn-http/actions/review', json={}).status_code == 409
        assert any(row['run_id'] == 'learn-http' and row['kind'] == 'learning'
                   for row in client.get('/api/runs').json())


def test_v2_shared_case_media_reads_exact_learning_resource_after_restore(tmp_path):
    repo, store, settings, learning = setup(tmp_path)
    run_id = 'learn-media'
    store.create_run(run_id, 'Media', 'Exact source', kind='learning')
    migration = learning.propose_migration(run_id)
    learning.apply(run_id, migration['id'], migration['base_library_sha256'],
                   migration['proposal_sha256'], 'migration-media-1')
    original = b'exact image bytes for case media'
    accept_upload(store, run_id, 'source.jpg', original)
    workspace = material_workspace(store, run_id)
    source = workspace['materials'][0]
    save_human_observation(store, run_id, name=source['name'],
        expected_sha256=source['sha256'], expected_manifest_sha256=workspace['expected_manifest_sha256'],
        purpose='reference', level='image_observed', coverage='one still image',
        findings=['One visible seam'], limitations=['No motion observed'])
    observed = next(row for row in session_snapshot(store, learning, run_id)['observations']
                    if row['observer_type'] == 'user')
    case = {'id': 'new-media-case', 'source_key': 'sha256:' + source['sha256'],
            'source_platform': 'local', 'source_market': 'unknown', 'title': 'Exact image',
            'comparison_group': source['sha256'], 'partition': 'pending',
            'eligible_for': [], 'tags': ['proof'], 'strategy': {},
            'limits': ['Still image only'], 'evidence': [observed],
            'media': {'resource_id': run_material_resource_id(run_id, source['name']),
                      'sha256': source['sha256']}}
    proposed = learning.propose_delta(run_id, {'rationale': 'Save exact image observation',
        'operations': [{'op': 'case.upsert', 'case': case}]})
    learning.apply(run_id, proposed['id'], proposed['base_library_sha256'],
                   proposed['proposal_sha256'], 'save-media-1')
    app = build_app(settings, client_factory=lambda: ScriptedModel([final_turn('offline')]),
                    learning_repository_root=repo, learning_media_root=tmp_path / 'media')
    with TestClient(app) as client:
        response = client.get('/api/learning/cases/new-media-case/media')
        assert response.status_code == 200 and response.content == original
        (store.run_dir(run_id) / 'materials/source.jpg').write_bytes(b'changed image bytes')
        assert client.get('/api/learning/cases/new-media-case/media').status_code == 404
    (store.run_dir(run_id) / 'materials/source.jpg').write_bytes(original)
    restored_root = tmp_path / 'restored-data'
    shutil.copytree(store.root, restored_root)
    restored = LearningAssets(repo, tmp_path / 'media', store=Store(restored_root))
    assert restored.case_media(learning.catalog(), 'new-media-case').read_bytes() == original


def test_learning_tool_proposal_http_review_and_new_project_context(tmp_path):
    repo, store, settings, learning = setup(tmp_path)
    run_id = 'learn-chain'
    store.create_run(run_id, 'Study', 'Discuss a scoped preference', kind='learning')
    migration = learning.propose_migration(run_id)
    learning.apply(run_id, migration['id'], migration['base_library_sha256'],
                   migration['proposal_sha256'], 'migration-chain-1')
    quote = '我喜欢在选择理由前有一点停顿'
    payload = {'rationale': 'Preserve a user preference, not a market fact',
        'operations': [{'op': 'preference.upsert', 'preference': {
            'id': 'spoken-pause', 'scope': 'user', 'owner': 'workspace-user',
            'dimension': 'pace', 'quote': quote,
            'source_ref': f'user-chat:{run_id}:0'}}]}
    model = ScriptedModel([tool_turn('propose-one', 'propose_learning_delta', payload),
                           final_turn('已准备待审偏好，尚未保存。')])
    app = build_app(settings, client_factory=lambda: model, learning_repository_root=repo,
                    learning_media_root=tmp_path / 'media')
    with TestClient(app) as client:
        posted = client.post(f'/api/runs/{run_id}/jobs', json={
            'text': quote + '。请先整理成待审记录。', 'request_id': 'learning-chain-one'})
        assert posted.status_code == 200, posted.text
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            job = client.get(f"/api/runs/{run_id}/jobs/{posted.json()['job_id']}").json()
            if job['status'] not in {'queued', 'running'}:
                break
            time.sleep(.02)
        assert job['status'] == 'succeeded', job
        proposals = [row for row in client.get(f'/api/runs/{run_id}/learning/session').json()['proposals']
                     if row['type'] == 'delta']
        assert len(proposals) == 1 and proposals[0]['status'] == 'pending'
        assert 'spoken-pause' not in {p['id'] for p in learning.catalog()['preferences']}
        proposal = proposals[0]
        applied = client.post(f"/api/runs/{run_id}/learning/proposals/{proposal['id']}/apply", json={
            'expected_library_sha256': proposal['base_library_sha256'],
            'expected_proposal_sha256': proposal['proposal_sha256'],
            'request_id': 'learning-chain-apply'})
        assert applied.status_code == 200, applied.text
    store.create_run('fresh-creative', 'Fresh', 'New creative task')
    creative = learning.context('fresh-creative', 'orchestrator', intent='new', format='video')
    assert quote in creative['text'] and creative['preferences'][0]['id'] == 'spoken-pause'
    assert 'spoken-pause' not in json.dumps(learning.context('fresh-creative', 'reviewer',
        intent='new', format='video'), ensure_ascii=False)


def test_method_usage_binds_real_method_revision_and_exact_artifact(tmp_path):
    _, store, _, learning = setup(tmp_path)
    store.create_run('new-work', 'New', '创作任务')
    directory = learning.method_summaries(run_id='new-work', purpose='message')
    assert directory['methods']
    selected = directory['methods'][0]
    artifact = store.write_artifact('new-work', 'writing', 1,
                                    '这句把可见动作接到具体选择理由。',
                                    summary='Script', author_role='writer', attempt='test')
    payload = {'request_id': 'usage-one', 'method_id': selected['id'],
               'revision': selected['revision'],
               'library_sha256': directory['library_sha256'],
               'purpose': 'message', 'decision': 'chosen',
               'reason': '本任务需要补足选择理由', 'stage': 'writing', 'version': 1,
               'artifact_sha256': artifact.sha256, 'passage': '具体选择理由'}
    first = record_method_usage(store, learning, 'new-work', payload)
    assert first['assessment'].startswith('agent_claim_only')
    assert first['task_profile'] == '创作任务'
    assert record_method_usage(store, learning, 'new-work', payload) == first
    try:
        record_method_usage(store, learning, 'new-work', dict(payload, request_id='usage-two',
                                                              passage='并不存在的台词'))
    except ValueError:
        pass
    else:
        raise AssertionError('A fabricated affected passage was accepted')


def test_creative_run_pins_shared_library_until_its_own_applied_change(tmp_path):
    _, store, _, learning = setup(tmp_path)
    store.create_run('pinned-work', 'Pinned', '旧项目')
    old = learning.method_summaries(run_id='pinned-work', purpose='message')
    original = json.loads(learning.library_path.read_text(encoding='utf-8-sig'))
    original['library_version'] = original.get('library_version', 1) + 1
    learning.library_path.write_text(json.dumps(original, ensure_ascii=False, indent=2) + '\n',
                                     encoding='utf-8')
    still_old = learning.method_summaries(run_id='pinned-work', purpose='message')
    assert still_old['library_sha256'] == old['library_sha256']
    store.create_run('fresh-work', 'Fresh', '新项目')
    fresh = learning.method_summaries(run_id='fresh-work', purpose='message')
    assert fresh['library_sha256'] != old['library_sha256']
    assert fresh['library_version'] == old['library_version'] + 1
    assert len([event for event in store.read_events('pinned-work')
                if event['kind'] == 'learning_library_pinned']) == 1


def test_historical_unpinned_context_cannot_silently_switch_library(tmp_path):
    _, store, _, learning = setup(tmp_path)
    store.create_run('historical-work', 'Old', '旧稿')
    store.append_event('historical-work', 'learning_context_loaded',
                       library_sha256='a' * 64, snapshot_sha256='b' * 64)
    with pytest.raises(LearningConflict, match='旧项目使用过另一版学习库'):
        learning.method_summaries(run_id='historical-work', purpose='message')


def test_legacy_creative_run_survives_reviewed_v2_migration(tmp_path):
    _, store, settings, learning = setup(tmp_path)
    store.create_run('legacy-work', 'Old', '继续已有项目')
    old_context = learning.context('legacy-work', 'orchestrator', intent='status', format='video')
    old_sha = old_context['library_sha256']
    (store.run_dir('legacy-work') / 'learning/library-pin.json').unlink()
    store.create_run('migration-review', 'Migration', 'review only', kind='learning')
    proposal = learning.propose_migration('migration-review')
    learning.apply('migration-review', proposal['id'], proposal['base_library_sha256'],
                   proposal['proposal_sha256'], 'migration-review-1')
    assert learning.catalog()['library_sha256'] != old_sha
    model = ScriptedModel([final_turn('旧项目可继续核对，尚未切到新库。')])
    result = Orchestrator(store, Pipeline(store), settings, model, Ledger(store, settings),
                          'legacy-work', learning_service=learning).send('请继续核对已有项目。')
    assert '旧项目可继续' in result.text
    assert learning.method_summaries(run_id='legacy-work')['library_sha256'] == old_sha
    pin = json.loads((store.run_dir('legacy-work') / 'learning/library-pin.json').read_text(encoding='utf-8'))
    assert pin['sha256'] == old_sha
    assert not any(event['kind'] == 'approval' for event in store.read_events('legacy-work'))


def test_missing_legacy_library_allows_readonly_followup_then_explicit_refresh(tmp_path):
    repo, store, settings, learning = setup(tmp_path)
    store.create_run('legacy-unbacked', 'Old', '核对旧项目')
    old = learning.context('legacy-unbacked', 'orchestrator', intent='status')
    (store.run_dir('legacy-unbacked') / 'learning/library-pin.json').unlink()
    current = json.loads(learning.library_path.read_text(encoding='utf-8-sig'))
    current['library_version'] = current.get('library_version', 1) + 1
    learning.library_path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    model = ScriptedModel([final_turn('只回看已有记录。')])
    Orchestrator(store, Pipeline(store), settings, model, Ledger(store, settings),
                 'legacy-unbacked', learning_service=learning).send('请只核对旧项目。')
    assert 'prepare_creative_context' not in model.calls[0]['tool_names']
    assert 'dispatch_stage' not in model.calls[0]['tool_names']
    assert not store.list_artifacts('legacy-unbacked')
    status = learning.pin_status('legacy-unbacked')
    assert status['historical_readonly'] and status['run_library_sha256'] == old['library_sha256']
    app = build_app(settings, client_factory=lambda: ScriptedModel([final_turn('offline')]),
                    learning_repository_root=repo, learning_media_root=tmp_path / 'media')
    with TestClient(app) as client:
        assert client.get('/api/runs/legacy-unbacked/learning').json()['pin_status'] == status
        body = {'expected_run_sha256': old['library_sha256'],
                'expected_current_sha256': status['current_library_sha256'],
                'request_id': 'refresh-user-1'}
        response = client.post('/api/runs/legacy-unbacked/learning/refresh', json=body)
        assert response.status_code == 200, response.text
        refreshed = response.json()
        assert client.post('/api/runs/legacy-unbacked/learning/refresh', json=body).json() == refreshed
    assert not refreshed['historical_readonly'] and not refreshed['refresh_available']
    next_model = ScriptedModel([final_turn('可以继续处理后续任务。')])
    Orchestrator(store, Pipeline(store), settings, next_model, Ledger(store, settings),
                 'legacy-unbacked', learning_service=learning).send('继续下一轮。')
    assert 'prepare_creative_context' in next_model.calls[0]['tool_names']
    assert learning.method_summaries(run_id='legacy-unbacked')['library_sha256'] == refreshed['current_library_sha256']


def test_actual_v1_shape_migrates_in_isolated_copy_with_exact_backup(tmp_path):
    _, store, _, learning = setup(tmp_path)
    store.create_run('migration-review', 'Migration', 'review only', kind='learning')
    original = learning.library_path.read_bytes()
    assert 'library_version' not in json.loads(original.decode('utf-8-sig'))
    preview = learning.migration_preview()
    assert preview['base_library_version'] == 1
    assert learning.library_path.read_bytes() == original
    proposal = learning.propose_migration('migration-review')
    applied = learning.apply('migration-review', proposal['id'],
                             proposal['base_library_sha256'],
                             proposal['proposal_sha256'], 'migration-review-1')
    assert applied['status'] == 'applied'
    backup = store.run_dir('migration-review') / 'learning/proposals' / proposal['id']
    assert (backup / 'before.json').read_bytes() == original
    assert json.loads((backup / 'migration-backup.json').read_text(encoding='utf-8'))['library_version'] == 1
    assert json.loads(learning.library_path.read_text(encoding='utf-8-sig'))['schema_version'] == 2
