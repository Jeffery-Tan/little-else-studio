"""Independent image routes, real synthetic pixels and mock HTTP; no paid calls."""
import base64
import copy
import hashlib
import io
import json
import threading

import httpx
import pytest
from PIL import Image

from studio import image_understanding
from studio.agent_images import observe, saved_observations, ImagePreflightError
from studio.agents.client import JobCancelled, ModelError, SafePreflightError, ToolCall
from studio.agents.mimo_client import MiMoClient, ENDPOINT
from studio.models import PipelineError
from studio.connections import ConnectionStore, ConnectionConflict
from studio.ledger import Ledger
from studio.materials import accept_upload
from test_agent_images import result, image_analysis_context
from test_deepseek_client import response, transport
from test_image_analysis import scene
from test_natural_reference import chat, scripted
from fakes import tool_turn, final_turn
from test_server import RUN
from test_observation_image_input import noisy_png
from test_writing_tool_schema import stream_reply
from test_agent_images import tool_result_for
from studio.materials import resolve
from test_bailian_omni import native, BASE as BAILIAN_BASE, MODEL as BAILIAN_MODEL

MIMO = image_understanding.MIMO_MODEL


def configure(scene, *, key=True):
    connections = ConnectionStore(scene[1].root)
    public = connections.public()
    payload = {**public, 'expected_revision': public['revision']}
    payload['connections'].append({'id': 'mimo-image', 'name': 'Image MiMo', 'provider': 'mimo',
        **({'api_key': 'synthetic-image-key'} if key else {})})
    payload['image_analysis'] = {'connection': 'mimo-image', 'model': MIMO}
    saved = connections.save(payload)
    return connections, saved


def frozen(scene, connections):
    return image_understanding.freeze(connections, scene[5],
        expected_revision=connections.public()['revision'],
        expected_info=image_understanding.connection_info(connections))


def call(scene, route, *, cancel=lambda: None, cancel_event=None, names=('fixture.png', 'second.png'),
         emit=lambda _: None):
    return observe(scene[1], scene[5], RUN,
        [dict(name=name, purpose='reference' if index == 1 else 'product') for index, name in enumerate(names)],
        endpoint=None, emit=emit, check_cancel=cancel,
        freeze_route=route, cancel_event=cancel_event)


def add_second(scene):
    accept_upload(scene[1], RUN, 'second.png', scene[4])


def answer(count=1, *, first=1):
    assert count == 1  # MiMo observers always report one actual source.
    report = json.loads(result(count).text)['images'][0]
    report['id'] = f'image-{first}'
    return response(text=json.dumps(report), finish='stop') | {'model': MIMO}


def strict_answer(*, first=1):
    """Historical candidate capability, separate from the selected JSON route."""
    report = json.loads(answer(first=first)['choices'][0]['message']['content'])
    data = response(text='', finish='tool_calls') | {'model': MIMO}
    data['choices'][0]['message']['tool_calls'] = [{'id': f'report-{first}', 'type': 'function',
        'function': {'name': image_understanding.REPORT_FUNCTION, 'arguments': json.dumps(report)}}]
    return data


def observer_reply(data, streaming):
    if not streaming:
        return data
    if data['choices'][0]['message'].get('tool_calls'):
        return stream_reply(data)
    frames = [{'model': MIMO, 'choices': [{'index': 0, 'delta': {
        'content': data['choices'][0]['message']['content'],
        'reasoning_content': 'SYNTHETIC_PRIVATE_REASONING'}, 'finish_reason': None}]},
        {'model': MIMO, 'usage': data['usage'], 'choices': [
            {'index': 0, 'delta': {}, 'finish_reason': data['choices'][0]['finish_reason']}]}]
    return httpx.Response(200, text='\n\n'.join('data: ' + json.dumps(frame) for frame in frames)
        + '\n\ndata: [DONE]\n\n', headers={'content-type': 'text/event-stream'})


def test_default_does_not_infer_image_route_from_video_or_roles(scene, monkeypatch):
    connections, saved = configure(scene)
    saved['expected_revision'] = saved['revision']
    saved['image_analysis'] = None
    saved['video_analysis'] = {'connection': 'mimo-image', 'model': MIMO}
    saved['roles']['orchestrator'] = {'connection': 'mimo-image', 'model': MIMO}
    saved = connections.save(saved)
    monkeypatch.setattr('studio.agents.bailian_omni.connection_info', lambda: {'legacy': True})
    before = connections.path.read_bytes()
    assert image_understanding.connection_info(connections) == {'legacy': True}
    assert connections.path.read_bytes() == before
    assert saved['image_analysis'] is None


def test_image_binding_is_independent_preserved_and_revision_checked(scene):
    connections, saved = configure(scene)
    assert saved['video_analysis'] is None
    assert saved['roles'] == scene[0].get('/api/connections').json()['roles']
    payload = {k: copy.deepcopy(saved[k]) for k in ('connections', 'roles')}
    payload['expected_revision'] = saved['revision']
    updated = connections.save(payload)
    assert updated['image_analysis'] == saved['image_analysis']
    with pytest.raises(ConnectionConflict):
        connections.save(payload)
    updated = connections.save({**updated, 'expected_revision': updated['revision'], 'image_analysis': None})
    assert updated['image_analysis'] is None


def test_switching_to_mimo_does_not_reobserve_existing_bailian_sources(native):
    native[2].turn = result(1)
    items = [dict(name='fixture.png', purpose='product')]
    first = observe(native[1], native[5], RUN, items, endpoint=BAILIAN_BASE,
        emit=lambda _: None, check_cancel=lambda: None, client_factory=lambda: native[2])
    configure(native)
    reused = observe(native[1], native[5], RUN, items, endpoint=None,
        emit=lambda _: None, check_cancel=lambda: None,
        freeze_route=lambda: pytest.fail('Changing route must not rerun cached sources'))
    assert reused['reused'] == 1 and reused['observations'] == first['observations']
    assert reused['observations'][0]['actual_model'] == BAILIAN_MODEL
    assert len(native[2].calls) == 1


@pytest.mark.parametrize('binding', [False, 'mimo-image', {},
    {'connection': 'missing', 'model': MIMO}, {'connection': 'test-api', 'model': MIMO},
    {'connection': 'mimo-image', 'model': 'wrong'},
    {'connection': 'mimo-image', 'model': MIMO, 'extra': True}])
def test_invalid_image_binding_never_saves_or_falls_back(scene, binding):
    connections, saved = configure(scene)
    before = connections.path.read_bytes()
    with pytest.raises(ValueError, match='图片理解'):
        connections.save({**saved, 'expected_revision': saved['revision'], 'image_analysis': binding})
    assert connections.path.read_bytes() == before


def test_image_freeze_uses_only_own_key_and_keeps_role_and_video_routes(scene, monkeypatch):
    connections, saved = configure(scene)
    decrypted = []
    monkeypatch.setattr('studio.connections._dpapi', lambda value, **_: decrypted.append(value) or value)
    stopped = lambda: None
    event = threading.Event()
    settings, client, info = image_understanding.freeze(connections, scene[5],
        expected_revision=saved['revision'], expected_info=image_understanding.connection_info(connections),
        check_cancel=stopped, cancel_event=event)
    assert decrypted == [b'synthetic-image-key']
    assert isinstance(client, MiMoClient) and client._token == 'synthetic-image-key'
    assert client.check_cancel is stopped and client.cancel_event is event
    assert settings.connection_by_role['material'] == 'mimo-image'
    assert settings.model_for('material') == MIMO and client.max_tokens == 4096
    assert client.json_output is True and client.structured_observation is False
    assert info['revision'] == saved['revision']
    assert connections.public()['roles'] == saved['roles']
    assert connections.public()['video_analysis'] is None
    assert 'synthetic-image-key' not in json.dumps(info)


def test_missing_mimo_key_is_saved_but_freeze_stops_without_bailian_fallback(scene, monkeypatch):
    connections, saved = configure(scene, key=False)
    monkeypatch.setattr('studio.agents.bailian_omni.freeze', lambda *a: pytest.fail('No fallback'))
    info = image_understanding.connection_info(connections)
    assert info['has_key'] is False
    with pytest.raises(SafePreflightError, match='API Key'):
        frozen(scene, connections)
    assert not Ledger(scene[1], scene[5]).entries(RUN)


def test_server_missing_image_key_does_not_block_text_job_preparation(scene, monkeypatch):
    connections, saved = configure(scene, key=False)
    monkeypatch.setattr('studio.agents.bailian_omni.freeze', lambda *a: pytest.fail('No fallback'))
    worker, summary = scene[0].app.state.jobs.prepare(RUN, reference_consent=True)
    assert callable(worker) and summary['image_observation']['ready'] is False
    assert summary['image_observation']['connection']['id'] == 'mimo-image'
    assert summary['image_observation']['revision'] == saved['revision']
    assert not scene[2].calls


def test_changed_revision_or_endpoint_cannot_freeze_old_image_choice(scene):
    connections, saved = configure(scene)
    info = image_understanding.connection_info(connections)
    with pytest.raises(SafePreflightError, match='连接已变化'):
        image_understanding.freeze(connections, scene[5], expected_revision=saved['revision'],
                                   expected_info={**info, 'base_url': 'https://changed.invalid'})
    connections.save({**saved, 'expected_revision': saved['revision']})
    with pytest.raises(SafePreflightError, match='设置已变化'):
        image_understanding.freeze(connections, scene[5], expected_revision=saved['revision'], expected_info=info)


def test_mimo_multiple_images_use_separate_calls_own_key_and_original_sources(scene, monkeypatch):
    connections, saved = configure(scene)
    add_second(scene)
    route = frozen(scene, connections)
    seen = transport(monkeypatch, [answer(), answer(first=2)])
    observed = call(scene, lambda: route)['observations']
    assert len(seen) == 2 and all(str(request.url) == ENDPOINT for request in seen)
    assert all(request.headers['Authorization'] == 'Bearer synthetic-image-key' for request in seen)
    images = []
    for index, request in enumerate(seen, 1):
        payload = json.loads(request.content)
        assert payload['model'] == MIMO and payload['response_format'] == {'type': 'json_object'}
        assert 'tools' not in payload and 'tool_choice' not in payload
        from studio.image_analysis import SYSTEM
        assert payload['messages'][0]['content'] == SYSTEM.split('仅返回 JSON 对象：')[0] + image_understanding.single_report_prompt(f'image-{index}')
        parts = payload['messages'][1]['content']
        pixels = [part['image_url']['url'] for part in parts if part['type'] == 'image_url']
        assert len(pixels) == 1
        assert f'image-{index}' in payload['messages'][0]['content']
        images.extend(pixels)
    assert all(base64.b64decode(value.split(',', 1)[1]) == scene[4] for value in images)
    assert {row['purpose'] for row in observed} == {'product', 'reference'}
    assert all(row['sha256'] == hashlib.sha256(scene[4]).hexdigest() for row in observed)
    assert all(row['provider'] == 'mimo' and row['connection_id'] == 'mimo-image' for row in observed)
    assert all(row['connection_revision'] == saved['revision'] for row in observed)
    ledger = Ledger(scene[1], route[0]).entries(RUN)
    assert len(ledger) == 2 and all(row.usage_status == 'known' for row in ledger)
    assert all((row.input_tokens, row.output_tokens) == (31, 17) for row in ledger)
    assert all(row.connection_id == 'mimo-image' and row.estimated_usd is None for row in ledger)
    assert len({row.call_id for row in ledger}) == 2
    events = (scene[1].run_dir(RUN) / 'events.jsonl').read_text('utf-8')
    assert images[0] not in events and 'synthetic-image-key' not in events
    assert call(scene, lambda: pytest.fail('Cache must not create a new client'))['reused'] == 2
    assert len(seen) == 2


@pytest.mark.parametrize('provider_rejection', [True, False])
def test_mimo_failure_keeps_completed_image_and_does_not_submit_later_images(scene, monkeypatch, provider_rejection):
    from studio.models import PipelineError
    connections, _ = configure(scene)
    add_second(scene)
    accept_upload(scene[1], RUN, 'third.png', scene[4])
    route = frozen(scene, connections)
    failure = (httpx.Response(400, json={'error': 'synthetic-rejected'}) if provider_rejection else
               response(text='{"images":[]}') | {'model': MIMO})
    seen = transport(monkeypatch, [answer(), failure])
    with pytest.raises(ModelError if provider_rejection else PipelineError):
        call(scene, lambda: route, names=('fixture.png', 'second.png', 'third.png'))
    assert len(seen) == 2 and len(saved_observations(scene[1], RUN)) == 1
    ledger = Ledger(scene[1], route[0]).entries(RUN)
    assert len(ledger) == 2 and ledger[0].usage_status == 'known'
    assert ledger[1].usage_status == ('unknown' if provider_rejection else 'known')
    assert all(row.connection_id == 'mimo-image' for row in ledger)
    assert saved_observations(scene[1], RUN)[0]['name'] == 'fixture.png'
    assert 'third.png' not in json.loads(seen[-1].content)['messages'][1]['content'][0]['text']


def test_six_mimo_images_keep_unique_pixels_labels_hashes_and_one_call_per_source(scene, monkeypatch):
    connections, _ = configure(scene)
    sources = {}
    names = tuple(f'frame-{index}.png' for index in (4, 1, 6, 2, 5, 3))
    for index, name in enumerate(names):
        raw = io.BytesIO()
        Image.new('RGB', (20 + index, 15), (index * 30, 110, 100)).save(raw, format='PNG')
        sources[name] = raw.getvalue()
        accept_upload(scene[1], RUN, name, sources[name])
    route = frozen(scene, connections)
    seen = transport(monkeypatch, [answer(first=index) for index in range(1, 7)])
    progress = []
    observed = call(scene, lambda: route, names=names, emit=progress.append)
    assert observed['batches'] == 6 and len(seen) == 6
    for index, (request, name, observation) in enumerate(zip(seen, names, observed['observations']), 1):
        parts = json.loads(request.content)['messages'][1]['content']
        assert len(parts) == 2 and parts[0]['type'] == 'text' and parts[1]['type'] == 'image_url'
        assert parts[0]['text'].splitlines()[0].endswith(name)
        assert f'image-{index}' in parts[0]['text']
        metadata_line = parts[0]['text'].splitlines()[-1]
        metadata = json.loads(metadata_line[metadata_line.index('{'):])
        pixels = base64.b64decode(parts[1]['image_url']['url'].split(',', 1)[1])
        assert pixels == sources[name]
        assert metadata['input_sha256'] == observation['sha256'] == hashlib.sha256(pixels).hexdigest()
        assert observation['name'] == name
    assert len(Ledger(scene[1], route[0]).entries(RUN)) == 6
    statuses = [item['message'] for item in progress if item.get('type') == 'status']
    assert len(statuses) == 6 and all(name in message for name, message in zip(names, statuses))
    assert '图片 6/6' in statuses[-1] and '本轮已保存 5 张观察' in statuses[-1]
    assert call(scene, lambda: pytest.fail('Cache cannot make a client'), names=names)['reused'] == 6
    assert len(seen) == 6


def test_mimo_large_image_uses_same_compression_and_preserves_source(scene, monkeypatch):
    connections, _ = configure(scene)
    original = noisy_png()
    accept_upload(scene[1], RUN, 'large.png', original)
    route = frozen(scene, connections)
    seen = transport(monkeypatch, [answer(1)])
    observed = observe(scene[1], scene[5], RUN, [dict(name='large.png', purpose='product')],
        endpoint=None, emit=lambda _: None, check_cancel=lambda: None, freeze_route=lambda: route)['observations'][0]
    parts = json.loads(seen[0].content)['messages'][1]['content']
    url = next(part['image_url']['url'] for part in parts if part['type'] == 'image_url')
    sent = base64.b64decode(url.split(',', 1)[1])
    assert url.startswith('data:image/jpeg;base64,') and len(sent) < len(original)
    assert resolve(scene[1], RUN, 'large.png').read_bytes() == original
    assert observed['sha256'] == hashlib.sha256(original).hexdigest()
    assert observed['image_input']['input_sha256'] == hashlib.sha256(sent).hexdigest()
    assert any('压缩副本' in value for value in observed['limitations'])


def test_revision_change_between_batches_keeps_first_observation_and_stops_unsent_batch(scene, monkeypatch):
    from studio.models import PipelineError
    connections, saved = configure(scene)
    add_second(scene)
    route = frozen(scene, connections)
    monkeypatch.setattr('studio.agents.bailian_omni.MAX_INLINE_BYTES', len(scene[4]) * 2 - 1)
    real = httpx.AsyncClient
    seen = []
    def reply(request):
        seen.append(request)
        connections.save({**saved, 'expected_revision': saved['revision']})
        return httpx.Response(200, json=answer(1))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real(transport=httpx.MockTransport(reply), **kw))
    def current_route():
        if connections.public()['revision'] != saved['revision']:
            raise SafePreflightError('Synthetic image setting changed')
        return route
    with pytest.raises(PipelineError, match='此前已保存 1 张'):
        call(scene, current_route)
    assert len(seen) == 1 and len(saved_observations(scene[1], RUN)) == 1
    assert len(Ledger(scene[1], route[0]).entries(RUN)) == 1


def test_mimo_cancel_after_received_response_keeps_known_usage_without_observation(scene, monkeypatch):
    connections, _ = configure(scene)
    add_second(scene)
    route = frozen(scene, connections)
    seen = transport(monkeypatch, [answer()])
    def stopped():
        if any(row.usage_status == 'known' and row.output_tokens == 17
               for row in Ledger(scene[1], route[0]).entries(RUN)):
            raise JobCancelled('Synthetic stop after settlement')
    with pytest.raises(JobCancelled):
        call(scene, lambda: route, cancel=stopped)
    assert len(seen) == 1 and not saved_observations(scene[1], RUN)
    row = Ledger(scene[1], route[0]).entries(RUN)[0]
    assert row.usage_status == 'known' and (row.input_tokens, row.output_tokens) == (31, 17)


def test_mimo_cancel_after_first_saved_image_never_submits_remaining_images(scene, monkeypatch):
    connections, _ = configure(scene)
    add_second(scene)
    accept_upload(scene[1], RUN, 'third.png', scene[4])
    route = frozen(scene, connections)
    seen = transport(monkeypatch, [answer()])
    def stopped():
        if saved_observations(scene[1], RUN):
            raise JobCancelled('Synthetic stop after first saved image')
    with pytest.raises(JobCancelled):
        call(scene, lambda: route, cancel=stopped, names=('fixture.png', 'second.png', 'third.png'))
    assert len(seen) == 1 and len(saved_observations(scene[1], RUN)) == 1
    assert len(Ledger(scene[1], route[0]).entries(RUN)) == 1


def test_server_freezes_mimo_image_route_without_bailian_and_uses_same_job(scene, monkeypatch):
    connections, saved = configure(scene)
    monkeypatch.setattr('studio.agents.bailian_omni.connection_info', lambda: None)
    monkeypatch.setattr('studio.agents.bailian_omni.freeze', lambda *a: pytest.fail('No legacy call'))
    turn = result(1)
    turn.model = MIMO
    turn.text = json.dumps(json.loads(turn.text)['images'][0])
    scripted(scene, monkeypatch, [
        tool_turn('route', 'prepare_creative_context', image_analysis_context('看这张商品图')),
        tool_turn('look', 'observe_images', {'images': [dict(name='fixture.png', purpose='product')]}),
        turn, final_turn('已查看商品图。')])
    job = chat(scene, '看这张商品图')
    assert job['status'] == 'succeeded', job
    captured = job['model_config']['image_observation']
    assert captured['ready'] and captured['connection']['id'] == 'mimo-image'
    assert captured['revision'] == saved['revision']
    assert scene[2].calls[2]['model'] == MIMO
    observed = saved_observations(scene[1], RUN)
    assert len(observed) == 1 and observed[0]['connection_id'] == 'mimo-image'


def test_server_config_change_after_job_freeze_blocks_pending_image_request(scene, monkeypatch):
    connections, saved = configure(scene)
    worker, summary = scene[0].app.state.jobs.prepare(RUN, reference_consent=True)
    assert summary['image_observation']['ready']
    connections.save({**saved, 'expected_revision': saved['revision']})
    class Observer:
        def __init__(self, *args, image_observer, **kwargs):
            self.observe = image_observer
        def send(self, *args, **kwargs):
            return self.observe([dict(name='fixture.png', purpose='product')], lambda _: None, lambda: None)
    monkeypatch.setattr('studio.server.Orchestrator', Observer)
    with pytest.raises(ImagePreflightError, match='设置已变化'):
        worker('synthetic task', lambda _: None, lambda: None)
    assert not scene[2].calls and not Ledger(scene[1], scene[5]).entries(RUN)


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('failure', ['plain_prose', 'many_reports', 'duplicate_call_id', 'unknown_function',
    'wrong_id', 'missing_findings', 'missing_limitations', 'extra_field', 'empty_findings',
    'empty_limitations', 'wrong_array_type', 'wrong_item_type', 'too_many_items', 'long_item',
    'finish_mismatch', 'previous_duplicate_json_array', 'markdown', 'surrounding_prose',
    'root_array', 'duplicate_key', 'non_finite', 'blank_item', 'wrong_id_type', 'max_tokens'])
def test_json_observer_real_parser_rejects_invalid_report_without_execution_or_retry(scene, monkeypatch, streaming, failure):
    connections, _ = configure(scene)
    route = frozen(scene, connections)
    data = answer()
    message = data['choices'][0]['message']
    args = json.loads(message['content'])
    raw = None
    if failure == 'plain_prose':
        raw = 'PRIVATE_REPORT: visible synthetic pixels'
    elif failure in {'many_reports', 'duplicate_call_id', 'unknown_function'}:
        data = strict_answer()
        message = data['choices'][0]['message']
        report = message['tool_calls'][0]
        if failure == 'unknown_function':
            report['function']['name'] = 'PRIVATE_UNKNOWN_FUNCTION'
        else:
            message['tool_calls'].append(copy.deepcopy(report))
            if failure == 'many_reports':
                message['tool_calls'][1]['id'] = 'distinct-report-call'
    elif failure == 'wrong_id':
        args['id'] = 'image-2'
    elif failure.startswith('missing_'):
        args.pop(failure[len('missing_'):])
    elif failure == 'extra_field':
        args['PRIVATE_UNKNOWN_KEY'] = 'PRIVATE_UNKNOWN_VALUE'
    elif failure == 'empty_findings':
        args['findings'] = []
    elif failure == 'empty_limitations':
        args['limitations'] = []
    elif failure == 'wrong_array_type':
        args['findings'] = 'PRIVATE_REPORT'
    elif failure == 'wrong_item_type':
        args['findings'] = [{'PRIVATE_UNKNOWN_KEY': 'PRIVATE_REPORT'}]
    elif failure == 'too_many_items':
        args['findings'] = ['Synthetic'] * 21
    elif failure == 'long_item':
        args['limitations'] = ['x' * 1001]
    elif failure == 'finish_mismatch':
        data['choices'][0]['finish_reason'] = 'tool_calls'
    elif failure == 'previous_duplicate_json_array':
        args = {'images': [{'id': 'image-1', 'findings': ['PRIVATE_REPORT']},
                           {'id': 'image-1', 'findings': ['PRIVATE_REPORT']}]}
    elif failure == 'markdown':
        raw = '```json\n' + json.dumps(args) + '\n```'
    elif failure == 'surrounding_prose':
        raw = 'PRIVATE_REPORT\n' + json.dumps(args)
    elif failure == 'root_array':
        args = [args]
    elif failure == 'duplicate_key':
        raw = '{"id":"image-2","id":"image-1","findings":["PRIVATE_REPORT"],"limitations":["Synthetic"]}'
    elif failure == 'non_finite':
        args['findings'] = [float('nan')]
    elif failure == 'blank_item':
        args['limitations'] = ['   ']
    elif failure == 'wrong_id_type':
        args['id'] = 1
    elif failure == 'max_tokens':
        data['choices'][0]['finish_reason'] = 'length'
    if 'tool_calls' not in message:
        message['content'] = raw if raw is not None else json.dumps(args)
    seen = transport(monkeypatch, [observer_reply(data, streaming)])
    with pytest.raises(PipelineError, match='模型图片观察格式不符合要求'):
        call(scene, lambda: route, names=('fixture.png',))
    assert len(seen) == 1 and not saved_observations(scene[1], RUN)
    payload = json.loads(seen[0].content)
    assert 'tools' not in payload and 'tool_choice' not in payload
    assert payload['response_format'] == {'type': 'json_object'}
    entry = Ledger(scene[1], route[0]).entries(RUN)[0]
    assert entry.usage_status == 'known' and (entry.input_tokens, entry.output_tokens) == (31, 17)
    invalid = next(e for e in scene[1].read_events(RUN) if e['kind'] == 'agent_image_result_invalid')
    assert invalid['call_id'] == entry.call_id
    public = json.dumps(invalid)
    assert 'PRIVATE_' not in public and 'UNKNOWN_FUNCTION' not in public and 'UNKNOWN_KEY' not in public
    assert invalid['diagnostic']['response_kind'] == 'single_image_json_report'
    from studio.tools.registry import REGISTRY
    assert image_understanding.REPORT_FUNCTION not in REGISTRY


@pytest.mark.parametrize('scene', [12], indirect=True)
def test_mimo_paid_report_failure_cannot_be_replayed_in_original_conversation_turn(scene, monkeypatch):
    configure(scene)
    text = '只看这张商品图片，不创作。'
    invalid = result()
    invalid.text = json.dumps({'id': 'image-1', 'findings': ['Synthetic pixels']})
    invalid.model = MIMO
    scripted(scene, monkeypatch, [
        tool_turn('route', 'prepare_creative_context', image_analysis_context(text)),
        tool_turn('first', 'observe_images', {'images': [dict(name='fixture.png', purpose='product')]}),
        invalid,
        tool_turn('replay', 'observe_images', {'images': [dict(name='fixture.png', purpose='product')]}),
        final_turn('本轮观察失败'), final_turn('本轮观察失败')])
    job = chat(scene, text)
    assert job['status'] == 'blocked', job
    assert tool_result_for(scene[2], 'first')['is_error'] is True
    replay = tool_result_for(scene[2], 'replay')
    assert replay['is_error'] is True and '已尝试' in replay['content']
    assert len([e for e in Ledger(scene[1], scene[5]).entries(RUN) if e.stage == 'image_analysis']) == 1
    assert not saved_observations(scene[1], RUN)


def test_json_report_retains_original_visible_response_size_limit(scene, monkeypatch):
    connections, _ = configure(scene)
    route = frozen(scene, connections)
    data = answer()
    data['choices'][0]['message']['content'] = 'x' * 45001
    seen = transport(monkeypatch, [data])
    with pytest.raises(PipelineError, match='格式不符合要求'):
        call(scene, lambda: route, names=('fixture.png',))
    assert len(seen) == 1 and not saved_observations(scene[1], RUN)
    assert Ledger(scene[1], route[0]).entries(RUN)[0].usage_status == 'known'


def test_single_report_schema_and_validator_share_shape_and_preserve_original_content():
    report = {'id': 'image-1', 'findings': ['  Synthetic visible pixels  '], 'limitations': ['Synthetic unknowns']}
    assert image_understanding.structured_report_tool('image-1')['input_schema'] == image_understanding.single_report_schema('image-1')
    assert image_understanding.validate_single_report(report, 'image-1') is report
    assert report['findings'][0] == '  Synthetic visible pixels  '
    with pytest.raises(ValueError):
        image_understanding.validate_single_report({**report, 'findings': [' ' * 1000 + 'x']}, 'image-1')


def test_single_report_safe_diagnostic_distinguishes_unknown_finish_without_retaining_content():
    from studio.agent_images import _result_diagnostic
    turn = result(1)
    turn.stop_reason = 'PRIVATE_UNKNOWN_STOP'
    turn.text = json.dumps({'id': 'image-1', 'findings': ['PRIVATE_REPORT'], 'PRIVATE_UNKNOWN_KEY': 'PRIVATE_REPORT'})
    diagnostic = _result_diagnostic(turn, [(dict(id='image-1'), b'', 'image/png')], single_report=True)
    assert diagnostic['actual_stop_reason'] == 'unknown'
    assert diagnostic['expected_finish_matches'] is False
    assert diagnostic['report_fields']['limitations']['present'] is False
    assert 'PRIVATE_' not in json.dumps(diagnostic)


@pytest.mark.parametrize('streaming', [False, True])
def test_json_observer_first_response_accepts_exact_report_with_real_http_parser(scene, monkeypatch, streaming):
    connections, _ = configure(scene)
    route = frozen(scene, connections)
    data = answer()
    report = json.loads(data['choices'][0]['message']['content'])
    report['findings'] = ['  Synthetic visible pixels  ']
    data['choices'][0]['message']['content'] = json.dumps(report)
    seen = transport(monkeypatch, [observer_reply(data, streaming)])
    observed = call(scene, lambda: route, names=('fixture.png',))['observations']
    assert len(seen) == 1 and len(observed) == 1
    assert observed[0]['findings'] == report['findings']
    assert Ledger(scene[1], route[0]).entries(RUN)[0].usage_status == 'known'
    payload = json.loads(seen[0].content)
    assert payload['response_format'] == {'type': 'json_object'}
    assert 'tools' not in payload and 'tool_choice' not in payload


def test_json_observer_retains_character_limit_instead_of_changing_to_utf8_byte_limit(scene, monkeypatch):
    connections, _ = configure(scene)
    route = frozen(scene, connections)
    data = answer()
    report = {'id': 'image-1', 'findings': ['观' * 1000] * 20, 'limitations': ['限' * 1000] * 20}
    text = json.dumps(report, ensure_ascii=False)
    assert len(text) < 45000 < len(text.encode('utf-8'))
    data['choices'][0]['message']['content'] = text
    seen = transport(monkeypatch, [data])
    observed = call(scene, lambda: route, names=('fixture.png',))['observations']
    assert len(seen) == 1 and observed[0]['findings'] == report['findings']
