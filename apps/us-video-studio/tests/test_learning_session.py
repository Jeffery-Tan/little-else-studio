"""Independent learning run boundaries and discussion path; offline only."""
from __future__ import annotations

import shutil
import json
import hashlib
from pathlib import Path
import pytest

from fakes import ScriptedModel, final_turn
from local_http import TestClient
from studio.agents.orchestrator import Orchestrator
from studio.config import Pricing, Settings
from studio.learning import LearningConflict, LearningService
from studio.learning_session import session_phase, session_snapshot
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
