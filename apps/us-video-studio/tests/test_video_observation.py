"""Decode real synthetic videos; only model inference is faked. Never network."""
import base64
import copy
import hashlib
import io
import json
from fractions import Fraction

import av
import pytest
from PIL import Image

from studio.agents.client import JobCancelled, Turn, Usage
from studio.creative_evidence import creative_evidence
from studio.evidence import evidence_fingerprint, evidence_snapshot
from studio.ledger import Ledger
from studio.materials import accept_upload
from studio.material_observations import ObservationError
from studio.video_observation import parse_result, public_packet, sample_video, record_details
from test_image_analysis import scene, wait_job, confirm
from test_server import RUN

RESULT = {'observations': [{'frame_ids': ['f1'], 'visible': 'First red scene'},
                           {'frame_ids': ['f6'], 'visible': 'Later blue scene'}],
          'reference_methods': [{'supports': [0, 1], 'mechanism': 'Scene contrast may signal a change',
              'non_transferable': 'No product performance or audience effects are established',
              'test_change': 'Test a before/after visual contrast using current product evidence'}],
          'limitations': ['No continuous action or sound was observed']}


def video_bytes(*, seconds=4, audio=False):
    output = io.BytesIO()
    with av.open(output, mode='w', format='mp4') as container:
        stream = container.add_stream('mpeg4', rate=4)
        stream.width, stream.height, stream.pix_fmt = 64, 96, 'yuv420p'
        sound = container.add_stream('aac', rate=8000) if audio else None
        for index in range(seconds * 4):
            frame = av.VideoFrame.from_image(Image.new('RGB', (64, 96), 'red' if index < 8 else 'blue'))
            frame.pts = index
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        if sound is not None:
            frame = av.AudioFrame(format='fltp', layout='mono', samples=8000)
            frame.sample_rate, frame.pts, frame.time_base = 8000, 0, Fraction(1, 8000)
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            for packet in sound.encode(frame):
                container.mux(packet)
            for packet in sound.encode():
                container.mux(packet)
    return output.getvalue()


def start(scene, *, result=None, **over):
    http, store, model, payload, _, _ = scene
    raw = video_bytes(audio=True)
    accept_upload(store, RUN, 'reference.mp4', raw)
    model.turn = Turn(text=json.dumps(RESULT if result is None else result), stop_reason='end_turn',
                      model='fixture-vision', usage=Usage(110, 25))
    request = {**payload, 'name': 'reference.mp4', 'purpose': 'reference',
               'expected_sha256': hashlib.sha256(raw).hexdigest(), **over}
    response = http.post(f'/api/runs/{RUN}/video-analysis', json=request)
    return response, request, raw


def current_candidate(http):
    return next(c for c in http.get(f'/api/runs/{RUN}/image-analysis').json()['candidates'] if c.get('media_kind') == 'video')


def test_actual_decode_time_and_frame_pixels_with_audio_explicitly_unobserved():
    packet = sample_video(video_bytes(audio=True))
    assert packet['audio_track_present'] is True and packet['audio_observed'] is False
    assert packet['duration_seconds'] == 4
    stamps = [f['time_seconds'] for f in packet['frames']]
    assert 2 <= len(stamps) <= 12 and stamps == sorted(set(stamps))
    assert stamps[0] == 0 and stamps[-1] == 3.75
    first, last = [Image.open(io.BytesIO(f['data'])).getpixel((30, 30)) for f in [packet['frames'][0], packet['frames'][-1]]]
    assert first[0] > first[2] + 100 and last[2] > last[0] + 100
    assert all(hashlib.sha256(f['data']).hexdigest() == f['sha256'] for f in packet['frames'])
    assert 'data' not in json.dumps(public_packet(packet))


def test_real_video_frames_cross_model_boundary_then_selected_observations_enter_creative_context(scene):
    http, store, model, _, _, settings = scene
    response, payload, raw = start(scene)
    assert response.status_code == 200, response.text
    assert wait_job(http, response.json())['status'] == 'succeeded'
    assert creative_evidence(store, RUN)['reference_observations'] == []
    candidate = current_candidate(http)
    blocks = model.calls[0]['messages'][0]['content']
    images = [b for b in blocks if b['type'] == 'image']
    assert len(images) == len(candidate['sampling']['frames'])
    for block, frame in zip(images, candidate['sampling']['frames']):
        assert hashlib.sha256(base64.b64decode(block['source']['data'])).hexdigest() == frame['sha256']
    assert all(b['type'] in ('image', 'text') for b in blocks)
    assert base64.b64encode(raw).decode() not in json.dumps(model.calls)
    assert model.calls[0]['tools'] == []
    before = evidence_fingerprint(store, RUN)
    saved = confirm(http, candidate, [0, 1])
    assert saved.status_code == 200, saved.text
    assert evidence_fingerprint(store, RUN) != before
    prepared = creative_evidence(store, RUN)
    assert prepared['product_observations'] == []
    ref = prepared['reference_observations'][0]
    assert ref['sampling']['audio_observed'] is False and ref['level'] == 'frames_observed'
    assert len(ref['reference_methods']) == 1 and len(ref['timeline']) == 2
    assert any('声画' in gap['reason'] for gap in prepared['gaps'])
    assert Ledger(store, settings).entries(RUN)[0].stage == 'video_analysis'
    assert http.post(f'/api/runs/{RUN}/video-analysis', json=payload).json()['job_id'] == response.json()['job_id']
    assert len(model.calls) == 1
    journal = (store.run_dir(RUN) / 'events.jsonl').read_text('utf-8')
    assert images[0]['source']['data'] not in journal


def test_unselected_support_cannot_carry_reference_hypothesis_into_evidence(scene):
    http, store, model, _, _, _ = scene
    response, _, _ = start(scene)
    assert wait_job(http, response.json())['status'] == 'succeeded'
    assert confirm(http, current_candidate(http), [0]).status_code == 200
    ref = creative_evidence(store, RUN)['reference_observations'][0]
    assert len(ref['findings']) == 1 and ref['reference_methods'] == []
    assert ref['timeline'][0]['finding_index'] == 0


@pytest.mark.parametrize('fault', ['frame', 'support', 'product', 'shape'])
def test_invalid_analysis_is_not_a_candidate_and_is_not_retried(scene, fault):
    result = copy.deepcopy(RESULT)
    if fault == 'frame': result['observations'][0]['frame_ids'] = ['f999']
    if fault == 'support': result['reference_methods'][0]['supports'] = [99]
    if fault == 'shape': result['observations'] = [None]
    response, _, _ = start(scene, result=result, **({'purpose': 'product'} if fault == 'product' else {}))
    http, store, model, _, _, settings = scene
    assert wait_job(http, response.json())['status'] == 'failed'
    assert not http.get(f'/api/runs/{RUN}/image-analysis').json()['candidates']
    assert len(model.calls) == 1 and Ledger(store, settings).entries(RUN)[0].usage_status == 'known'


def test_cancel_before_sampling_never_reaches_provider(scene, monkeypatch):
    def cancelled(*args):
        raise JobCancelled('fixture cancelled')
    monkeypatch.setattr('studio.video_observation.sample_video', cancelled)
    response, _, _ = start(scene)
    http, store, model, _, _, settings = scene
    assert wait_job(http, response.json())['status'] == 'cancelled'
    assert model.calls == [] and Ledger(store, settings).entries(RUN) == []


def test_local_invalid_video_fails_before_model(scene):
    http, store, model, payload, _, settings = scene
    accept_upload(store, RUN, 'broken.mp4', b'not a video')
    response = http.post(f'/api/runs/{RUN}/video-analysis', json={**payload, 'name': 'broken.mp4',
        'expected_sha256': hashlib.sha256(b'not a video').hexdigest()})
    assert wait_job(http, response.json())['status'] == 'failed'
    assert model.calls == [] and Ledger(store, settings).entries(RUN) == []


def test_changed_video_cannot_adopt_old_observation(scene):
    http, store, _, _, _, _ = scene
    response, _, _ = start(scene)
    assert wait_job(http, response.json())['status'] == 'succeeded'
    candidate = current_candidate(http)
    (store.run_dir(RUN) / 'materials/reference.mp4').write_bytes(video_bytes(seconds=2))
    assert confirm(http, candidate, [0, 1]).status_code == 409
    assert creative_evidence(store, RUN)['reference_observations'] == []


def test_timestamp_or_audio_coverage_tampering_cannot_enter_evidence(scene):
    http, store, _, _, _, _ = scene
    response, _, _ = start(scene)
    assert wait_job(http, response.json())['status'] == 'succeeded'
    assert confirm(http, current_candidate(http), [0, 1]).status_code == 200
    path = store.run_dir(RUN) / 'evidence/observations.json'
    original = json.loads(path.read_text('utf-8'))
    for field in ('timestamp', 'audio'):
        tampered = copy.deepcopy(original)
        row = tampered['observations'][0]
        if field == 'timestamp': row['timeline'][0]['time_seconds'] = [999]
        else: row['sampling']['audio_observed'] = True
        path.write_text(json.dumps(tampered), encoding='utf-8')
        snapshot = evidence_snapshot(store, RUN)
        assert snapshot['errors']
        assert not next(m for m in snapshot['materials'] if m['name'] == 'reference.mp4')['observations']


def test_long_video_and_cancelled_decode_are_bounded():
    with pytest.raises(ObservationError, match='90'):
        sample_video(video_bytes(seconds=91))
    def cancel():
        raise JobCancelled('cancelled')
    with pytest.raises(JobCancelled):
        sample_video(video_bytes(), cancel)
