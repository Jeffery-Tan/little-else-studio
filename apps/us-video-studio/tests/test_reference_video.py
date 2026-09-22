"""Unified reference tasks: real codecs, isolated jobs, fake network and models."""
import hashlib
import json

import pytest

from studio.agents.client import Turn, Usage, ModelError
from studio.creative_evidence import creative_evidence
from studio.materials import accept_upload
from studio.reference_video import task_state
from studio.tiktok_reference import ReferenceFetchError
from test_bailian_omni import native, RESULT, MODEL, BASE
from test_image_analysis import scene, wait_job
from test_video_observation import video_bytes
from test_server import RUN

URL = 'https://www.tiktok.com/@fixture/video/7250000000000000000'


def request(native, *, remote=False, intent='analyze'):
    http, store, model, _, _, _ = native
    raw = video_bytes(audio=True)
    model.turn = Turn(text=json.dumps(RESULT), stop_reason='end_turn', model=MODEL, usage=Usage(18032, 2000))
    p = dict(request_id='request-0001', reference_id='reference-0001', intent=intent,
             consent=True, expected_revision=1, expected_endpoint=BASE)
    if remote:
        p['source_url'] = URL
    else:
        accept_upload(store, RUN, 'local.mp4', raw)
        p.update(name='local.mp4', expected_sha256=hashlib.sha256(raw).hexdigest())
    return p, raw


def submit(native, p):
    response = native[0].post(f'/api/runs/{RUN}/reference-videos', json=p)
    assert response.status_code == 200, response.text
    return wait_job(native[0], response.json())


def downloader(monkeypatch, raw):
    calls = []
    def fetch(url, cancel):
        cancel()
        calls.append(url)
        return raw, dict(video_id='7250000000000000000', source_url=url, canonical_url=URL,
                         sha256=hashlib.sha256(raw).hexdigest(), extractor_version='fixture')
    monkeypatch.setattr('studio.reference_video.fetch_reference', fetch)
    return calls


def test_local_analysis_stops_at_report_without_adopting_creation_evidence(native):
    p, _ = request(native)
    p['instruction'] = '重点拆解前3秒的声音与动作'
    job = submit(native, p)
    assert job['status'] == 'succeeded', job
    evidence = creative_evidence(native[1], RUN)
    assert not evidence['errors'] and not evidence['product_observations']
    assert evidence['reference_observations'] == []
    task = task_state(native[1], RUN)[0]
    assert task['status'] == 'completed' and '声画' in task['report']
    assert submit(native, p)['job_id'] == job['job_id']
    assert submit(native, {**p, 'request_id': 'request-0002'})['status'] == 'succeeded'
    assert len(native[2].calls) == 1
    assert p['instruction'] in json.dumps(native[2].calls[0]['messages'], ensure_ascii=False)


def test_remote_analysis_failure_resumes_without_download(native, monkeypatch):
    p, raw = request(native, remote=True)
    downloads = downloader(monkeypatch, raw)
    native[2].turn = Turn(text='bad json', stop_reason='end_turn', model=MODEL, usage=Usage(10, 2))
    assert submit(native, p)['status'] == 'failed'
    assert task_state(native[1], RUN)[0]['status'] == 'downloaded'
    native[2].turn = Turn(text=json.dumps(RESULT), stop_reason='end_turn', model=MODEL, usage=Usage(10, 2))
    assert submit(native, {**p, 'request_id': 'request-0002'})['status'] == 'succeeded'
    assert downloads == [URL] and len(native[2].calls) == 2


def test_recreate_planner_failure_reuses_completed_analysis(native, monkeypatch):
    p, raw = request(native, remote=True, intent='recreate')
    downloads = downloader(monkeypatch, raw)
    model = native[2]
    original = model.complete
    def complete(**kwargs):
        if model.calls:
            model.error = ModelError(503, 'fixture failure')
        return original(**kwargs)
    monkeypatch.setattr(model, 'complete', complete)
    assert submit(native, p)['status'] == 'failed'
    assert task_state(native[1], RUN)[0]['status'] == 'analyzed'
    monkeypatch.setattr(model, 'complete', original)
    model.error = None
    model.turn = Turn(text='结合当前商品继续策划。', stop_reason='end_turn', model='fixture', usage=Usage(20, 4))
    assert submit(native, {**p, 'request_id': 'request-0002'})['status'] == 'succeeded'
    assert len(model.calls) == 3 and len(downloads) == 1
    assert len(native[0].get(f'/api/runs/{RUN}/image-analysis').json()['candidates']) == 1
    assert not any(e['kind'].startswith('production_') for e in native[1].read_events(RUN))


def test_link_failure_can_continue_with_file_and_preserve_intent(native, monkeypatch):
    p, raw = request(native, remote=True)
    def fail(*_): raise ReferenceFetchError('请上传原视频继续')
    monkeypatch.setattr('studio.reference_video.fetch_reference', fail)
    assert submit(native, p)['status'] == 'failed'
    assert not native[2].calls
    accept_upload(native[1], RUN, 'fallback.mp4', raw)
    retry = {**p, 'request_id': 'request-0002', 'source_url': None, 'name': 'fallback.mp4',
             'expected_sha256': hashlib.sha256(raw).hexdigest()}
    assert submit(native, retry)['status'] == 'succeeded'
    task = task_state(native[1], RUN)[0]
    assert task['source_url'] == URL and task['origin'] == 'user_upload'


@pytest.mark.parametrize('raw', [b'not a video', video_bytes()])
def test_invalid_or_silent_download_does_not_call_model(native, monkeypatch, raw):
    p, _ = request(native, remote=True)
    downloader(monkeypatch, raw)
    assert submit(native, p)['status'] == 'failed'
    assert not native[2].calls
    assert not list((native[1].run_dir(RUN) / 'materials').glob('tiktok-*.mp4'))


@pytest.mark.parametrize('change', [dict(intent='recreate'), dict(instruction='change'),
    dict(source_url=URL.replace('7250', '7251')), dict(name='other.mp4', expected_sha256='0'*64)])
def test_resume_cannot_change_task_source_or_intent(native, change):
    p, _ = request(native)
    assert submit(native, p)['status'] == 'succeeded'
    response = native[0].post(f'/api/runs/{RUN}/reference-videos', json={**p, **change, 'request_id': 'request-0002'})
    assert response.status_code in (409, 422)
    assert len(native[2].calls) == 1


@pytest.mark.parametrize('change', [dict(consent=False), dict(expected_revision=999), dict(expected_endpoint='https://changed.invalid')])
def test_preflight_does_not_download_or_bill(native, monkeypatch, change):
    p, raw = request(native, remote=True)
    downloads = downloader(monkeypatch, raw)
    response = native[0].post(f'/api/runs/{RUN}/reference-videos', json={**p, **change})
    assert response.status_code == 409 and not downloads and not native[2].calls
