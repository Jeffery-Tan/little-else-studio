"""Pinned method versions and bounded retrieval; no real calls or shared data."""
import copy
import json

import pytest

from studio.learning import LearningConflict, SKILLS, METHOD_CARDS, _json
from studio.method_snapshots import checked_document
from test_learning import env


def edit_skill(service, suffix='\nNEW METHOD TEXT'):
    path = service.repository_root / '.agents/skills' / SKILLS[0] / 'SKILL.md'
    path.write_text(path.read_text(encoding='utf-8') + suffix, encoding='utf-8')


def refresh(service, status, request_id='methods-refresh-1', *, methods=True):
    return service.refresh_run_pin('run-one', status['run_library_sha256'],
        status['current_library_sha256'], request_id,
        **({'expected_run_methods_sha256': status['run_methods_sha256'],
            'expected_current_methods_sha256': status['current_methods_sha256']} if methods else {}))


def test_all_creative_documents_are_frozen_before_the_next_role(env):
    service, store, _ = env
    first = service.context('run-one', 'writer')
    original = (store.run_dir('run-one') / 'learning/method-pin.json').read_bytes()
    edit_skill(service)
    card_path = service.repository_root / METHOD_CARDS['visual-design']['file']
    card_path.write_text('A NEW VISUAL METHOD', encoding='utf-8')
    director = service.context('run-one', 'director')
    assert 'NEW METHOD TEXT' not in director['text'] and 'A NEW VISUAL METHOD' not in director['text']
    assert director['methods_sha256'] == first['methods_sha256']
    assert (store.run_dir('run-one') / 'learning/method-pin.json').read_bytes() == original
    assert service.read_method_document('visual-design', role='director', run_id='run-one')['text'].startswith('Fixture')
    catalog = service.skill_catalog('director', run_id='run-one')
    frozen = next(row for row in catalog['skills'] if row['id'] == SKILLS[0])
    assert frozen['sha256'] == director['skills'][0]['sha256']
    assert 'creative-package-contract' not in json.loads(original)['documents']


def test_method_only_refresh_requires_exact_visible_hashes_and_is_idempotent(env):
    service, store, _ = env
    before = service.context('run-one', 'director')
    edit_skill(service)
    status = service.pin_status('run-one')
    assert status['refresh_available'] and status['methods_refresh_available']
    assert not status['library_refresh_available']
    assert '+NEW METHOD TEXT' in status['method_changes'][0]['diff']
    with pytest.raises(LearningConflict, match='准确新旧方法'):
        refresh(service, status, methods=False)
    with pytest.raises(LearningConflict, match='Method documents changed'):
        refresh(service, dict(status, current_methods_sha256='0' * 64))
    after = refresh(service, status)
    assert not after['refresh_available']
    assert refresh(service, status) == after
    assert 'NEW METHOD TEXT' in service.context('run-one', 'director')['text']
    assert service.context('run-one', 'director')['library_sha256'] == before['library_sha256']
    assert len([row for row in store.read_events('run-one') if row['kind'] == 'learning_library_refreshed']) == 1


def test_library_only_refresh_preserves_method_version(env):
    service, _, _ = env
    first = service.context('run-one', 'director')
    edit_skill(service)
    library = json.loads(service.library_path.read_text(encoding='utf-8'))
    library['library_version'] += 1
    service.library_path.write_text(_json(library), encoding='utf-8')
    after = refresh(service, service.pin_status('run-one'), methods=False)
    assert after['methods_refresh_available'] and not after['library_refresh_available']
    context = service.context('run-one', 'director')
    assert context['methods_sha256'] == first['methods_sha256']
    assert 'NEW METHOD TEXT' not in context['text']


def test_legacy_context_recovers_only_saved_full_documents_not_current_sources(env):
    service, store, artifact = env
    original = service.context('run-one', 'writer')
    (store.run_dir('run-one') / 'learning/method-pin.json').unlink()
    edit_skill(service)
    writer = service.context('run-one', 'writer')
    assert writer['skills'] == original['skills']
    assert SKILLS[0] in writer['missing_methods']
    with pytest.raises(LearningConflict, match='缺少准确方法正文'):
        service.context('run-one', 'director')
    assert service.context('run-one', 'orchestrator', intent='status')['skills'] == []
    assert store.read_artifact('run-one', artifact.stage, artifact.version).sha256 == artifact.sha256
    status = service.pin_status('run-one')
    assert status['method_status'] == 'partial'
    refresh(service, status)
    assert 'NEW METHOD TEXT' in service.context('run-one', 'director')['text']


def test_missing_legacy_snapshot_cannot_be_filled_from_current_repository(env):
    service, store, _ = env
    original = service.context('run-one', 'writer')
    (store.run_dir('run-one') / 'original-unused.txt').write_text('untouched', encoding='utf-8')
    (store.run_dir('run-one') / 'learning/method-pin.json').unlink()
    (store.run_dir('run-one') / original['evidence_ref']).unlink()
    status = service.pin_status('run-one')
    assert set(SKILLS) <= set(status['missing_methods'])
    with pytest.raises(LearningConflict, match='缺少准确方法正文'):
        service.context('run-one', 'writer')


def test_corrupted_method_pin_is_not_silently_rebuilt(env):
    service, store, _ = env
    service.context('run-one', 'writer')
    path = store.run_dir('run-one') / 'learning/method-pin.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    data['documents'][SKILLS[1]]['text'] = 'CORRUPT'
    path.write_text(_json(data), encoding='utf-8')
    raw = path.read_bytes()
    with pytest.raises(LearningConflict, match='固定方法快照无法核对'):
        service.context('run-one', 'writer')
    assert path.read_bytes() == raw


def test_bom_legacy_document_retains_exact_byte_fingerprint():
    import hashlib
    source = {'id': 'legacy', 'text': 'Exact text',
              'sha256': hashlib.sha256(b'\xef\xbb\xbfExact text').hexdigest()}
    assert checked_document(source)['utf8_bom'] is True
    with pytest.raises(ValueError, match='fingerprint'):
        checked_document(dict(source, text='Changed'))


def test_legacy_library_pin_without_document_history_remains_a_visible_gap(env, monkeypatch):
    service, store, _ = env
    service.context('run-one', 'writer')
    (store.run_dir('run-one') / 'learning/method-pin.json').unlink()
    original_read = store.read_events
    monkeypatch.setattr(store, 'read_events', lambda run: [row for row in original_read(run)
        if row['kind'] not in {'learning_context_loaded', 'learning_document_loaded'}])
    status = service.pin_status('run-one')
    assert status['method_status'] == 'partial' and 'us-creator-video' in status['missing_methods']
    with pytest.raises(LearningConflict, match='缺少准确方法正文'):
        service.read_method_document('us-creator-video', role='writer', run_id='run-one')


def test_relevance_ranks_late_method_before_directory_budget_and_loads_at_most_two(env):
    service, _, _ = env
    library = json.loads(service.library_path.read_text(encoding='utf-8'))
    base = library['methods'][0]
    library['methods'] = [dict(copy.deepcopy(base), id=f'method-{index:02d}', when='unrelated shape',
                               decision='unrelated shape') for index in range(12)]
    library['methods'].extend([dict(copy.deepcopy(base), id=f'caption-{index}',
        when='caption dialogue', decision='caption dialogue has a specific reason') for index in range(3)])
    service.library_path.write_text(_json(library), encoding='utf-8')
    result = service.context('run-one', 'writer', task_query='caption dialogue', tags=[])
    assert result['method_directory']['methods'][0]['id'].startswith('caption-')
    assert len(result['method_directory']['methods']) == 8
    assert len(result['retrieved_methods']) == 2
    assert all(row['method']['id'].startswith('caption-') for row in result['retrieved_methods'])
    assert all(row['evidence'] for row in result['retrieved_methods'])
    assert all('procedure' not in row for row in result['method_directory']['methods'])
    assert 'hidden-case' not in _json(result) and 'pending-case' not in _json(result)


def test_no_relevance_loads_no_complete_method_but_keeps_eligible_directory(env):
    service, _, _ = env
    result = service.context('run-one', 'writer', task_query='zebra unrelated', tags=[])
    assert result['retrieved_methods'] == []
    assert result['method_directory']['methods']
    assert not any(row['kind'] == 'method_usage_recorded' for row in service.store.read_events('run-one'))
