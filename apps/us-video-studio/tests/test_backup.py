import json
import zipfile

import pytest

from studio import backup


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'source'
    run = root / 'runs' / 'test-run'
    run.mkdir(parents=True)
    (run / 'run.json').write_text('{"run_id":"test-run"}', encoding='utf-8')
    (run / 'events.jsonl').write_bytes(b'{"kind":"saved"}\n')
    (run / 'materials').mkdir()
    (run / 'materials' / 'sample.png').write_bytes(b'fake-media')
    (root / 'connections.json').write_text(json.dumps({
        'schema_version': 1, 'revision': 3, 'roles': {'writer': {'connection': 'deepseek', 'model': 'test'}},
        'connections': [{'id': 'deepseek', 'name': 'DeepSeek', 'provider': 'deepseek',
                         'base_url': 'https://api.deepseek.com/v1', 'pricing': {},
                         'protected_key': 'private-test-value-never-export'}]}), encoding='utf-8')
    (root / 'config.json').write_text('{"budget_limit_usd":10,"api_key":"private-test-key-value"}', encoding='utf-8')
    (root / 'runtime-venv').mkdir()
    (root / 'runtime-venv' / 'secret.txt').write_text('not included')
    return root


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(backup, 'live_state', lambda url, ids: {'test-run': 'stable'})


def export(source, output, **kwargs):
    return backup.export_backup(source, output, live_url='http://127.0.0.1:4760', **kwargs)


def test_roundtrip_keeps_project_bytes_and_roles_without_credentials(source, tmp_path):
    archive = tmp_path / 'backup.zip'
    manifest = export(source, archive)
    target = tmp_path / 'laptop'
    backup.restore_backup(archive, target)
    assert manifest['run_ids'] == ['test-run']
    for name in ('run.json', 'events.jsonl', 'materials/sample.png'):
        assert (target / 'runs/test-run' / name).read_bytes() == (source / 'runs/test-run' / name).read_bytes()
    connections = json.loads((target / 'connections.json').read_bytes())
    assert connections['roles']['writer']['connection'] == 'deepseek'
    assert 'protected_key' not in connections['connections'][0]
    assert json.loads((target / 'config.json').read_bytes()) == {'budget_limit_usd': 10}
    assert not (target / 'runtime-venv').exists()
    with zipfile.ZipFile(archive) as z:
        assert all(b'private-test' not in z.read(name) for name in z.namelist())


def test_existing_target_and_backup_are_never_overwritten(source, tmp_path):
    archive = tmp_path / 'backup.zip'
    export(source, archive)
    original = archive.read_bytes()
    with pytest.raises(ValueError, match='new backup'):
        export(source, archive)
    with pytest.raises(ValueError, match='new directory'):
        backup.restore_backup(archive, source)
    assert archive.read_bytes() == original


def test_source_mutation_removes_incomplete_backup(source, tmp_path, monkeypatch):
    archive = tmp_path / 'backup.zip'
    original = backup.portable_bytes
    def changed(name, data):
        if name.endswith('sample.png'):
            (source / name).write_bytes(b'changed-during-copy')
        return original(name, data)
    monkeypatch.setattr(backup, 'portable_bytes', changed)
    with pytest.raises(ValueError, match='Source changed'):
        export(source, archive)
    assert not archive.exists()


def test_activity_change_refuses_export(source, tmp_path, monkeypatch):
    states = iter([{'revision': 1}, {'revision': 2}])
    monkeypatch.setattr(backup, 'live_state', lambda *_: next(states))
    archive = tmp_path / 'backup.zip'
    with pytest.raises(ValueError, match='Studio changed'):
        export(source, archive)
    assert not archive.exists()


def test_secret_in_project_fails_closed(source, tmp_path):
    path = source / 'runs/test-run/chat.jsonl'
    path.write_text('{"text":"Bearer ' + 's' * 30 + '"}', encoding='utf-8')
    archive = tmp_path / 'backup.zip'
    with pytest.raises(ValueError, match='Potential credential') as exc:
        export(source, archive)
    assert 's' * 30 not in str(exc.value)
    assert not archive.exists()


def test_only_uncurated_history_json_can_be_omitted(source, tmp_path):
    name = 'runs/test-run/history/archive/old/response.json'
    path = source / name
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"url":"https://example.invalid/?Signature=private"}')
    manifest_path = source / 'runs/test-run/history/manifest.json'
    history = {'files': [{'id': 'old', 'archive_path': 'old/response.json'}], 'documents': [], 'materials': []}
    manifest_path.write_text(json.dumps(history), encoding='utf-8')
    result = export(source, tmp_path / 'ok.zip', omit=[name])
    assert result['omissions'][0]['path'] == name
    assert path.exists()
    history['documents'] = [{'id': 'old'}]
    manifest_path.write_text(json.dumps(history), encoding='utf-8')
    with pytest.raises(ValueError, match='curated'):
        export(source, tmp_path / 'bad.zip', omit=[name])
    with pytest.raises(ValueError, match='historical JSON'):
        export(source, tmp_path / 'bad.zip', omit=['runs/test-run/run.json'])


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'runs/test/../../escape',
                                  'runs/test/C:/secret', 'runs\\test\\secret', 'runs/test/file.',
                                  'runs/test//file', 'credentials.json', 'runs/test/NUL',
                                  'runs/test/AUX.txt', 'runs/test/com1/log', 'runs/test/bad?.png'])
def test_unsafe_archive_paths_rejected(name):
    with pytest.raises(ValueError):
        backup.safe_name(name)


def test_corrupt_payload_or_extra_file_cannot_restore(source, tmp_path):
    original = tmp_path / 'original.zip'
    export(source, original)
    for label, extra in [('tampered', False), ('extra', True)]:
        bad = tmp_path / (label + '.zip')
        with zipfile.ZipFile(original) as src, zipfile.ZipFile(bad, 'w') as dst:
            for name in src.namelist():
                data = src.read(name)
                if not extra and name.endswith('sample.png'):
                    data = b'corruption'
                dst.writestr(name, data)
            if extra:
                dst.writestr('runs/test-run/extra.txt', b'extra')
        with pytest.raises(ValueError):
            backup.restore_backup(bad, tmp_path / label)
        assert not (tmp_path / label).exists()


def test_symlink_zip_member_rejected(tmp_path):
    path = tmp_path / 'link.zip'
    with zipfile.ZipFile(path, 'w') as z:
        info = zipfile.ZipInfo('runs/test-run/link')
        info.external_attr = 0o120777 << 16
        z.writestr(info, b'/outside')
    with pytest.raises(ValueError, match='linked'):
        backup.verify_backup(path)


def test_offline_export_respects_running_writer(source, tmp_path):
    from studio.jobs import WriterLease
    from studio.models import PipelineError
    with WriterLease(source), pytest.raises(PipelineError, match='写入进程'):
        backup.export_backup(source, tmp_path / 'blocked.zip')
