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


def test_reference_current_work_uses_event_order_when_timestamps_share_a_second(native):
    store = native[1]
    store.append_event(RUN, 'attempt_submitted', stage='writing', sha256='a' * 64)
    store.append_event(RUN, 'reference_video_started', reference_id='ordered-reference',
                       request={'intent': 'analyze', 'instruction': '只拆解'})
    assert task_state(store, RUN)[0]['after_creative_work'] is True
    store.append_event(RUN, 'production_draft', draft={'draft_id': 'later-work'})
    assert task_state(store, RUN)[0]['after_creative_work'] is False


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


def test_reference_breakdown_and_prompt_edits_keep_source_and_versions(native):
    p, _ = request(native)
    assert submit(native, p)['status'] == 'succeeded'
    url = f'/api/runs/{RUN}/reference-videos'
    task = native[0].get(url).json()['tasks'][0]
    candidate = task['candidate']
    assert candidate['timeline'] and candidate['audiovisual']['audio_sent']
    assert candidate['reference_methods']
    edit = dict(request_id='prompt-edit-001', analysis_id=candidate['analysis_id'],
                candidate_sha256=candidate['candidate_sha256'], expected_prompt_sha256=None,
                expected_prompt_revision=None,
                prompt='  分镜一：展示当前商品可核对的外观。  ')
    endpoint = f'{url}/{p["reference_id"]}/prompt'
    first = native[0].post(endpoint, json=edit)
    assert first.status_code == 200, first.text
    assert first.json()['prompt']['text'] == edit['prompt'].strip()
    assert native[0].post(endpoint, json=edit).json() == first.json()
    saved = native[0].get(url).json()['tasks'][0]['prompt']
    assert saved['sha256'] == first.json()['prompt']['sha256']
    assert saved['analysis_id'] == candidate['analysis_id']
    conflict = native[0].post(endpoint, json={**edit, 'request_id': 'prompt-edit-002',
        'prompt': '另一版', 'expected_prompt_sha256': None})
    assert conflict.status_code == 409
    updated = native[0].post(endpoint, json={**edit, 'request_id': 'prompt-edit-003',
        'prompt': '第二版', 'expected_prompt_sha256': saved['sha256'],
        'expected_prompt_revision': saved['revision']})
    assert updated.status_code == 200
    assert native[0].get(url).json()['tasks'][0]['prompt']['text'] == '第二版'
    stale_revision = native[0].post(endpoint, json={**edit, 'request_id': 'prompt-edit-004',
        'prompt': '第三版', 'expected_prompt_sha256': updated.json()['prompt']['sha256'],
        'expected_prompt_revision': saved['revision']})
    assert stale_revision.status_code == 409


def test_remote_analysis_failure_resumes_without_download(native, monkeypatch):
    p, raw = request(native, remote=True)
    downloads = downloader(monkeypatch, raw)
    native[2].turn = Turn(text='bad json', stop_reason='end_turn', model=MODEL, usage=Usage(10, 2))
    assert submit(native, p)['status'] == 'failed'
    failed = task_state(native[1], RUN)[0]
    assert failed['status'] == 'downloaded' and failed['execution_status'] == 'failed'
    native[2].turn = Turn(text=json.dumps(RESULT), stop_reason='end_turn', model=MODEL, usage=Usage(10, 2))
    assert submit(native, {**p, 'request_id': 'request-0002'})['status'] == 'succeeded'
    assert task_state(native[1], RUN)[0]['execution_status'] == 'succeeded'
    assert downloads == [URL] and len(native[2].calls) == 2


@pytest.mark.parametrize('failed', [False, True])
def test_canonical_alias_keeps_original_task_and_cached_source(native, monkeypatch, failed):
    p, raw = request(native, remote=True)
    p['source_url'] = 'https://vm.tiktok.com/SyntheticAlias/'
    downloads = downloader(monkeypatch, raw)
    if failed:
        native[2].turn = Turn(text='bad json', stop_reason='end_turn', model=MODEL, usage=Usage(10, 2))
    assert submit(native, p)['status'] == ('failed' if failed else 'succeeded')
    native[2].turn = Turn(text=json.dumps(RESULT), stop_reason='end_turn', model=MODEL, usage=Usage(10, 2))
    observe = native[0].app.state.observe_reference
    result = observe(RUN, URL, 'Read the saved source', lambda _: None, lambda: None,
                     endpoint=BASE, revision=1)
    assert result['reference_id'] == p['reference_id'] and result['reused'] is not failed
    assert downloads == [p['source_url']] and len(native[2].calls) == (2 if failed else 1)
    result = observe(RUN, URL, 'Reuse completed observation', lambda _: None, lambda: None,
                     endpoint=BASE, revision=1)
    assert result['reference_id'] == p['reference_id'] and result['reused'] is True
    assert len(task_state(native[1], RUN)) == 1 and len(native[2].calls) == (2 if failed else 1)


@pytest.mark.parametrize('source_kind', ['local', 'url', 'downloaded_file'])
def test_explicit_conversation_retry_keeps_source_and_uses_current_scope(native, monkeypatch, source_kind):
    remote = source_kind != 'local'
    p, raw = request(native, remote=remote)
    original_instruction = '重点拆解开场钩子与手持观感'
    current_instruction = '无口播，重点拆解装载动作与画面推进'
    p['instruction'] = original_instruction
    downloads = downloader(monkeypatch, raw)
    native[2].turn = Turn(text='bad json', stop_reason='end_turn', model=MODEL, usage=Usage(10, 2))
    assert submit(native, p)['status'] == 'failed'
    assert len(native[2].calls) == 1
    source_before = next(e for e in native[1].read_events(RUN) if e['kind'] == 'reference_video_source')
    source = URL if source_kind == 'url' else source_before['source']['name']
    native[2].turn = Turn(text=json.dumps(RESULT), stop_reason='end_turn', model=MODEL, usage=Usage(10, 2))

    # This internal observer is reached only after the orchestrator validates
    # this turn's explicit user quote; no new Job or automatic retry is created.
    observe = native[0].app.state.observe_reference
    result = observe(RUN, source, current_instruction, lambda _: None, lambda: None,
                     endpoint=BASE, revision=1)
    assert result['reference_id'] == p['reference_id'] and result['reused'] is False
    assert len(native[2].calls) == 2 and downloads == ([URL] if remote else [])
    sent = json.dumps(native[2].calls[1]['messages'], ensure_ascii=False)
    assert current_instruction in sent and original_instruction not in sent
    events = native[1].read_events(RUN)
    started = [e for e in events if e['kind'] == 'reference_video_started']
    sources = [e for e in events if e['kind'] == 'reference_video_source']
    requests = [e for e in events if e['kind'] == 'reference_video_analysis_requested']
    assert len(started) == len(sources) == 1
    assert started[0]['request']['instruction'] == original_instruction
    assert sources[0]['source'] == source_before['source']
    assert [e['instruction'] for e in requests] == [original_instruction, current_instruction]
    assert all(e['reference_id'] == p['reference_id'] for e in requests)
    assert len({e['request_id'] for e in requests}) == 2
    assert all(e['sha256'] == hashlib.sha256(raw).hexdigest() for e in requests)

    reused = observe(RUN, source, '继续沿用已完成的观察', lambda _: None, lambda: None,
                     endpoint=BASE, revision=1)
    assert reused['reference_id'] == p['reference_id'] and reused['reused'] is True
    assert len(native[2].calls) == 2 and downloads == ([URL] if remote else [])
    assert len([e for e in native[1].read_events(RUN)
                if e['kind'] == 'reference_video_analysis_requested']) == 2


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
    failed = task_state(native[1], RUN)[0]
    assert failed['status'] == 'started' and failed['execution_status'] == 'failed'
    assert '请上传原视频继续' in failed['failure_reason']
    accept_upload(native[1], RUN, 'fallback.mp4', raw)
    retry = {**p, 'request_id': 'request-0002', 'source_url': None, 'name': 'fallback.mp4',
             'expected_sha256': hashlib.sha256(raw).hexdigest()}
    assert submit(native, retry)['status'] == 'succeeded'
    task = task_state(native[1], RUN)[0]
    assert task['source_url'] == URL and task['origin'] == 'user_upload'
    assert task['execution_status'] == 'succeeded' and 'failure_reason' not in task


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
