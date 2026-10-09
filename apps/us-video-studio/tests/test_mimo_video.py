"""Real local audiovisual codecs and isolated routing, synthetic model calls only."""
import base64
import copy
import hashlib
import io
import json
import math
import struct
from fractions import Fraction

import av
import pytest
from PIL import Image

from studio.agents.client import ModelError, SafePreflightError, Turn, Usage
from studio.agents.mimo_client import MiMoClient, ENDPOINT
from studio.agents.deepseek_client import DeepSeekClient
from studio.audiovisual_observation import _compact, prepare_video
from studio.connections import ConnectionStore
from studio.creative_evidence import creative_evidence
from studio.ledger import Ledger
from studio.materials import accept_upload
from studio.video_understanding import connection_info, freeze, MIMO_MODEL, MIMO_MAX_INPUT_BYTES
from test_bailian_omni import RESULT
from test_deepseek_client import response, transport
from test_image_analysis import scene, wait_job, confirm
from test_natural_reference import chat, scripted, inspect
from test_product_video import product_tool, downloader as product_downloader
from test_reference_video import URL
from test_server import RUN
from test_video_observation import video_bytes

BASE = 'https://api.xiaomimimo.com/v1'


def configure(scene, *, key=True):
    connections = ConnectionStore(scene[1].root)
    before = connections.public()
    payload = {**before, 'expected_revision': before['revision']}
    payload['connections'].append({'id': 'mimo-video', 'name': 'MiMo video', 'provider': 'mimo',
                                  **({'api_key': 'synthetic-only'} if key else {})})
    payload['video_analysis'] = {'connection': 'mimo-video', 'model': MIMO_MODEL}
    saved = connections.save(payload)
    assert saved['roles'] == before['roles']
    return connections, saved


def observation(*, product=False):
    value = copy.deepcopy(RESULT)
    if product:
        value['reference_methods'] = []
    return Turn(text=json.dumps(value), stop_reason='end_turn', model=MIMO_MODEL, usage=Usage(100, 20))


def start(scene, *, raw=None, result=None, **over):
    connections, saved = configure(scene)
    raw = raw if raw is not None else video_bytes(audio=True)
    accept_upload(scene[1], RUN, 'mimo-native.mp4', raw)
    scene[2].turn = result or observation()
    payload = {**scene[3], 'expected_revision': saved['revision'], 'name': 'mimo-native.mp4',
               'expected_sha256': hashlib.sha256(raw).hexdigest(), 'connection_id': 'mimo-video',
               'model': MIMO_MODEL, 'expected_endpoint': BASE, 'purpose': 'reference', **over}
    return scene[0].post(f'/api/runs/{RUN}/video-analysis', json=payload), payload, raw


def test_native_analysis_sends_original_video_and_audio_then_adopts_selected_timeline(scene):
    http, store, model, _, _, settings = scene
    result, payload, raw = start(scene)
    assert result.status_code == 200, result.text
    assert wait_job(http, result.json())['status'] == 'succeeded'
    c = http.get(f'/api/runs/{RUN}/image-analysis').json()['candidates'][0]
    block = model.calls[0]['messages'][0]['content'][1]
    assert block['type'] == 'video' and base64.b64decode(block['source']['data']) == raw
    assert block['fps'] == 2 and block['media_resolution'] == 'default'
    assert c['provider'] == 'mimo' and c['actual_model'] == MIMO_MODEL
    assert c['audiovisual']['audio_sent'] and c['audiovisual']['source_sha256'] == payload['expected_sha256']
    assert len(c['timeline']) == 2 and not creative_evidence(store, RUN)['reference_observations']
    assert confirm(http, c, [0, 1]).status_code == 200
    assert creative_evidence(store, RUN)['reference_observations'][0]['level'] == 'audiovisual_review'
    row = Ledger(store, settings).entries(RUN)[0]
    assert row.usage_status == 'known' and row.estimated_usd is None
    assert http.post(f'/api/runs/{RUN}/video-analysis', json=payload).json()['job_id'] == result.json()['job_id']
    assert len(model.calls) == 1
    events = (store.run_dir(RUN) / 'events.jsonl').read_text('utf-8')
    assert block['source']['data'] not in events and 'synthetic-only' not in events


def test_mimo_without_native_selection_preserves_frame_only_transmission(scene):
    from test_video_observation import RESULT as FRAME_RESULT
    connections, saved = configure(scene)
    saved = connections.save({**saved, 'expected_revision': saved['revision'], 'video_analysis': None})
    raw = video_bytes(audio=True)
    accept_upload(scene[1], RUN, 'frame-only.mp4', raw)
    scene[2].turn = Turn(text=json.dumps(FRAME_RESULT), stop_reason='end_turn',
                         model=MIMO_MODEL, usage=Usage(100, 20))
    request = {**scene[3], 'name': 'frame-only.mp4', 'expected_sha256': hashlib.sha256(raw).hexdigest(),
               'expected_revision': saved['revision'], 'connection_id': 'mimo-video',
               'model': MIMO_MODEL, 'expected_endpoint': BASE, 'purpose': 'reference'}
    response = scene[0].post(f'/api/runs/{RUN}/video-analysis', json=request)
    assert response.status_code == 200, response.text
    assert wait_job(scene[0], response.json())['status'] == 'succeeded'
    blocks = scene[2].calls[0]['messages'][0]['content']
    assert any(block['type'] == 'image' for block in blocks)
    assert all(block['type'] in ('image', 'text') for block in blocks)
    assert base64.b64encode(raw).decode() not in json.dumps(blocks)
    candidate = scene[0].get(f'/api/runs/{RUN}/image-analysis').json()['candidates'][0]
    assert 'audiovisual' not in candidate and candidate['sampling']['audio_observed'] is False


@pytest.mark.parametrize('thinking', [True, False])
def test_mimo_http_wire_preserves_video_mime_and_sampling(monkeypatch, thinking):
    data = response('Synthetic observation')
    data['model'] = MIMO_MODEL
    seen = transport(monkeypatch, [data])
    raw = video_bytes(audio=True)
    encoded = base64.b64encode(raw).decode()
    MiMoClient('synthetic-only', thinking=thinking).complete(model=MIMO_MODEL, system='Synthetic', tools=[],
        messages=[{'role': 'user', 'content': [{'type': 'video', 'source': {'type': 'base64',
            'media_type': 'video/mp4', 'data': encoded}, 'fps': 2, 'media_resolution': 'default'},
            {'type': 'text', 'text': 'Observe the actual audio and video'}]}])
    part = json.loads(seen[0].content)['messages'][1]['content'][0]
    assert part == {'type': 'video_url', 'video_url': {'url': 'data:video/mp4;base64,' + encoded},
                    'fps': 2, 'media_resolution': 'default'}
    assert str(seen[0].url) == ENDPOINT


@pytest.mark.parametrize('change', [{'fps': True}, {'fps': 11}, {'media_resolution': 'unbounded'},
                                  {'source': {'type': 'url', 'data': 'https://private.invalid'}},
                                  {'source': {'type': 'base64', 'data': 'not base64'}}])
def test_invalid_video_shape_never_sends_http(monkeypatch, change):
    seen = transport(monkeypatch, [])
    block = {'type': 'video', 'source': {'type': 'base64', 'data': base64.b64encode(b'fixture').decode()}, **change}
    with pytest.raises(ModelError) as caught:
        MiMoClient('synthetic-only').complete(model=MIMO_MODEL, system='', tools=[],
            messages=[{'role': 'user', 'content': [block]}])
    assert not caught.value.provider_invoked and seen == []


def test_other_adapters_still_reject_video(monkeypatch):
    seen = transport(monkeypatch, [])
    with pytest.raises(ModelError):
        DeepSeekClient('synthetic-only').complete(model='deepseek-flash', system='', tools=[],
            messages=[{'role': 'user', 'content': [{'type': 'video', 'source': {'type': 'base64', 'data': 'Zg=='}}]}])
    assert seen == []


def test_missing_mimo_key_does_not_fall_back_or_decrypt_other_keys(scene, monkeypatch):
    connections, saved = configure(scene, key=False)
    monkeypatch.setattr('studio.agents.bailian_omni.freeze', lambda *a, **k: pytest.fail('No provider fallback'))
    info = connection_info(connections)
    assert info['id'] == 'mimo-video' and not info['has_key']
    raw = video_bytes(audio=True)
    accept_upload(scene[1], RUN, 'no-key.mp4', raw)
    request = {**scene[3], 'name': 'no-key.mp4', 'expected_sha256': hashlib.sha256(raw).hexdigest(),
               'expected_revision': saved['revision'], 'connection_id': info['id'], 'model': MIMO_MODEL,
               'expected_endpoint': BASE}
    result = scene[0].post(f'/api/runs/{RUN}/video-analysis', json=request)
    assert result.status_code == 409 and scene[2].calls == []
    assert Ledger(scene[1], scene[5]).entries(RUN) == []


def test_frozen_video_route_keeps_its_key_after_settings_change(scene):
    connections, saved = configure(scene)
    frozen = freeze(connections, scene[5], expected_revision=saved['revision'], expected_endpoint=BASE)
    updated = {**saved, 'expected_revision': saved['revision'], 'video_analysis': None}
    connections.save(updated)
    assert isinstance(frozen[1], MiMoClient) and frozen[1]._token == 'synthetic-only'
    assert frozen[2]['max_input_bytes'] == MIMO_MAX_INPUT_BYTES
    with pytest.raises(SafePreflightError, match='变化'):
        freeze(connections, scene[5], expected_revision=saved['revision'], expected_endpoint=BASE)


def test_native_reference_and_product_conversation_share_selected_mimo_route(scene, monkeypatch):
    configure(scene)
    accept_upload(scene[1], RUN, 'local.mp4', video_bytes(audio=True))
    scripted(scene, monkeypatch, [inspect('analyze', quote='分析这个参考视频'), observation()])
    job = chat(scene, '分析这个参考视频 local.mp4 的画面和声音')
    assert job['status'] == 'succeeded', job
    c = scene[0].get(f'/api/runs/{RUN}/image-analysis').json()['candidates'][0]
    assert c['provider'] == 'mimo' and c['audiovisual']['audio_sent']
    calls = len(scene[2].calls)
    scripted(scene, monkeypatch, [inspect('analyze', quote='再分析这个参考视频')])
    assert chat(scene, '再分析这个参考视频 local.mp4', request_id='natural-reuse')['status'] == 'succeeded'
    assert len(scene[2].calls) == calls + 1  # Only the producer, no repeated video call.
    product_downloader(monkeypatch, video_bytes(audio=True))
    scripted(scene, monkeypatch, [product_tool(), observation(product=True)])
    job = chat(scene, '用这条商品视频补全商品信息 ' + URL, request_id='natural-product')
    assert job['status'] == 'succeeded', job
    rows = scene[0].get(f'/api/runs/{RUN}/image-analysis').json()['candidates']
    product = next(c for c in rows if c['purpose'] == 'product')
    assert product['provider'] == 'mimo' and product['audiovisual']['audio_sent']
    assert product['status'] == 'pending' and not creative_evidence(scene[1], RUN)['product_observations']


def test_missing_audio_response_rejected_with_usage_and_no_retry(scene):
    result, _, _ = start(scene, raw=video_bytes())
    assert result.status_code == 200
    assert wait_job(scene[0], result.json())['status'] == 'failed'
    assert len(scene[2].calls) == 1
    assert Ledger(scene[1], scene[5]).entries(RUN)[0].usage_status == 'known'
    assert not scene[0].get(f'/api/runs/{RUN}/image-analysis').json()['candidates']


def test_explicit_reference_endpoint_and_cached_reuse_without_key(scene):
    connections, saved = configure(scene)
    raw = video_bytes(audio=True)
    accept_upload(scene[1], RUN, 'local.mp4', raw)
    scene[2].turn = observation()
    request = {'request_id': 'explicit-native', 'reference_id': 'native-reference', 'intent': 'analyze',
               'consent': True, 'expected_revision': saved['revision'], 'expected_endpoint': BASE,
               'name': 'local.mp4', 'expected_sha256': hashlib.sha256(raw).hexdigest()}
    job = scene[0].post(f'/api/runs/{RUN}/reference-videos', json=request)
    assert job.status_code == 200, job.text
    assert wait_job(scene[0], job.json())['status'] == 'succeeded'
    before = len(scene[2].calls)
    updated = {**connections.public(), 'expected_revision': saved['revision']}
    next(c for c in updated['connections'] if c['id'] == 'mimo-video')['clear_key'] = True
    cleared = connections.save(updated)
    request.update(request_id='explicit-reuse', expected_revision=cleared['revision'])
    reused = scene[0].post(f'/api/runs/{RUN}/reference-videos', json=request)
    assert reused.status_code == 200, reused.text
    assert wait_job(scene[0], reused.json())['status'] == 'succeeded'
    assert len(scene[2].calls) == before
    assert scene[0].get(f'/api/runs/{RUN}/reference-videos').json()['tasks'][0]['candidate']['provider'] == 'mimo'


@pytest.mark.parametrize('video', [{'connection': 'test-api', 'model': MIMO_MODEL},
                                 {'connection': ['mimo-video'], 'model': MIMO_MODEL},
                                 {'connection': 'mimo-video', 'model': 'unverified-model'},
                                 {'connection': 'mimo-video', 'model': MIMO_MODEL, 'fps': 10}])
def test_invalid_video_selection_preserves_configuration(scene, video):
    connections, saved = configure(scene)
    updated = {**saved, 'expected_revision': saved['revision'], 'video_analysis': video}
    with pytest.raises(ValueError):
        connections.save(updated)
    assert connections.public() == {k: v for k, v in saved.items() if k != 'warnings'}


def test_old_settings_client_does_not_clear_selected_video_route(scene):
    connections, saved = configure(scene)
    update = {**saved, 'expected_revision': saved['revision']}
    update.pop('video_analysis')
    assert connections.save(update)['video_analysis'] == saved['video_analysis']


def test_real_tone_packets_survive_visual_compaction_and_time_checks():
    output = io.BytesIO()
    with av.open(output, mode='w', format='mp4') as container:
        video = container.add_stream('mpeg4', rate=4)
        video.width, video.height, video.pix_fmt = 64, 96, 'yuv420p'
        sound = container.add_stream('aac', rate=8000)
        for index in range(16):
            frame = av.VideoFrame.from_image(Image.new('RGB', (64, 96), 'red'))
            frame.pts = index
            for packet in video.encode(frame): container.mux(packet)
        for packet in video.encode(): container.mux(packet)
        frame = av.AudioFrame(format='fltp', layout='mono', samples=8000)
        frame.sample_rate, frame.pts, frame.time_base = 8000, 0, Fraction(1, 8000)
        for plane in frame.planes:
            samples = [math.sin(2 * math.pi * 440 * i / 8000) * .3 for i in range(plane.buffer_size // 4)]
            plane.update(struct.pack('<' + 'f' * len(samples), *samples))
        for packet in sound.encode(frame): container.mux(packet)
        for packet in sound.encode(): container.mux(packet)
    raw = output.getvalue()
    compact = _compact(raw, lambda: None, max_bytes=MIMO_MAX_INPUT_BYTES)
    def packets(data):
        with av.open(io.BytesIO(data)) as container:
            return [hashlib.sha256(bytes(p)).hexdigest() for p in container.demux(audio=0) if p.size]
    assert packets(raw) == packets(compact) and len(packets(raw)) > 1
    _, before = prepare_video(raw, max_bytes=MIMO_MAX_INPUT_BYTES)
    _, after = prepare_video(compact, max_bytes=MIMO_MAX_INPUT_BYTES)
    assert after['audio_ranges'] == before['audio_ranges'] and after['audio_sent']


def test_mimo_limit_keeps_large_valid_source_while_bailian_compacts():
    raw = video_bytes(audio=True) + bytes(7_000_001)
    sent, info = prepare_video(raw, max_bytes=MIMO_MAX_INPUT_BYTES)
    assert sent is raw and not info['transcoded']
    sent, info = prepare_video(raw)
    assert sent is not raw and info['transcoded']


def test_small_mov_uses_actual_quicktime_mime():
    output = io.BytesIO()
    with av.open(output, mode='w', format='mov') as container:
        video = container.add_stream('mpeg4', rate=4)
        video.width, video.height, video.pix_fmt = 64, 96, 'yuv420p'
        for i in range(8):
            frame = av.VideoFrame.from_image(Image.new('RGB', (64, 96), 'blue'))
            frame.pts = i
            for packet in video.encode(frame): container.mux(packet)
        for packet in video.encode(): container.mux(packet)
    raw = output.getvalue()
    sent, packet = prepare_video(raw, max_bytes=MIMO_MAX_INPUT_BYTES)
    assert sent is raw and packet['input_media_type'] == 'video/quicktime'
