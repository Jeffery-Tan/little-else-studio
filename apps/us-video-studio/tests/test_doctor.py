"""Read-only startup diagnosis; no model, network or real project data."""
import socket

from studio.doctor import inspect, runtime_identity, code_fingerprint, _fingerprint_paths, mark_new_root
from studio.jobs import WriterLease


def by_name(report, name):
    return next(check for check in report['checks'] if check['name'] == name)


def test_missing_data_root_is_reported_without_creation(tmp_path):
    missing = tmp_path / 'not-restored'
    report = inspect(missing, port=0)
    assert not report['ready_to_start']
    assert by_name(report, 'data_root')['status'] == 'error'
    assert not missing.exists()


def test_empty_existing_root_needs_explicit_new_intent(tmp_path):
    assert by_name(inspect(tmp_path, port=0), 'project_data')['status'] == 'error'
    assert by_name(inspect(tmp_path, port=0, allow_new=True), 'project_data')['status'] == 'ok'
    assert not (tmp_path / '.studio-root.json').exists()
    mark_new_root(tmp_path)
    assert by_name(inspect(tmp_path, port=0), 'project_data')['status'] == 'ok'


def test_lock_file_alone_is_not_project_data_and_probe_does_not_write(tmp_path):
    lock = tmp_path / '.studio-writer.lock'
    lock.write_bytes(b'')
    assert by_name(inspect(tmp_path, port=0), 'project_data')['status'] == 'error'
    assert lock.read_bytes() == b''


def test_writer_and_port_conflicts_have_distinct_remedies(tmp_path):
    with socket.socket() as listener, WriterLease(tmp_path):
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        report = inspect(tmp_path, port=listener.getsockname()[1])
    assert by_name(report, 'writer')['status'] == 'error'
    assert by_name(report, 'port')['status'] == 'error'
    assert '原服务' in by_name(report, 'writer')['remedy']
    assert '端口' in by_name(report, 'port')['remedy']


def test_runtime_identity_omits_raw_path_and_credentials(tmp_path):
    data = runtime_identity(tmp_path)
    assert data['code_fingerprint'] and data['started_at']
    assert len(data['data_root_id']) == 12
    assert str(tmp_path) not in str(data)
    assert '未验证模型连接' in data['meaning']


def test_fingerprint_covers_extracted_route_modules(monkeypatch):
    from pathlib import Path
    route = next(path for path in _fingerprint_paths() if path.name == 'http_approvals.py')
    before = code_fingerprint()
    read_bytes = Path.read_bytes
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_bytes', lambda path: read_bytes(path) + b'changed'
                      if path == route else read_bytes(path))
        assert code_fingerprint() != before


def test_fingerprint_includes_loaded_shared_learning_code():
    assert any(path.as_posix().endswith('/scripts/creative_learning.py')
               for path in _fingerprint_paths())


def test_unavailable_source_metadata_is_unknown_without_aborting_runtime(monkeypatch, tmp_path):
    import subprocess
    from studio import doctor
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    data = doctor.runtime_identity(tmp_path)
    assert data['source_revision'] is None and data['source_modified'] is None
    assert data['code_fingerprint'] and data['started_at']


def test_source_version_reports_loaded_revision_and_local_changes_without_diff_contents(monkeypatch):
    import subprocess
    from studio import doctor
    results = iter([subprocess.CompletedProcess([], 0, '641020175af2\n', ''),
                    subprocess.CompletedProcess([], 0, b'?? apps/us-video-studio/studio/local.py\n', b'')])
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: next(results))
    assert doctor.source_version() == {'source_revision': '641020175af2', 'source_modified': True}


def test_source_version_distinguishes_clean_checkout_and_unavailable_status(monkeypatch):
    import subprocess
    from studio import doctor
    for status, expected in ((0, False), (128, None)):
        results = iter([subprocess.CompletedProcess([], 0, '641020175af2\n', ''),
                        subprocess.CompletedProcess([], status, b'', b'')])
        monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: next(results))
        assert doctor.source_version() == {'source_revision': '641020175af2', 'source_modified': expected}
