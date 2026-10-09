"""Native sound/video inputs, real local codecs, fake provider and credentials."""
import base64
import copy
import hashlib
import json

import httpx
import pytest

from studio.agents.bailian_omni import MODEL, CONNECTION, OmniClient, freeze
from studio.agents.client import ModelError, Turn, Usage
from studio.agents.wan_video import WanCredentials
from studio.audiovisual_observation import prepare_video, parse_result, _compact
from studio.creative_evidence import creative_evidence
from studio.evidence import evidence_snapshot
from studio.ledger import Ledger
from studio.material_observations import ObservationError
from studio.materials import accept_upload
from test_image_analysis import scene, wait_job, confirm
from test_video_observation import video_bytes
from test_server import RUN

BASE = 'https://fixture.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'
RESULT = {'observations': [{'start_seconds': 0, 'end_seconds': .8,
    'visible': 'A red scene', 'audible': 'Silence in the decoded audio', 'relationship': 'No audible action cue'},
    {'start_seconds': 2, 'end_seconds': 3.9, 'visible': 'A blue scene', 'audible': '', 'relationship': ''}],
    'reference_methods': [{'supports': [0, 1], 'mechanism': 'Compare the two scenes',
        'non_transferable': 'No performance established', 'test_change': 'Test a scene contrast'}],
    'limitations': ['Audio ends before the video']}


@pytest.fixture
def native(scene, monkeypatch):
    monkeypatch.setattr('studio.agents.wan_video.existing_credentials_available', lambda: True)
    monkeypatch.setattr('studio.agents.wan_video.existing_endpoint', lambda: BASE.replace('/compatible-mode/v1', '/api/v1'))
    monkeypatch.setattr('studio.agents.wan_video.load_existing_credentials', lambda: WanCredentials('fixture-key', 'fixture'))
    return scene


def start(native, result=None, **over):
    http, store, model, payload, _, _ = native
    raw = video_bytes(audio=True)
    accept_upload(store, RUN, 'native.mp4', raw)
    model.turn = Turn(text=json.dumps(result or RESULT), stop_reason='end_turn', model=MODEL, usage=Usage(18032, 2000))
    request = {**payload, 'name': 'native.mp4', 'expected_sha256': hashlib.sha256(raw).hexdigest(),
               'connection_id': CONNECTION, 'model': MODEL, 'purpose': 'reference', 'expected_endpoint': BASE, **over}
    return http.post(f'/api/runs/{RUN}/video-analysis', json=request), request, raw


def test_native_input_full_bytes_then_selected_timeline_enters_creative_context(native):
    http, store, model, _, _, settings = native
    response, request, raw = start(native)
    assert response.status_code == 200, response.text
    assert wait_job(http, response.json())['status'] == 'succeeded'
    c = http.get(f'/api/runs/{RUN}/image-analysis').json()['candidates'][0]
    blocks = model.calls[0]['messages'][0]['content']
    assert base64.b64decode(blocks[1]['source']['data']) == raw
    assert c['audiovisual']['audio_sent'] and c['audiovisual']['audio_ranges'][0][1] < 2
    assert any('部分时间' in item for item in c['limitations'])
    assert not creative_evidence(store, RUN)['reference_observations']
    assert confirm(http, c, [1]).status_code == 200
    context = creative_evidence(store, RUN)
    item = context['reference_observations'][0]
    assert item['level'] == 'audiovisual_review' and item['timeline'][0]['finding_index'] == 1
    assert item['reference_methods'] == [] and item['audiovisual']['source_sha256'] == hashlib.sha256(raw).hexdigest()
    assert not context['product_observations']
    row = Ledger(store, settings).entries(RUN)[0]
    assert row.estimated_usd is None and row.estimated_cny == pytest.approx(.0198256)
    assert row.input_cny_per_mtok == .8 and row.output_cny_per_mtok == 2.7
    assert c['estimated_cny'] == row.estimated_cny
    assert http.post(f'/api/runs/{RUN}/video-analysis', json=request).json()['job_id'] == response.json()['job_id']
    assert len(model.calls) == 1
    events = (store.run_dir(RUN) / 'events.jsonl').read_text('utf-8')
    assert blocks[1]['source']['data'] not in events and 'fixture-key' not in events


@pytest.mark.parametrize('change', [dict(expected_endpoint='https://changed.invalid'), dict(model='qwen-vl'), dict(expected_revision=999), dict(consent=False)])
def test_changed_or_wrong_native_request_fails_before_provider(native, change):
    response, _, _ = start(native, **change)
    assert response.status_code == 409
    assert native[2].calls == []


def test_public_discovery_does_not_decrypt(native, monkeypatch):
    def fail():
        raise AssertionError('must not decrypt')
    monkeypatch.setattr('studio.agents.wan_video.load_existing_credentials', fail)
    doc = native[0].get(f'/api/runs/{RUN}/image-analysis').json()
    assert doc['bailian_omni']['suggested_model'] == MODEL
    assert 'fixture-key' not in json.dumps(doc)


@pytest.mark.parametrize('fault', ['time', 'audio_range', 'support'])
def test_invalid_model_observations_retain_cny_usage_without_retry(native, fault):
    result = copy.deepcopy(RESULT)
    if fault == 'time': result['observations'][0]['end_seconds'] = 100
    if fault == 'audio_range': result['observations'][1]['audible'] = 'fabricated sound'
    if fault == 'support': result['reference_methods'][0]['supports'] = [99]
    response, _, _ = start(native, result)
    assert wait_job(native[0], response.json())['status'] == 'failed'
    assert len(native[2].calls) == 1
    assert Ledger(native[1], native[5]).entries(RUN)[0].estimated_cny is not None
    assert not native[0].get(f'/api/runs/{RUN}/image-analysis').json()['candidates']
    rejected = [e for e in native[1].read_events(RUN) if e['kind'] == 'analysis_response_rejected']
    assert len(rejected) == 1 and rejected[0]['validation_phase'] == 'observations'
    assert rejected[0]['call_id'] == Ledger(native[1], native[5]).entries(RUN)[0].call_id
    assert len(rejected[0]['response_sha256']) == 64 and rejected[0]['reason']
    assert 'response' not in rejected[0]  # No raw model response retained.


def test_real_compaction_preserves_audio_timing_and_native_input_identity():
    raw = video_bytes(audio=True)
    compact = _compact(raw, lambda: None)
    _, original_info = prepare_video(raw)
    _, compact_info = prepare_video(compact)
    assert compact != raw
    assert compact_info['duration_seconds'] == pytest.approx(original_info['duration_seconds'], abs=.1)
    assert compact_info['audio_ranges'] == original_info['audio_ranges']
    assert compact_info['audio_sent']


def test_no_audio_cannot_be_reported_as_heard():
    raw, info = prepare_video(video_bytes())
    assert info['audio_sent'] is False and info['audio_ranges'] == []
    with pytest.raises(ObservationError, match='没有发送音轨'):
        parse_result(RESULT, info, 'reference')


def test_oversized_original_is_compacted_and_both_hashes_are_bound():
    raw = video_bytes(audio=True) + bytes(7_000_001)
    sent, info = prepare_video(raw)
    assert len(sent) < 7_000_000 and info['transcoded'] is True
    assert info['source_sha256'] == hashlib.sha256(raw).hexdigest()
    assert info['input_sha256'] == hashlib.sha256(sent).hexdigest()
    assert info['audio_sent']


def test_small_webm_is_converted_to_supported_mp4():
    import av
    import io
    from PIL import Image
    output = io.BytesIO()
    with av.open(output, mode='w', format='webm') as container:
        video = container.add_stream('libvpx', rate=4)
        video.width, video.height, video.pix_fmt = 64, 96, 'yuv420p'
        for i in range(8):
            frame = av.VideoFrame.from_image(Image.new('RGB', (64, 96), 'blue'))
            frame.pts = i
            for packet in video.encode(frame): container.mux(packet)
        for packet in video.encode(): container.mux(packet)
    sent, info = prepare_video(output.getvalue())
    assert info['transcoded'] and b'ftyp' in sent[:32]


def test_native_evidence_tampering_cannot_change_findings_or_source(native):
    response, _, _ = start(native)
    assert wait_job(native[0], response.json())['status'] == 'succeeded'
    c = native[0].get(f'/api/runs/{RUN}/image-analysis').json()['candidates'][0]
    assert confirm(native[0], c, [0, 1]).status_code == 200
    path = native[1].run_dir(RUN) / 'evidence/observations.json'
    original = json.loads(path.read_text('utf-8'))
    for key in ('finding', 'source'):
        doc = copy.deepcopy(original)
        if key == 'finding': doc['observations'][0]['findings'][0] = 'fabricated'
        else: doc['observations'][0]['audiovisual']['source_sha256'] = '0' * 64
        path.write_text(json.dumps(doc), encoding='utf-8')
        assert evidence_snapshot(native[1], RUN)['errors']
        assert creative_evidence(native[1], RUN)['reference_observations'] == []


def stream_response(*, done=True, usage=True):
    chunks = [{'model': MODEL, 'choices': [{'delta': {'content': 'answer'}, 'finish_reason': None}]},
              {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}]
    if usage: chunks.append({'choices': [], 'usage': {'prompt_tokens': 18032, 'completion_tokens': 2000}})
    return ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + ('data: [DONE]\n\n' if done else '')


@pytest.mark.parametrize('done,usage', [(True, True), (False, True), (True, False)])
def test_sse_wire_contains_video_sound_and_real_usage(monkeypatch, done, usage):
    seen = []
    raw = video_bytes(audio=True)
    def serve(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, text=stream_response(done=done, usage=usage))
    real = httpx.Client
    monkeypatch.setattr('studio.agents.bailian_omni.httpx.Client', lambda **kw: real(transport=httpx.MockTransport(serve), **kw))
    client = OmniClient(BASE, 'fixture-key')
    kwargs = dict(model=MODEL, tools=[], system='inspect', messages=[{'role': 'user', 'content': [
        {'type': 'video', 'source': {'type': 'base64', 'data': base64.b64encode(raw).decode()}}]}])
    if not done:
        with pytest.raises(ModelError) as caught: client.complete(**kwargs)
        assert caught.value.usage_status == 'partial'
    else:
        turn = client.complete(**kwargs)
        assert turn.text == 'answer' and turn.stop_reason == 'end_turn'
        assert turn.usage.input_tokens == (18032 if usage else None)
    assert len(seen) == 1 and seen[0]['stream'] is True
    assert base64.b64decode(seen[0]['messages'][1]['content'][0]['video_url']['url'].split(',', 1)[1]) == raw
    assert seen[0]['modalities'] == ['text'] and seen[0]['reasoning_effort'] == 'low'
