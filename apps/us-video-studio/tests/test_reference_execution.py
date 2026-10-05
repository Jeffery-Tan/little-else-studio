"""Reference steps follow their exact owning Job, including conversation jobs."""
import pytest

from studio.reference_video import _execution_job_id, task_state, reuse_analysis
from studio.store import Store
from studio.agent_runtime import bind_task, reset_task
from studio.tiktok_reference import ReferenceFetchError
from fakes import final_turn
from test_bailian_omni import native
from test_image_analysis import scene, wait_job
from test_natural_reference import chat, inspect, scripted
from test_reference_video import URL, request as reference_request, submit, downloader
from test_server import RUN


@pytest.fixture
def journal(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Reference execution fixture', 'Synthetic input only')
    store.append_event(RUN, 'job_created', job_id='conversation-1', job_kind='conversation')
    store.append_event(RUN, 'reference_video_started', reference_id='reference-1',
                       request={'intent': 'analyze', 'instruction': '只拆解参考'})
    store.append_event(RUN, 'reference_video_execution', reference_id='reference-1',
                       job_id='conversation-1')
    return store


@pytest.mark.parametrize('status', ['failed', 'blocked', 'cancelled', 'interrupted'])
def test_conversation_terminal_state_is_visible_on_its_reference(journal, status):
    journal.append_event(RUN, 'job_status', job_id='conversation-1',
                         status=status, reason='Fixture terminal reason')
    task = task_state(journal, RUN)[0]
    assert task['status'] == 'started'
    assert task['job_id'] == 'conversation-1'
    assert task['execution_status'] == status
    assert task['failure_reason'] == 'Fixture terminal reason'


def test_retry_uses_new_owner_and_ignores_old_failure(journal):
    journal.append_event(RUN, 'reference_video_step_failed', reference_id='reference-1',
                         job_id='conversation-1', reason='Old download failed')
    journal.append_event(RUN, 'job_status', job_id='conversation-1', status='failed')
    journal.append_event(RUN, 'job_created', job_id='conversation-2', job_kind='conversation')
    journal.append_event(RUN, 'reference_video_execution', reference_id='reference-1',
                         job_id='conversation-2')
    journal.append_event(RUN, 'job_status', job_id='conversation-2', status='running')
    journal.append_event(RUN, 'job_status', job_id='conversation-1', status='interrupted',
                         reason='Late old termination')
    task = task_state(journal, RUN)[0]
    assert task['job_id'] == 'conversation-2' and task['execution_status'] == 'running'
    assert 'failure_reason' not in task


def test_saved_analysis_remains_completed_if_later_planning_fails(journal):
    journal.append_event(RUN, 'reference_video_completed', reference_id='reference-1')
    journal.append_event(RUN, 'job_status', job_id='conversation-1', status='failed',
                         reason='Planner unavailable')
    task = task_state(journal, RUN)[0]
    assert task['status'] == 'completed'
    assert task['execution_status'] == 'failed' and task['failure_reason'] == 'Planner unavailable'


def test_step_error_survives_generic_conversation_completion_reason(journal):
    journal.append_event(RUN, 'reference_video_step_failed', reference_id='reference-1',
                         job_id='conversation-1', reason='Download timed out; upload the original')
    journal.append_event(RUN, 'job_status', job_id='conversation-1', status='blocked',
                         reason='Deliverable incomplete')
    assert task_state(journal, RUN)[0]['failure_reason'] == 'Download timed out; upload the original'


def test_unknown_execution_is_not_inferred_from_an_unrelated_job(journal):
    journal.append_event(RUN, 'reference_video_execution', reference_id='reference-1',
                         job_id='missing-owner')
    journal.append_event(RUN, 'job_created', job_id='unrelated', job_kind='conversation')
    journal.append_event(RUN, 'job_status', job_id='unrelated', status='succeeded')
    task = task_state(journal, RUN)[0]
    assert task['job_id'] == 'missing-owner' and task['execution_status'] == 'unknown'
    assert _execution_job_id(journal, RUN) is None


def test_active_role_uses_its_exact_parent_job(journal):
    journal.append_event(RUN, 'agent_runtime_task_created', task_id='role-task',
                         task={'parent_job_id': 'conversation-1'})
    activation = bind_task('role-task')
    try:
        assert _execution_job_id(journal, RUN) == 'conversation-1'
    finally:
        reset_task(activation)


def test_saved_candidate_without_ready_event_recovers_report_without_observation(tmp_path):
    from test_reference_admission import selected_reference
    store, target = selected_reference(tmp_path)
    store.append_event(RUN, 'job_created', job_id='explicit-recovery', job_kind='conversation')
    store.append_event(RUN, 'job_status', job_id='explicit-recovery', status='running')
    activation = bind_task('explicit-recovery')
    try:
        reused = reuse_analysis(store, RUN, target['reference_id'])
    finally:
        reset_task(activation)
    task = task_state(store, RUN)[0]
    assert reused['reused'] and task['report'] == reused['report']
    assert task['analysis_id'] == target['analysis_id']
    assert task['candidate']['analysis_id'] == target['analysis_id']
    assert task['status'] == 'completed' and task['job_id'] == 'explicit-recovery'


def test_failed_natural_download_has_job_identity_and_original_error(native, monkeypatch):
    def fail(*_):
        raise ReferenceFetchError('Fixture download timed out; upload the original')
    monkeypatch.setattr('studio.reference_video.fetch_reference', fail)
    scripted(native, monkeypatch, [
        inspect('analyze', source=URL, quote='拆解这条参考'),
        final_turn('请上传原视频继续'),
    ])
    job = chat(native, '拆解这条参考 ' + URL)
    tasks = native[0].get(f'/api/runs/{RUN}/reference-videos').json()['tasks']
    assert len(tasks) == 1
    task = tasks[0]
    assert task['job_id'] == job['job_id']
    assert task['execution_status'] == job['status']
    assert task['status'] == 'started'
    assert task['failure_reason'] == 'Fixture download timed out; upload the original'
    assert not any(event['kind'] == 'video_analysis_candidate' for event in native[1].read_events(RUN))


def test_explicit_reuse_rebinds_saved_analysis_without_another_observation(native, monkeypatch):
    payload, raw = reference_request(native, remote=True)
    downloads = downloader(monkeypatch, raw)
    original = submit(native, payload)
    native[1].append_event(RUN, 'job_status', job_id=original['job_id'],
                           status='failed', reason='Old planner failed')
    assert task_state(native[1], RUN)[0]['failure_reason'] == 'Old planner failed'
    observation_calls = len(native[2].calls)
    scripted(native, monkeypatch, [inspect('analyze', source=URL, quote='拆解这条参考')])
    job = chat(native, '拆解这条参考 ' + URL, request_id='reuse-reference')
    assert job['status'] == 'succeeded', job
    task = task_state(native[1], RUN)[0]
    assert task['job_id'] == job['job_id'] and task['execution_status'] == 'succeeded'
    assert task['status'] == 'completed' and 'failure_reason' not in task
    assert len(native[2].calls) == observation_calls + 1
    assert downloads == [URL]


def test_exact_ui_target_rebinds_saved_analysis_without_observation_connection(native, monkeypatch):
    payload, raw = reference_request(native, remote=True)
    downloads = downloader(monkeypatch, raw)
    original = submit(native, payload)
    native[1].append_event(RUN, 'job_status', job_id=original['job_id'],
                           status='failed', reason='Old planner failed')
    task = task_state(native[1], RUN)[0]
    target = dict(run_id=RUN, reference_id=task['reference_id'],
                  analysis_id=task['candidate']['analysis_id'],
                  candidate_sha256=task['candidate']['candidate_sha256'])
    monkeypatch.setattr('studio.agents.bailian_omni.connection_info', lambda: None)
    observation_calls = len(native[2].calls)
    text = '拆解这条选中的参考'
    scripted(native, monkeypatch, [inspect('analyze', source=URL, quote=text)])
    response = native[0].post(f'/api/runs/{RUN}/jobs', json=dict(
        text=text, request_id='reuse-ui-target', reference_target=target,
        reference_consent=False))
    assert response.status_code == 200, response.text
    job = wait_job(native[0], response.json())
    assert job['status'] == 'succeeded', job
    assert not job['model_config'].get('reference_analysis')
    task = task_state(native[1], RUN)[0]
    assert task['job_id'] == job['job_id'] and task['execution_status'] == 'succeeded'
    assert task['status'] == 'completed' and 'failure_reason' not in task
    assert len(native[2].calls) == observation_calls + 1
    assert downloads == [URL]
