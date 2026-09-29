import io
import json
import zipfile

from local_http import TestClient

from studio.progress import project_progress
from studio.server import build_app
from studio.store import Store
from studio.pipeline import Pipeline
from test_server import settings_for


def test_next_action_follows_real_prerequisites_and_user_decision(tmp_path):
    store = Store(tmp_path)
    store.create_run('fixture', 'Fixture', 'Offline fixture')
    pipeline = Pipeline(store)
    assert project_progress(pipeline.state('fixture').to_dict())['stage'] == 'intake'
    attempt = pipeline.start_attempt('fixture', 'intake')
    pipeline.submit('fixture', 'intake', attempt.token, content='Product brief', summary='Brief')
    out = project_progress(pipeline.state('fixture').to_dict())
    assert out['stage'] == 'direction' and out['completed'] == 1
    attempt = pipeline.start_attempt('fixture', 'direction')
    assert project_progress(pipeline.state('fixture').to_dict())['action'] == 'recovery'
    pipeline.submit('fixture', 'direction', attempt.token, content='Direction', summary='Direction')
    out = project_progress(pipeline.state('fixture').to_dict())
    assert out['label'] == '阅读并决定' and out['action'] == 'review'
    assert '采用或退回' in out['detail']


def test_old_approval_does_not_mark_legacy_artifact_ready(tmp_path):
    store = Store(tmp_path)
    store.create_run('fixture', 'Fixture', 'Offline fixture')
    store.write_artifact('fixture', 'intake', 1, 'Old text', summary='Legacy', author_role='orchestrator', attempt='test')
    path = store.run_dir('fixture') / 'artifacts/intake-v1.json'
    value = json.loads(path.read_text())
    import hashlib
    value['digest_algorithm'] = 'body-v1'
    value['sha256'] = hashlib.sha256(b'Old text').hexdigest()
    path.write_text(json.dumps(value), encoding='utf-8')
    out = project_progress(Pipeline(store).state('fixture').to_dict())
    assert out['label'] == '准备新版本'
    assert out['completed'] == 0
    assert '不继承历史批准' in out['prompt']


def test_delivery_is_read_only_and_labels_unfinished_work(tmp_path):
    settings = settings_for(tmp_path)
    store = Store(tmp_path)
    store.create_run('fixture', 'Fixture', 'Offline fixture')
    pipeline = Pipeline(store)
    attempt = pipeline.start_attempt('fixture', 'intake')
    pipeline.submit('fixture', 'intake', attempt.token, content='Product brief', summary='Brief')
    before = store.read_events('fixture')
    with TestClient(build_app(settings, client_factory=lambda: None)) as client:
        summary = client.get('/api/runs/fixture/delivery').json()
        assert not summary['ready']
        assert summary['documents'][0]['status'] == '已整理'
        assert not summary['documents'][0]['adopted']
        result = client.get('/api/runs/fixture/delivery/download')
        assert result.status_code == 200
        with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
            assert archive.read('intake-v1.md').decode('utf-8-sig') == 'Product brief'
            assert '创意方向' in archive.read('README.md').decode('utf-8-sig')
            manifest = json.loads(archive.read('delivery-manifest.json'))
            assert manifest['documents'][0]['sha256'] == summary['documents'][0]['sha256']
        assert client.get('/api/runs/fixture/timeline').json()['progress']['stage'] == 'direction'
        assert client.get('/api/runs').json()[0]['progress']['completed'] == 1
        assert client.get('/api/runs/missing/delivery').status_code == 404
    assert store.read_events('fixture') == before


def test_delivery_refuses_altered_document(tmp_path):
    store = Store(tmp_path)
    store.create_run('fixture', 'Fixture', 'Offline fixture')
    artifact = store.write_artifact('fixture', 'intake', 1, 'Original', summary='Brief', author_role='orchestrator', attempt='test')
    (store.run_dir('fixture') / 'artifacts/intake-v1.md').write_text('Changed', encoding='utf-8')
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: None)) as client:
        assert client.get('/api/runs/fixture/delivery/download').status_code == 409
        assert not client.get('/api/runs/fixture/delivery').json()['documents'][0]['integrity_ok']


def test_material_http_conflicts_and_invalidation(tmp_path):
    from studio.materials import accept_upload
    store = Store(tmp_path)
    store.create_run('fixture', 'Fixture', 'Offline fixture')
    accept_upload(store, 'fixture', 'photo.png', b'isolated test bytes')
    pipeline = Pipeline(store)
    attempt = pipeline.start_attempt('fixture', 'intake')
    pipeline.submit('fixture', 'intake', attempt.token, content='Old input', summary='Brief')
    app = build_app(settings_for(tmp_path), client_factory=lambda: None)
    with TestClient(app) as client:
        before = client.get('/api/runs/fixture/material-observations').json()
        item = before['materials'][0]
        payload = {'name':item['name'], 'expected_sha256':item['sha256'],
                   'expected_manifest_sha256':before['expected_manifest_sha256'],
                   'purpose':'reference', 'level':'image_observed', 'coverage':'Fixture only',
                   'findings':['Fixture note'], 'limitations':['No product facts']}
        app.state.jobs.active = ('fixture', 'test-job')
        assert client.post('/api/runs/fixture/material-observations',json=payload).status_code == 409
        app.state.jobs.active = None
        saved = client.post('/api/runs/fixture/material-observations',json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()['materials'][0]['status'] == 'has_observations'
        assert client.post('/api/runs/fixture/material-observations',json=payload).status_code == 409
        stage = client.get('/api/runs/fixture/state').json()['stages'][0]
        assert stage['stale']
        assert store.read_artifact_text('fixture','intake',1) == 'Old input'
        assert client.get('/api/runs/fixture/ledger').json()['rows'] == []
