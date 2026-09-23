"""Synthetic migration fixtures only; no providers or real project writes."""
import hashlib
import io
import json

import pytest
from PIL import Image
from local_http import TestClient

from studio.history_import import import_plan, prepare
from studio.project_history import snapshot, read_document
from studio.evidence import evidence_snapshot
from studio.agents.roles import creative_selection, production_allowed
from studio.agents.orchestrator import Orchestrator
from studio.jobs import WriterLease
from studio.ledger import Ledger
from studio.pipeline import Pipeline
from studio.store import Store
from studio.tools.registry import tools_for
from studio.tools.workspace import Workspace
from studio.server import build_app
from test_server import settings_for
from fakes import ScriptedModel


@pytest.fixture
def plan(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'draft.md').write_text('# Prior draft\nNot adopted', encoding='utf-8')
    (source / 'approvals.json').write_text('{"approved": true}', encoding='utf-8')
    (source / 'old-script.py').write_text('raise RuntimeError("Do not run")')
    out = io.BytesIO()
    Image.new('RGB', (240, 240), 'blue').save(out, 'PNG')
    (source / 'reference.png').write_bytes(out.getvalue())
    return {'market': 'US', 'run_id': 'imported-case', 'title': 'Migrated fixture',
            'brief': 'Explicit migration; retain pause.', 'intake': 'Source facts and unadopted draft remain historical.',
            'summary': 'Historical material only', 'historical_cost_note': 'Previous actual bill unknown',
            'sources': [{'id': 'previous', 'root': str(source)}],
            'documents': [{'source': 'previous', 'path': 'draft.md', 'title': 'Prior draft', 'status': 'draft'}],
            'materials': [{'source': 'previous', 'path': 'reference.png', 'label': 'Prior generated reference',
                           'origin': 'generated', 'sha256': hashlib.sha256(out.getvalue()).hexdigest()}]}


def test_import_preserves_all_bytes_but_not_active_approvals_or_ledger(tmp_path, plan):
    root = tmp_path / 'data'
    result = import_plan(root, plan)
    assert result['archive']['files'] == 4
    store = Store(root)
    run = store.run_dir(plan['run_id'])
    for path in (tmp_path / 'source').iterdir():
        assert (run / 'history/archive/previous' / path.name).read_bytes() == path.read_bytes()
    assert not (run / 'approvals.jsonl').exists()
    assert not (run / 'ledger.jsonl').exists()
    assert not (run / 'chat.jsonl').exists()
    state = Pipeline(store).state(plan['run_id'])
    assert state.stages['intake'].latest and not state.stages['intake'].stale
    assert not state.stages['writing'].latest
    assert not production_allowed(creative_selection(store, plan['run_id']))
    assert creative_selection(store, plan['run_id'])['intent'] == 'pause'
    info = snapshot(store, plan['run_id'])
    doc = read_document(store, plan['run_id'], info['documents'][0]['id'])
    assert 'Not adopted' in doc['content']
    assert not evidence_snapshot(store, plan['run_id'])['errors']
    assert evidence_snapshot(store, plan['run_id'])['materials'][0]['origin'] == 'generated'


def test_import_refuses_duplicate_without_changes(tmp_path, plan):
    root = tmp_path / 'data'
    import_plan(root, plan)
    run = root / 'runs' / plan['run_id']
    old = {p.relative_to(run): p.read_bytes() for p in run.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='已存在'):
        import_plan(root, plan)
    assert old == {p.relative_to(run): p.read_bytes() for p in run.rglob('*') if p.is_file()}


def test_live_writer_is_not_bypassed(tmp_path, plan):
    root = tmp_path / 'data'
    with WriterLease(root):
        with pytest.raises(Exception, match='写入进程'):
            import_plan(root, plan)
    assert not (root / 'runs' / plan['run_id']).exists()


@pytest.mark.parametrize('change', ['market', 'outside', 'missing', 'hash', 'duplicate_media', 'script_document'])
def test_invalid_plans_do_not_publish(tmp_path, plan, change):
    if change == 'market': plan['market'] = 'SEA'
    if change == 'outside': plan['sources'][0]['files'] = ['../secret.txt']
    if change == 'missing': plan['documents'][0]['path'] = 'missing.md'
    if change == 'hash': plan['materials'][0]['sha256'] = '0' * 64
    if change == 'duplicate_media': plan['materials'] *= 2
    if change == 'script_document': plan['documents'][0]['path'] = 'old-script.py'
    with pytest.raises(ValueError): import_plan(tmp_path / 'data', plan)
    assert not (tmp_path / 'data/runs' / plan['run_id']).exists()


def test_exact_source_allowlist_omits_unrelated_files(tmp_path, plan):
    plan['sources'][0]['files'] = ['draft.md', 'reference.png']
    result = prepare(plan)
    assert result['archive']['files'] == 2


def test_history_tampering_invalidates_dependencies(tmp_path, plan):
    root = tmp_path / 'data'
    import_plan(root, plan)
    store = Store(root)
    info = snapshot(store, plan['run_id'])
    doc_id = info['documents'][0]['id']
    (store.run_dir(plan['run_id']) / 'history/archive/previous/draft.md').write_text('changed')
    with pytest.raises(ValueError, match='变化'): read_document(store, plan['run_id'], doc_id)
    assert evidence_snapshot(store, plan['run_id'])['errors']
    assert Pipeline(store).state(plan['run_id']).stages['intake'].stale


def test_manifest_tampering_is_rejected(tmp_path, plan):
    root = tmp_path / 'data'
    import_plan(root, plan)
    store = Store(root)
    (store.run_dir(plan['run_id']) / 'history/manifest.json').write_text('{}')
    with pytest.raises(ValueError, match='指纹'): snapshot(store, plan['run_id'])


def test_http_only_serves_registered_text(tmp_path, plan):
    root = tmp_path / 'data'
    import_plan(root, plan)
    with TestClient(build_app(settings_for(root))) as client:
        base = '/api/runs/imported-case/history'
        info = client.get(base).json()
        assert info['available'] and 'sources' not in info and 'files' not in info
        assert client.get(base + '/' + info['documents'][0]['id']).status_code == 200
        assert client.get(base + '/old-script.py').status_code == 409
        assert client.post(base, json={}).status_code == 405


def test_orchestrator_can_read_history_but_reviewers_cannot(tmp_path, plan):
    root = tmp_path / 'data'
    import_plan(root, plan)
    settings = settings_for(root)
    store = Store(root)
    pipeline = Pipeline(store)
    workspace = Workspace(store, pipeline, plan['run_id'], 'orchestrator')
    assert 'read_project_history' in workspace.render_state()
    agent = Orchestrator(store, pipeline, settings, ScriptedModel([]), Ledger(store, settings), plan['run_id'])
    info = json.loads(agent._execute('read_project_history', {}, workspace))
    assert info['materials'][0]['origin'] == 'generated'
    assert info['materials'][0]['label'] == 'Prior generated reference'
    text = agent._execute('read_project_history', {'document_id': info['documents'][0]['id']}, workspace)
    assert 'Not adopted' in text
    for role in ['reviewer', 'director', 'writer', 'material']:
        assert all(t['name'] != 'read_project_history' for t in tools_for(role))
