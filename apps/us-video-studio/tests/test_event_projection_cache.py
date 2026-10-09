import json
import pytest
from studio.store import Store
from studio.models import PipelineError


def test_cached_rows_are_detached_and_external_changes_invalidate(tmp_path):
    store = Store(tmp_path)
    store.create_run('cache', 'cache', 'test only')
    store.append_event('cache', 'example', values=['original'])
    first = store.read_events('cache')
    first[-1]['values'].append('not persisted')
    assert store.read_events('cache')[-1]['values'] == ['original']
    other = Store(tmp_path)
    other.append_event('cache', 'external', value=2)
    assert store.read_events('cache')[-1]['kind'] == 'external'
    path = store.run_dir('cache') / 'events.jsonl'
    path.write_text('{broken', encoding='utf-8')
    with pytest.raises(PipelineError):
        store.read_events('cache')


def test_unchanged_reads_reuse_parsed_projection(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.create_run('cache', 'cache', 'test only')
    store.read_events('cache')
    def unexpected(*args):
        raise AssertionError('unchanged journal should not be reparsed')
    monkeypatch.setattr('studio.store._read_jsonl', unexpected)
    assert store.read_events('cache') == []


def test_external_append_between_validation_and_cache_install_is_not_hidden(tmp_path, monkeypatch):
    """A late external append must never get the earlier rows' cache entry."""
    store = Store(tmp_path)
    store.create_run('cache', 'cache', 'test only')
    store.append_event('cache', 'first')
    path = store.run_dir('cache') / 'events.jsonl'
    original = store._event_stamp
    checks = 0

    def append_after_validation(candidate):
        nonlocal checks
        stamp = original(candidate)
        checks += 1
        if checks == 2:
            # The read has validated its original stamp. An external writer
            # lands just before cache installation, without our run lock.
            with path.open('a', encoding='utf-8') as target:
                target.write(json.dumps({'seq': 2, 'at': '2026-10-09T00:00:00Z',
                                         'kind': 'external'}) + '\n')
        return stamp

    monkeypatch.setattr(store, '_event_stamp', append_after_validation)
    assert [row['kind'] for row in store.read_events('cache')] == ['first']
    assert [row['kind'] for row in store.read_events('cache')] == ['first', 'external']
    appended = store.append_event('cache', 'third')
    assert appended['seq'] == 3


def test_external_append_after_own_write_is_seen_by_next_read(tmp_path, monkeypatch):
    import studio.store as module
    store = Store(tmp_path)
    store.create_run('cache', 'cache', 'test only')
    store.read_events('cache')
    original = module._append_jsonl_after_check

    def append_with_external_tail(path, record):
        original(path, record)
        original(path, {'seq': record['seq'] + 1, 'at': record['at'], 'kind': 'external'})

    monkeypatch.setattr(module, '_append_jsonl_after_check', append_with_external_tail)
    store.append_event('cache', 'own')
    assert [row['kind'] for row in store.read_events('cache')] == ['own', 'external']
