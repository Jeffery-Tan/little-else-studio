"""Real shipped JS preparation payloads against disposable offline stores."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from studio.materials import accept_upload
from studio.production import (DraftIn, ProductionError, _read_state_without_service,
                               attempt_records, prepare_draft)
from test_shot_identity_versions import revise, success
from test_shots import RUN, image_input, prepared


def video_history(tmp_path):
    store, pipe = prepared(tmp_path)
    image = image_input(store, pipe, 'image-old', shot_id='shot-01')
    image_attempt = success(store, image, 1)
    frame = attempt_records(store, RUN)[image_attempt]['output']
    source = store.latest_artifact(RUN, 'writing')
    video = prepare_draft(store, pipe, RUN, DraftIn(request_id='video-old', kind='video',
        prompt='Synthetic video movement', source_version=source.version, source_sha256=source.sha256,
        model='wan3.0-video', duration=3, resolution='720P', shot_id='shot-01', reference=frame))
    raw = b'Explicit offline video fixture; not generated or decoded.'
    name, sha = 'synthetic-clip.mp4', hashlib.sha256(raw).hexdigest()
    accept_upload(store, RUN, name, raw)
    store.append_event(RUN, 'production_attempt', attempt_id='old-video-result',
        draft_id=video['draft_id'], draft_sha256=video['draft_sha256'], media_kind='video',
        status='succeeded', output=None)
    store.append_event(RUN, 'production_output', attempt_id='old-video-result', output={'name':name, 'sha256':sha})
    current = revise(store, pipe, deepcopy(source.structured['segments']))
    return store, pipe, image, image_attempt, video, current, sha


def candidate(state):
    return next(row for row in state['reuse_candidates'] if row['source_attempt_id'] == 'old-video-result')


def reuse_current_image(store, pipe, image, image_attempt, current):
    return prepare_draft(store, pipe, RUN, DraftIn(request_id='image-reused', kind='image',
        prompt=image['shot_prompt'], source_version=current.version, source_sha256=current.sha256,
        model='codex-image', shot_id='shot-01', reuse_attempt_id=image_attempt))


def ui_payload(state):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to exercise the shipped JS payload boundary')
    app = Path(__file__).resolve().parents[1]
    script = """
const fs=require('node:fs'),ui=require('./studio/web/production.js');
const data=JSON.parse(fs.readFileSync(0,'utf8'));
const candidate=data.reuse_candidates.find(row=>row.source_attempt_id==='old-video-result');
process.stdout.write(JSON.stringify(ui.reusePayload(candidate,data,'video-from-ui')));
"""
    result = subprocess.run([node, '-e', script], input=json.dumps(state), text=True,
                            capture_output=True, cwd=app, timeout=20, check=True)
    return json.loads(result.stdout)


def test_real_js_video_reuse_payload_binds_current_first_frame_and_is_accepted(tmp_path):
    store, pipe, image, image_attempt, old_video, current, video_sha = video_history(tmp_path)
    before_frame = candidate(_read_state_without_service(store, pipe, RUN))
    assert not before_frame['eligible'] and '当前首帧' in before_frame['reason']
    reused_image = reuse_current_image(store, pipe, image, image_attempt, current)
    state = _read_state_without_service(store, pipe, RUN)
    projected = candidate(state)
    assert projected['eligible'] and projected['changed_dependencies'] == []
    frame = next(row['output'] for row in attempt_records(store, RUN).values()
                 if row['draft_id'] == reused_image['draft_id'])
    assert projected['reuse_reference'] == {key:frame[key] for key in ('name', 'sha256')} | {'purpose':'continuity'}
    assert frame['name'] != old_video['reference']['name']
    assert projected['source_version'] == current.version
    assert projected['source_sha256'] == current.sha256
    payload = ui_payload(state)
    assert payload['references'] == [projected['reuse_reference']]
    assert not {'reference', 'consent', 'nonce', 'authorization_id'} & set(payload)
    draft = prepare_draft(store, pipe, RUN, DraftIn(**payload))
    output = next(row for row in attempt_records(store, RUN).values() if row['draft_id'] == draft['draft_id'])
    assert output['provider'] == 'local_reuse' and output['output']['sha256'] == video_sha
    after = _read_state_without_service(store, pipe, RUN)
    assert not after['video_sequence'][0]['approved']
    assert not next(row for row in after['materials'] if row['name'] == frame['name'])['approved']


@pytest.mark.parametrize('problem', ['unknown', 'different_bytes', 'missing_bytes', 'wrong_shot'])
def test_video_reuse_requires_exact_known_same_shot_current_frame(tmp_path, problem):
    store, pipe, image, image_attempt, old_video, current, _ = video_history(tmp_path)
    if problem in {'unknown', 'missing_bytes'}:
        current_image = reuse_current_image(store, pipe, image, image_attempt, current)
        attempt = next(row for row in attempt_records(store, RUN).values()
                       if row['draft_id'] == current_image['draft_id'])
        if problem == 'unknown':
            store.append_event(RUN, 'production_status', attempt_id=attempt['attempt_id'], update={'status':'unknown'})
        else:
            (store.run_dir(RUN) / 'materials' / attempt['output']['name']).unlink()
    else:
        current_image = image_input(store, pipe, 'new-image', shot_id='shot-02' if problem == 'wrong_shot' else 'shot-01')
        success(store, current_image, 2)
    state = _read_state_without_service(store, pipe, RUN)
    projected = candidate(state)
    assert not projected['eligible'] and projected['reuse_reference'] is None
    before = store.read_events(RUN)
    with pytest.raises(ProductionError):
        prepare_draft(store, pipe, RUN, DraftIn(request_id='no-fallback', kind='video',
            prompt=old_video['shot_prompt'], source_version=current.version, source_sha256=current.sha256,
            model='wan3.0-video', duration=3, resolution='720P', shot_id='shot-01',
            reference=old_video['reference'], reuse_attempt_id='old-video-result'))
    assert store.read_events(RUN) == before


def test_stale_ui_frame_reference_is_rejected_without_recording_or_submitting(tmp_path):
    store, pipe, image, image_attempt, old_video, current, _ = video_history(tmp_path)
    reuse_current_image(store, pipe, image, image_attempt, current)
    payload = ui_payload(_read_state_without_service(store, pipe, RUN))
    payload['references'] = [{**old_video['reference'], 'purpose':'continuity'}]
    before = store.read_events(RUN)
    with pytest.raises(ProductionError, match='当前准确首帧'):
        prepare_draft(store, pipe, RUN, DraftIn(**payload))
    assert store.read_events(RUN) == before
