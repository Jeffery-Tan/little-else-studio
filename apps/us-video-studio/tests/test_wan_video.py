"""Offline transport contract: no credentials, network, uploads or generation."""
from dataclasses import replace
import hashlib
import io
import json

import httpx
from PIL import Image
import pytest

from studio.agents.wan_video import (
    MODEL, WanCredentials, WanVideoClient, WanVideoError, WanVideoRequest,
    build_payload, estimate_cost, existing_credentials_available, existing_endpoint, validate_video_url,
)

ENDPOINT = 'https://workspace-test.cn-beijing.maas.aliyuncs.com/api/v1'
VIDEO_URL = 'https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/test/video.mp4?signature=test'


def picture(mode='RGB', size=(240, 320), format='PNG'):
    output = io.BytesIO()
    Image.new(mode, size).save(output, format=format)
    return output.getvalue()


def request(**changes):
    data = picture()
    return replace(WanVideoRequest('The camera moves closer.', data, hashlib.sha256(data).hexdigest()), **changes)


def client(handler, loader=None):
    return WanVideoClient(loader or (lambda: WanCredentials('offline-test-key', 'workspace-test')),
                          client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_construction_and_price_estimate_do_not_load_credentials():
    calls = []
    WanVideoClient(lambda: calls.append('secret'))
    result = estimate_cost(5, '720P')
    assert result['amount'] == 3
    assert result['currency'] == 'CNY'
    assert result['kind'] == 'list_price_estimate'
    assert calls == []


def test_presence_check_does_not_open_key(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert not existing_credentials_available()
    directory = tmp_path / 'E-commerce' / 'credentials'
    directory.mkdir(parents=True)
    (directory / 'dashscope.dpapi').write_text('intentionally invalid encrypted data')
    (directory / 'dashscope.json').write_text('invalid metadata')
    assert existing_credentials_available()


def test_endpoint_reads_only_metadata_and_does_not_require_key(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    directory = tmp_path / 'E-commerce' / 'credentials'
    directory.mkdir(parents=True)
    (directory / 'dashscope.json').write_text(json.dumps({'workspace_id': 'workspace-test', 'region': 'cn-beijing'}))
    assert existing_endpoint() == ENDPOINT
    assert not existing_credentials_available()


@pytest.mark.parametrize('metadata', [{}, [], {'workspace_id': '../bad', 'region': 'cn-beijing'},
                                     {'workspace_id': 'workspace-test', 'region': 'us-east-1'}])
def test_endpoint_rejects_missing_or_other_region_metadata(tmp_path, monkeypatch, metadata):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    directory = tmp_path / 'E-commerce' / 'credentials'
    directory.mkdir(parents=True)
    (directory / 'dashscope.json').write_text(json.dumps(metadata))
    with pytest.raises(WanVideoError) as caught:
        existing_endpoint()
    assert not caught.value.submission_unknown


def test_expected_endpoint_prevents_submit_to_changed_workspace():
    with pytest.raises(WanVideoError) as caught:
        client(lambda _: pytest.fail('no generation must be sent')).submit(request(),
            expected_endpoint='https://another.cn-beijing.maas.aliyuncs.com/api/v1')
    assert caught.value.code == 'connection_changed'
    assert not caught.value.submission_unknown


def test_expected_endpoint_matches_and_submits_once():
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={'output': {'task_id': 'task-1', 'task_status': 'PENDING'}})
    assert client(handler).submit(request(), expected_endpoint=ENDPOINT).task_id == 'task-1'
    assert len(calls) == 1


def test_payload_exact_bytes_and_explicit_parameters():
    value = request()
    payload = build_payload(value)
    assert payload['model'] == MODEL
    assert payload['input']['prompt'] == value.prompt
    assert len(payload['input']['media']) == 1
    media = payload['input']['media'][0]
    assert media['type'] == 'first_frame'
    assert media['url'].startswith('data:image/png;base64,')
    assert payload['parameters'] == {'duration': 5, 'resolution': '720P', 'ratio': 'adaptive', 'audio': True, 'prompt_extend': False}


@pytest.mark.parametrize('changes', [
    {'duration': True}, {'duration': 1}, {'duration': 31}, {'duration': -1},
    {'duration': 2.5}, {'resolution': '4K'}, {'ratio': '10:1'},
    {'audio': 'true'}, {'prompt_extend': 1}, {'prompt': ''}, {'prompt': ' '},
    {'prompt': 'x' * 20001}, {'image_sha256': '0' * 64},
    {'image_bytes': b'not a picture'},
])
def test_invalid_input_rejected_before_credentials_and_network(changes):
    def fail(*_):
        pytest.fail('Invalid input must not load credentials or reach network')
    with pytest.raises(WanVideoError):
        client(fail, fail).submit(request(**changes))


@pytest.mark.parametrize('mode,size', [('RGBA', (240, 320)), ('RGB', (239, 320)), ('RGB', (240, 2000))])
def test_invalid_image_geometry_or_alpha(mode, size):
    data = picture(mode, size)
    with pytest.raises(WanVideoError, match='参考图'):
        build_payload(request(image_bytes=data, image_sha256=hashlib.sha256(data).hexdigest()))


def test_corrupt_image_not_accepted_from_headers():
    data = picture()[:80]
    with pytest.raises(WanVideoError):
        build_payload(request(image_bytes=data, image_sha256=hashlib.sha256(data).hexdigest()))


def test_submit_one_post_returns_fixed_recovery_endpoint():
    calls = []
    def handler(req):
        calls.append(req)
        assert req.method == 'POST'
        assert str(req.url) == ENDPOINT + '/services/aigc/video-generation/video-synthesis'
        assert req.headers['x-dashscope-async'] == 'enable'
        assert req.headers['authorization'] == 'Bearer offline-test-key'
        assert json.loads(req.content)['model'] == MODEL
        return httpx.Response(200, json={'output': {'task_id': 'task-1', 'task_status': 'PENDING'}, 'request_id': 'req-1'})
    result = client(handler).submit(request())
    assert len(calls) == 1
    assert result.task_id == 'task-1'
    assert result.endpoint == ENDPOINT
    assert result.status == 'PENDING'
    assert result.usage is None and result.cost_usd is None


@pytest.mark.parametrize('error', [httpx.ReadTimeout('secret internal detail'), httpx.ConnectError('key=secret')])
def test_submit_network_failure_is_unknown_never_retries(error):
    calls = []
    def handler(req):
        calls.append(req)
        raise error
    with pytest.raises(WanVideoError) as caught:
        client(handler).submit(request())
    assert caught.value.submission_unknown
    assert 'secret' not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize('status,unknown', [(400, False), (401, False), (429, False), (408, True), (500, True), (302, True)])
def test_submit_http_error_classifies_unknown_and_never_follows_redirect(status, unknown):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(status, headers={'location': 'https://untrusted.example/'}, text='secret-body')
    with pytest.raises(WanVideoError) as caught:
        client(handler).submit(request())
    assert caught.value.submission_unknown is unknown
    assert caught.value.http_status == status
    assert 'secret' not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize('body', [b'bad json', b'{}', b'[]', b'{"output":{"task_id":"task-1","task_status":"invalid"}}'])
def test_submit_malformed_response_is_unknown(body):
    with pytest.raises(WanVideoError) as caught:
        client(lambda _: httpx.Response(200, content=body)).submit(request())
    assert caught.value.submission_unknown


def test_poll_is_get_same_task_no_submit_and_usage_sanitized():
    calls = []
    def handler(req):
        calls.append(req)
        assert req.method == 'GET'
        assert str(req.url) == ENDPOINT + '/tasks/task-1'
        return httpx.Response(200, json={
            'output': {'task_id': 'task-1', 'task_status': 'SUCCEEDED', 'video_url': VIDEO_URL},
            'usage': {'duration': 5.2, 'output_video_duration': 5.2, 'input_video_duration': 0, 'video_count': 1,
                      'fps': 30, 'SR': 720, 'ratio': '9:16', 'secret': 'not copied', 'billed_usd': 0},
        })
    result = client(handler).poll('task-1', endpoint=ENDPOINT)
    assert len(calls) == 1
    assert result.video_url == VIDEO_URL
    assert result.usage['output_video_duration'] == 5.2
    assert result.usage['input_video_duration'] == 0
    assert 'secret' not in result.usage and 'billed_usd' not in result.usage
    assert result.cost_usd is None


@pytest.mark.parametrize('status', ['FAILED', 'CANCELED', 'UNKNOWN'])
def test_terminal_states_preserved_without_fabricated_usage(status):
    result = client(lambda _: httpx.Response(200, json={'output': {'task_id': 'task-1', 'task_status': status}})).poll('task-1', endpoint=ENDPOINT)
    assert result.status == status
    assert result.usage is None


def test_changed_workspace_prevents_retargeting_task():
    with pytest.raises(WanVideoError) as caught:
        client(lambda _: pytest.fail('must not send'), lambda: WanCredentials('test', 'different')).poll('task-1', endpoint=ENDPOINT)
    assert caught.value.code == 'connection_changed'
    assert not caught.value.submission_unknown


@pytest.mark.parametrize('task,endpoint', [('a/../b', ENDPOINT), ('ok', 'https://evil.example/api/v1')])
def test_invalid_task_rejected_before_credentials(task, endpoint):
    with pytest.raises(WanVideoError):
        WanVideoClient(lambda: pytest.fail('must not load credentials')).poll(task, endpoint=endpoint)


def test_poll_mismatched_task_response_rejected():
    with pytest.raises(WanVideoError) as caught:
        client(lambda _: httpx.Response(200, json={'output': {'task_id': 'different', 'task_status': 'SUCCEEDED'}})).poll('task-1', endpoint=ENDPOINT)
    assert not caught.value.submission_unknown


@pytest.mark.parametrize('url', [
    'http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.mp4',
    'https://127.0.0.1/a.mp4', 'https://evil.example/a.mp4',
    'https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com.evil.example/a.mp4',
    'https://user:password@dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.mp4',
    'https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com:8443/a.mp4',
    'https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.html',
])
def test_download_url_restrictions_before_io(url):
    with pytest.raises(WanVideoError):
        client(lambda _: pytest.fail('must not download')).download(url)


def mp4_header():
    # This is intentionally a header fixture, NOT an actual decodable video.
    return (24).to_bytes(4, 'big') + b'ftypisom' + b'\0' * 12


def test_download_never_loads_or_sends_api_credentials():
    def handler(req):
        assert 'authorization' not in req.headers
        assert req.method == 'GET'
        return httpx.Response(200, content=mp4_header())
    data = client(handler, lambda: pytest.fail('must not load key')).download(VIDEO_URL)
    assert data == mp4_header()
    assert validate_video_url(VIDEO_URL) == VIDEO_URL


@pytest.mark.parametrize('response', [
    httpx.Response(200, content=b'<html>not video</html>'),
    httpx.Response(200, headers={'content-length': '500'}, content=mp4_header()),
    httpx.Response(302, headers={'location': 'https://127.0.0.1/a.mp4'}),
])
def test_invalid_download_and_redirect_do_not_publish(response):
    calls = []
    def handler(req):
        calls.append(req)
        return response
    with pytest.raises(WanVideoError):
        client(handler).download(VIDEO_URL, max_bytes=100)
    assert len(calls) == 1


def test_stream_limit_applies_without_content_length():
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield mp4_header()
            yield b'X' * 100
    with pytest.raises(WanVideoError) as caught:
        client(lambda _: httpx.Response(200, stream=Stream())).download(VIDEO_URL, max_bytes=100)
    assert caught.value.code == 'video_too_large'
