from local_http import TestClient
from studio.server import build_app
from studio.store import Store
from studio.jobs import JobManager
from studio.pipeline import Pipeline
from studio.ledger import Ledger
from studio.models import PipelineError
from test_server import settings_for
import pytest


def test_delete_restore_preserves_content_and_persists_across_restart(tmp_path):
    store=Store(tmp_path)
    store.create_run('keep', '保留项目', 'brief')
    store.create_run('remove', '<待删除>', 'brief')
    store.append_chat('remove', 'user', '原始对话')
    material=store.run_dir('remove')/'materials'/'reference.png'
    material.parent.mkdir();material.write_bytes(b'fixture')
    settings=settings_for(tmp_path)
    with TestClient(build_app(settings)) as http:
        assert http.delete('/api/runs/remove').status_code==200
        assert http.delete('/api/runs/remove').status_code==200
        assert [r['run_id'] for r in http.get('/api/runs').json()]==['keep']
        assert http.get('/api/trash').json()[0]['run_id']=='remove'
        assert http.get('/api/runs/remove/timeline').status_code==410
        assert http.post('/api/runs/remove/jobs',json={'text':'继续','request_id':'deleted'}).status_code==410
        assert len([e for e in store.read_events('remove') if e['kind']=='project_deleted'])==1
        assert material.read_bytes()==b'fixture'
        assert len(store.read_chat('remove'))==1
        assert len(store.list_runs())==2  # Global accounting still sees deleted runs.
    with TestClient(build_app(settings)) as http:
        assert len(http.get('/api/trash').json())==1
        assert http.post('/api/runs/remove/restore',json={}).status_code==200
        assert http.post('/api/runs/remove/restore',json={}).status_code==200
        assert http.get('/api/trash').json()==[]
        assert len(http.get('/api/runs').json())==2
        assert http.get('/api/runs/remove/timeline').status_code==200
        assert material.read_bytes()==b'fixture'
        assert len(store.read_chat('remove'))==1


def test_active_project_cannot_be_deleted_but_other_projects_can(tmp_path):
    store=Store(tmp_path)
    for run in ['busy','idle']:store.create_run(run,run,'brief')
    app=build_app(settings_for(tmp_path))
    with TestClient(app) as http:
        jobs=app.state.jobs
        jobs.active=('busy','fake-job')
        try:
            assert http.delete('/api/runs/busy').status_code==409
            assert not store.deletion_state('busy')
            assert http.delete('/api/runs/idle').status_code==200
        finally:jobs.active=None


def test_deleted_first_message_cannot_silently_resurrect_or_create_duplicate(tmp_path):
    with TestClient(build_app(settings_for(tmp_path))) as http:
        payload={'text':'new idea','request_id':'first'}
        run=http.post('/api/conversations',json=payload).json()
        http.delete('/api/runs/'+run['run_id'])
        assert http.post('/api/conversations',json=payload).status_code==410
        assert http.get('/api/runs').json()==[]
        assert len(http.get('/api/trash').json())==1
        assert http.delete('/api/runs/missing').status_code==404


def test_job_guard_rechecks_deleted_under_submission_lock(tmp_path):
    settings=settings_for(tmp_path);store=Store(tmp_path)
    store.create_run('deleted','deleted','brief');store.set_deleted('deleted',True)
    manager=JobManager(store,Pipeline(store),Ledger(store,settings),lambda _:pytest.fail('must not prepare a model'))
    try:
        with pytest.raises(PipelineError,match='回收站'):
            manager.submit('deleted','hello','request')
    finally:manager.close()
