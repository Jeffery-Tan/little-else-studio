"""The original single-video worker only queries after releasing text capacity."""
import threading
from types import SimpleNamespace

from studio.agents.wan_video import WanVideoResult
from test_production import BASE, RUN, ENDPOINT, action, ready, scene, wait, workspace


def test_original_video_wait_releases_text_slot_without_a_second_submission(scene, monkeypatch):
    http, store, _, _, _, videos, _ = scene
    item = ready(scene)
    entered, release = threading.Event(), threading.Event()
    def pending(task_id, *, endpoint):
        videos.polls.append(task_id)
        entered.set()
        assert release.wait(10)
        return WanVideoResult(task_id, 'PENDING', endpoint)
    monkeypatch.setattr(videos, 'poll', pending)
    generated = action(scene, item, 'generate')
    assert generated.status_code == 200, generated.text
    original = generated.json()
    assert entered.wait(2)
    jobs = http.app.state.jobs
    try:
        projected = http.get(f'/api/runs/{RUN}/jobs/{original["job_id"]}').json()
        assert projected['lane'] == 'query' and projected['phase'] == 'remote_waiting'
        assert jobs.active is None
        assert workspace(scene)['active'] is True
        assert len(videos.calls) == 1
        duplicate = http.post(BASE + '/attempts/generate-1/refresh', json={'request_id': 'duplicate'})
        assert duplicate.status_code == 409 and '已经在查询' in duplicate.text
        def text_prepare(_rid):
            def run(_text, _emit, check):
                # Same actual ledger lease contract used by a text model call.
                with jobs.ledger.call_guard(concurrent=True, check_cancel=check):
                    return SimpleNamespace(stopped_because='done', text='text complete', rounds=1, used_tools=[])
            return run, {'source': 'offline'}
        text = jobs.submit(RUN, '继续文字讨论', 'text-during-video', prepare=text_prepare, enqueue=True)
        assert wait(scene, SimpleResponse(text))['status'] == 'succeeded'
        assert jobs.get(RUN, original['job_id'])['status'] == 'running'
        jobs.cancel(RUN, original['job_id'])
    finally:
        release.set()
    assert wait(scene, generated)['status'] == 'cancelled'
    attempt = workspace(scene)['drafts'][0]['attempts'][0]
    assert attempt['task_id'] == 'fixture-task' and attempt['status'] == 'submitted'
    assert len(videos.calls) == 1
    assert jobs.queue()['items'] == []


class SimpleResponse:
    status_code = 200
    text = 'synthetic'
    def __init__(self, value):
        self.value = value
    def json(self):
        return self.value
