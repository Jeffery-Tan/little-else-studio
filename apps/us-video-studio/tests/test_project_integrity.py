"""Real temporary project files and HTTP admission; no network or model spend."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from types import SimpleNamespace

import pytest
from local_http import TestClient

from studio.config import Settings
from studio.doctor import inspect
from studio.jobs import JobManager
from studio.ledger import Ledger
from studio.models import Approval, LedgerEntry
from studio.pipeline import Pipeline
from studio.project_integrity import ProjectDataError, inspect_project
from studio.server import build_app
from studio.store import Store


FILES = ('run.json', 'events.jsonl', 'chat.jsonl', 'ledger.jsonl', 'approvals.jsonl')


def settings(root, **over):
    return Settings(data_root=root, base_url='https://example.invalid', **over)


def fixture_projects(root):
    store = Store(root)
    for run_id in ('healthy', 'damaged'):
        store.create_run(run_id, run_id, 'Offline integrity fixture')
        store.append_event(run_id, 'fixture_created')
    return store


def fingerprints(base):
    return {str(path.relative_to(base)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in base.rglob('*') if path.is_file()}


def test_legacy_positive_integer_attempt_is_readable_without_repair_and_stays_writable(tmp_path):
    store = Store(tmp_path)
    store.create_run('legacy', 'Legacy attempt fixture', 'Offline only')
    store.write_artifact('legacy', 'direction', 1, 'Synthetic old body', summary='Legacy',
                         author_role='director', attempt='direction#1')
    path = store.run_dir('legacy') / 'artifacts' / 'direction-v1.json'
    # Reconstruct precisely the metadata the old Store wrote for attempt=1.
    record = json.loads(path.read_text(encoding='utf-8'))
    record['attempt'] = 1
    path.write_bytes((json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    original = path.read_bytes()
    assert inspect_project(tmp_path, 'legacy')['available']
    assert store.read_artifact('legacy', 'direction', 1).attempt == 1
    assert store.verify_artifact('legacy', 'direction', 1)
    store.append_event('legacy', 'healthy_write_after_legacy_read')
    store.write_artifact('legacy', 'direction', 2, 'Synthetic new body', summary='New',
                         author_role='director', attempt='direction#2')
    assert inspect_project(tmp_path, 'legacy')['available']
    assert store.read_artifact('legacy', 'direction', 2).attempt == 'direction#2'
    assert path.read_bytes() == original


@pytest.mark.parametrize('attempt', [False, True, 0, -1, 1.0, [], {}, None, '', '  ', 1])
def test_new_artifact_rejects_bad_attempt_before_writing_files(tmp_path, attempt):
    from studio.models import PipelineError
    store = Store(tmp_path)
    store.create_run('fresh', 'New attempt fixture', 'Offline only')
    before = fingerprints(store.run_dir('fresh'))
    with pytest.raises(PipelineError, match='nonempty string task identity'):
        store.write_artifact('fresh', 'direction', 1, 'Synthetic body', summary='New',
                             author_role='director', attempt=attempt)
    assert fingerprints(store.run_dir('fresh')) == before
    assert not store.list_artifacts('fresh')
    assert inspect_project(tmp_path, 'fresh')['available']


@pytest.mark.parametrize('attempt', [False, True, 0, -1, 1.0, [], {}, None])
def test_legacy_attempt_exception_does_not_accept_other_types(tmp_path, attempt):
    store = Store(tmp_path)
    store.create_run('legacy', 'Invalid attempt fixture', 'Offline only')
    store.write_artifact('legacy', 'direction', 1, 'Synthetic body', summary='Legacy',
                         author_role='director', attempt='direction#1')
    path = store.run_dir('legacy') / 'artifacts' / 'direction-v1.json'
    record = json.loads(path.read_text(encoding='utf-8'))
    record['attempt'] = attempt
    path.write_bytes(json.dumps(record).encode('utf-8'))
    before = fingerprints(store.run_dir('legacy'))
    assert not inspect_project(tmp_path, 'legacy')['available']
    with pytest.raises(ProjectDataError):
        store.append_event('legacy', 'must_not_write')
    assert fingerprints(store.run_dir('legacy')) == before


def corrupt(store, filename, payload=b'{"private_fixture":'):
    path = store.run_dir('damaged') / filename
    if filename == 'events.jsonl':
        with path.open('ab') as handle:
            handle.write(payload)
    else:
        path.write_bytes(payload)
    return path


@pytest.mark.parametrize('filename', FILES)
@pytest.mark.parametrize('after_startup', [False, True])
def test_corruption_isolated_at_startup_and_all_http_mutations(tmp_path, filename, after_startup):
    store = fixture_projects(tmp_path)
    if not after_startup:
        corrupt(store, filename)
    app = build_app(settings(tmp_path), client_factory=lambda: pytest.fail('No model may be prepared'))
    with TestClient(app) as client:
        if after_startup:
            corrupt(store, filename)
        originals = fingerprints(store.run_dir('damaged'))
        assert client.get('/api/runtime').status_code == 200
        catalog = client.get('/api/runs')
        assert catalog.status_code == 200, catalog.text
        bad = next(row for row in catalog.json() if row['run_id'] == 'damaged')
        assert bad['integrity']['available'] is False
        issue = next(item for item in bad['integrity']['issues'] if item['file'] == filename)
        assert issue['category'] == 'invalid_json'
        assert len(issue['sha256']) == 64
        assert 'private_fixture' not in json.dumps(bad['integrity'])
        assert client.get('/api/runs/damaged/integrity').json() == bad['integrity']
        for suffix in ('state', 'timeline', 'chat', 'ledger', 'recovery'):
            response = client.get('/api/runs/healthy/' + suffix)
            assert response.status_code == 200, (suffix, response.text)
            assert client.get('/api/runs/damaged/' + suffix).status_code == 409
        assert client.get('/api/trash').status_code == 200
        assert client.post('/api/runs', json={'run_id': 'damaged', 'title': 'Cannot overwrite',
                                             'brief': 'No changes'}).status_code == 409
        tested = set()
        for route in app.routes:
            path = getattr(route, 'path', '')
            if not path.startswith('/api/runs/{run_id}'):
                continue
            path = path.replace('{run_id}', 'damaged')
            path = re.sub(r'\{[^}]+\}', 'fixture', path)
            for method in getattr(route, 'methods', set()) & {'POST', 'PUT', 'PATCH', 'DELETE'}:
                headers = {'content-type': ('multipart/form-data; boundary=fixture'
                                            if path.endswith('/materials') else 'application/json')}
                response = client.request(method, path, content=b'{}', headers=headers)
                assert response.status_code == 409, (method, path, response.text)
                assert response.json()['integrity']['run_id'] == 'damaged'
                tested.add((method, path))
        assert len(tested) >= 25
        assert fingerprints(store.run_dir('damaged')) == originals
        assert app.state.jobs.active is None


@pytest.mark.parametrize('filename', FILES)
def test_store_job_and_model_intent_do_not_touch_bad_originals(tmp_path, filename):
    from studio.store import _append_jsonl, _atomic_write_text
    store = fixture_projects(tmp_path)
    corrupt(store, filename)
    originals = fingerprints(store.run_dir('damaged'))
    ledger = Ledger(store, settings(tmp_path))
    manager = JobManager(store, Pipeline(store), ledger, lambda run_id: pytest.fail('No runner preparation'))
    manager.initialize()
    operations = (
        lambda: store.append_event('damaged', 'should_not_exist'),
        lambda: store.append_chat('damaged', 'user', 'should not exist'),
        lambda: store.write_artifact('damaged', 'intake', 1, 'body', summary='s',
                                     author_role='orchestrator', attempt='fixture'),
        lambda: store.set_deleted('damaged', True),
        lambda: store.append_approval('damaged', Approval('intake', '0' * 64, 'returned', '', '', '')),
        lambda: store.archive_run('damaged'),
        lambda: ledger.begin_call('damaged', role='writer', stage='writing', model='fixture'),
        lambda: ledger.begin_media_call('damaged', model='fixture', provider='fixture', usage_unit='images'),
        lambda: manager.submit('damaged', 'No call', 'fixture-request'),
        lambda: _append_jsonl(store.run_dir('damaged') / 'agent-sessions' / 'fixture' / 'journal.jsonl',
                              {'kind': 'must_not_write'}),
        lambda: _atomic_write_text(store.run_dir('damaged') / 'agent-sessions' / 'fixture' / 'session.json', '{}'),
    )
    try:
        for operation in operations:
            with pytest.raises(ProjectDataError):
                operation()
        assert fingerprints(store.run_dir('damaged')) == originals
    finally:
        manager.close()


def test_healthy_job_completes_while_damaged_project_remains_visible(tmp_path):
    store = fixture_projects(tmp_path)
    corrupt(store, 'events.jsonl')
    done = threading.Event()

    def prepare(run_id):
        def run(text, emit, check_cancel):
            emit({'type': 'fixture', 'message': 'Offline job'})
            done.set()
            return SimpleNamespace(stopped_because='done', text='Fixture complete', rounds=0, used_tools=[])
        return run, {'provider': 'offline-fixture'}

    manager = JobManager(store, Pipeline(store), Ledger(store, settings(tmp_path)), prepare)
    try:
        job = manager.submit('healthy', 'Run offline fixture', 'healthy-request')
        assert done.wait(3)
        with manager.changed:
            assert manager.changed.wait_for(lambda: manager.active is None, timeout=3)
        assert manager.get('healthy', job['job_id'])['status'] == 'succeeded'
        assert {row['run_id'] for row in store.project_catalog()} == {'healthy', 'damaged'}
    finally:
        manager.close()


def test_in_flight_damage_releases_process_slot_without_writing_original(tmp_path):
    store = fixture_projects(tmp_path)
    entered, proceed = threading.Event(), threading.Event()

    def prepare(run_id):
        def run(text, emit, check_cancel):
            entered.set()
            assert proceed.wait(3)
            store.append_chat(run_id, 'assistant', 'must not append')
        return run, {'provider': 'offline-fixture'}

    manager = JobManager(store, Pipeline(store), Ledger(store, settings(tmp_path)), prepare)
    try:
        manager.submit('damaged', 'Wait offline', 'fixture-request')
        assert entered.wait(3)
        corrupt(store, 'chat.jsonl')
        originals = fingerprints(store.run_dir('damaged'))
        proceed.set()
        with manager.changed:
            assert manager.changed.wait_for(lambda: manager.active is None, timeout=3)
        assert fingerprints(store.run_dir('damaged')) == originals
    finally:
        proceed.set()
        manager.close()


def test_health_check_waits_for_journal_append_without_lock_inversion(tmp_path):
    from studio.project_integrity import journal_lock
    store = fixture_projects(tmp_path)
    ledger = Ledger(store, settings(tmp_path))
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    path = store.run_dir('healthy') / 'chat.jsonl'
    row = b'{"at":"fixture","role":"user","text":"whole row"}\n'

    def partial_writer():
        with journal_lock(path):
            with path.open('wb') as handle:
                handle.write(row[:10])
                handle.flush()
                entered.set()
                assert release.wait(3)
                handle.write(row[10:])

    def reader():
        with ledger._locked():
            store.require_healthy('healthy')
        completed.set()

    writer = threading.Thread(target=partial_writer)
    writer.start()
    assert entered.wait(3)
    checking = threading.Thread(target=reader)
    checking.start()
    assert not completed.wait(.1)
    release.set()
    writer.join(3)
    checking.join(3)
    assert completed.is_set()
    assert store.project_integrity('healthy')['available'] is True


def test_each_event_append_checks_once_and_other_shared_writes_still_guard(tmp_path, monkeypatch):
    import studio.project_integrity as integrity
    store = fixture_projects(tmp_path)
    checked = []
    original = integrity.inspect_project

    def inspect_once(root, run_id):
        checked.append(run_id)
        return original(root, run_id)

    monkeypatch.setattr(integrity, 'inspect_project', inspect_once)
    store.append_event('healthy', 'fixture_one_check')
    assert checked == ['healthy']
    checked.clear()
    store.append_chat('healthy', 'user', 'Shared write still guards')
    assert checked == ['healthy']
    (store.run_dir('healthy') / 'approvals.jsonl').write_bytes(b'{')
    with pytest.raises(ProjectDataError):
        store.append_event('healthy', 'must_check_again')
    assert checked == ['healthy', 'healthy']


def test_costs_are_partial_not_zero_and_readable_costs_of_bad_projects_count(tmp_path):
    store = fixture_projects(tmp_path)
    ledger = Ledger(store, settings(tmp_path))
    for run_id, cost in (('healthy', 1.25), ('damaged', 2.5)):
        store.append_ledger(run_id, LedgerEntry('', run_id, 'writer', 'writing', 'fixture',
                                              1, 2, cost, 'fixture', usage_status='known'))
    corrupt(store, 'chat.jsonl')
    assert ledger.totals().estimated_usd == 3.75
    corrupt(store, 'ledger.jsonl')
    totals = ledger.totals().to_dict()
    assert totals['estimated_usd'] is None
    assert totals['known_estimated_usd'] == 1.25
    assert totals['summary_complete'] is False
    assert totals['incomplete_projects'] == ['damaged']
    assert totals['calls'] == 1  # Known subtotal, not a fabricated count of bad rows.


@pytest.mark.parametrize('payload,category', [(b'[]', 'invalid_record'), (b'\xff', 'invalid_utf8'),
                                           (b'{"run_id":"other"}', 'invalid_record')])
def test_metadata_error_has_only_safe_fixed_diagnostic(tmp_path, payload, category):
    store = fixture_projects(tmp_path)
    corrupt(store, 'run.json', payload)
    issue = inspect_project(tmp_path, 'damaged')['issues'][0]
    assert issue['category'] == category
    assert issue['bytes'] == len(payload)


@pytest.mark.parametrize('kind', ['artifact', 'artifact_body', 'session', 'session_missing', 'session_journal', 'metadata_missing'])
def test_other_startup_readers_are_isolated_without_changing_originals(tmp_path, kind):
    from studio.agent_sessions import AgentSessions
    store = fixture_projects(tmp_path)
    if kind.startswith('artifact'):
        store.write_artifact('damaged', 'intake', 1, 'Offline body', summary='Fixture',
                             author_role='orchestrator', attempt='fixture')
        path = store.run_dir('damaged') / 'artifacts' / ('intake-v1.md' if kind == 'artifact_body' else 'intake-v1.json')
        path.write_bytes(b'\xff' if kind == 'artifact_body' else b'{')
    elif kind.startswith('session'):
        row = AgentSessions(store, 'damaged').ensure(role='writer', stage='writing')
        folder = store.run_dir('damaged') / 'agent-sessions' / row['session_id']
        if kind == 'session_missing':
            (folder / 'session.json').unlink()
        else:
            (folder / ('journal.jsonl' if kind == 'session_journal' else 'session.json')).write_bytes(b'{')
    else:
        (store.run_dir('damaged') / 'run.json').unlink()
    originals = fingerprints(store.run_dir('damaged'))
    with TestClient(build_app(settings(tmp_path))) as client:
        bad = next(row for row in client.get('/api/runs').json() if row['run_id'] == 'damaged')
        assert bad['integrity']['available'] is False
        assert client.get('/api/runs/healthy/state').status_code == 200
        assert client.post('/api/runs/damaged/jobs', json={}).status_code == 409
    assert fingerprints(store.run_dir('damaged')) == originals


def test_doctor_reports_isolation_without_changing_files_or_hiding_root_errors(tmp_path):
    store = fixture_projects(tmp_path)
    corrupt(store, 'events.jsonl')
    originals = fingerprints(tmp_path)
    report = inspect(tmp_path, port=0)
    assert report['ready_to_start'] is True
    assert report['unavailable_projects'][0]['run_id'] == 'damaged'
    assert next(row for row in report['checks'] if row['name'] == 'project_integrity')['required'] is False
    assert fingerprints(tmp_path) == originals
    (tmp_path / 'config.json').write_bytes(b'{')
    assert inspect(tmp_path, port=0)['ready_to_start'] is False


def test_unknown_catalog_failure_is_not_swallowed_at_startup(tmp_path, monkeypatch):
    fixture_projects(tmp_path)
    monkeypatch.setattr(Store, 'project_catalog', lambda self: (_ for _ in ()).throw(RuntimeError('fixture bug')))
    app = build_app(settings(tmp_path))
    with pytest.raises(RuntimeError, match='fixture bug'):
        with TestClient(app):
            pass


def test_local_html_and_static_revalidate_cache_without_changing_api(tmp_path):
    fixture_projects(tmp_path)
    with TestClient(build_app(settings(tmp_path))) as client:
        page = client.get('/')
        assert page.headers['cache-control'] == 'no-cache'
        static = client.get('/static/app.js')
        assert static.headers['cache-control'] == 'no-cache'
        etag = static.headers['etag']
        validated = client.get('/static/app.js', headers={'if-none-match': etag})
        assert validated.status_code == 304
        assert validated.headers['cache-control'] == 'no-cache'
        assert 'cache-control' not in client.get('/api/runs').headers
