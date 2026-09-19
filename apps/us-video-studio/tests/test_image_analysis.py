"""Image bytes cross a fake boundary; all credentials, images and data are isolated."""
import base64
import hashlib
import io
import json
import threading
import time

import httpx
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from studio.agents.client import Turn, Usage, ModelError, AnthropicCompatClient
from studio.agents.openai_compat import OpenAICompatClient
from studio.connections import ConnectionStore
from studio.evidence import evidence_fingerprint
from studio.ledger import Ledger
from studio.materials import accept_upload
from studio.pipeline import Pipeline
from studio.server import build_app
from studio.store import Store
from test_connections import api_payload
from test_server import RUN, settings_for

RESULT = {'findings': ['Visible handle', 'Visible zipper'], 'limitations': ['Back is not visible']}


class VisionFixture:
    def __init__(self):
        self.calls = []
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.turn = Turn(text=json.dumps(RESULT), stop_reason='end_turn', model='fixture-vision', usage=Usage(110, 25))
        self.error = None

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        assert self.release.wait(5)
        if self.error:
            raise self.error
        return self.turn


@pytest.fixture
def scene(tmp_path, monkeypatch):
    # Test-only key protection, never an application fallback.
    monkeypatch.setattr('studio.connections._dpapi', lambda value, **_: value)
    store = Store(tmp_path)
    store.create_run(RUN, 'Isolated image flow', 'Synthetic input only')
    image = io.BytesIO()
    Image.new('RGB', (20, 15), (45, 110, 100)).save(image, format='PNG')
    raw = image.getvalue()
    accept_upload(store, RUN, 'fixture.png', raw)
    connections = ConnectionStore(tmp_path)
    config = api_payload(connections)
    # Leave the API unbound to normal roles; analysis must still use its key.
    config['roles'] = connections.public()['roles']
    connections.save(config)
    model = VisionFixture()
    settings = settings_for(tmp_path)
    app = build_app(settings, client_factory=lambda: model)
    payload = dict(request_id='vision-1', name='fixture.png', expected_sha256=hashlib.sha256(raw).hexdigest(),
                   expected_revision=1, connection_id='test-api', model='custom-model', purpose='product', consent=True)
    with TestClient(app) as http:
        yield http, store, model, payload, raw, settings
    model.release.set()


def submit(scene, **over):
    http, _, _, payload, _, _ = scene
    return http.post(f'/api/runs/{RUN}/image-analysis', json={**payload, **over})


def wait_job(http, job):
    for _ in range(300):
        current = http.get(f"/api/runs/{RUN}/jobs/{job['job_id']}").json()
        if current['status'] not in {'running', 'queued', 'cancelling'}:
            return current
        time.sleep(.01)
    pytest.fail('isolated job failed to finish')


def candidate(http):
    return http.get(f'/api/runs/{RUN}/image-analysis').json()['candidates'][0]


def confirm(http, c, selection=None, revision=None):
    return http.post(f"/api/runs/{RUN}/image-analysis/{c['analysis_id']}/confirm", json={
        'expected_candidate_sha256': c['candidate_sha256'], 'expected_manifest_sha256': revision,
        'selected_findings': [0] if selection is None else selection})


def test_actual_image_input_candidate_selection_and_provenance(scene):
    http, store, model, payload, raw, settings = scene
    pipeline = Pipeline(store)
    attempt = pipeline.start_attempt(RUN, 'intake')
    pipeline.submit(RUN, 'intake', attempt.token, content='original', summary='Original')
    before = evidence_fingerprint(store, RUN)
    response = submit(scene)
    assert response.status_code == 200, response.text
    assert wait_job(http, response.json())['status'] == 'succeeded'
    assert evidence_fingerprint(store, RUN) == before
    wire = model.calls[0]['messages'][0]['content'][0]
    assert wire['source']['media_type'] == 'image/png'
    assert base64.b64decode(wire['source']['data']) == raw
    assert model.calls[0]['tools'] == []
    c = candidate(http)
    assert c['status'] == 'pending'
    assert c['estimated_usd'] == pytest.approx(.00016)
    row = Ledger(store, settings).entries(RUN)[0]
    assert row.call_status == 'settled' and row.connection_id == 'test-api'
    assert c['call_id'] == row.call_id
    assert not pipeline.stage_state(RUN, 'intake').stale
    saved = confirm(http, c)
    assert saved.status_code == 200, saved.text
    observation = saved.json()['materials'][0]['observations'][0]
    assert observation['findings'] == ['Visible handle']
    assert observation['source_kind'] == 'model_observation_confirmed'
    assert observation['analysis_id'] == c['analysis_id']
    assert observation['limitations'][-1].startswith('仅观察')
    assert pipeline.stage_state(RUN, 'intake').stale
    assert pipeline.stage_state(RUN, 'direction').approval is None
    assert candidate(http)['selected_findings'] == [0]
    # Lost confirmation response cannot append twice, nor adopt another selection.
    assert confirm(http, c).status_code == 200
    assert confirm(http, c, [1]).status_code == 409
    assert len(model.calls) == 1
    assert len(saved.json()['materials'][0]['observations']) == 1
    journal = (store.run_dir(RUN) / 'events.jsonl').read_text('utf-8')
    assert wire['source']['data'] not in journal
    assert 'synthetic-credential-only' not in journal
    assert ConnectionStore(store.root).public()['roles']['material']['connection'] == 'codex'


def test_duplicate_submission_busy_and_reconnect_do_not_repeat_calls(scene):
    http, store, model, payload, _, _ = scene
    model.release.clear()
    response = submit(scene)
    assert response.status_code == 200
    assert model.entered.wait(2)
    job = response.json()
    assert submit(scene).json()['job_id'] == job['job_id']
    assert submit(scene, purpose='reference').status_code == 409
    assert submit(scene, request_id='another').status_code == 409
    assert http.post(f'/api/runs/{RUN}/jobs', json={'request_id': 'chat', 'text': 'go'}).status_code == 409
    assert http.get(f'/api/runs/{RUN}/jobs/latest').json()['job_kind'] == 'image_analysis'
    model.release.set()
    assert wait_job(http, job)['status'] == 'succeeded'
    assert submit(scene).json()['job_id'] == job['job_id']
    assert len(model.calls) == 1


@pytest.mark.parametrize('over', [dict(consent=False), dict(expected_sha256='0'*64),
    dict(expected_revision=0), dict(connection_id='codex'), dict(name='../fixture.png'),
    dict(model='text with whitespace')])
def test_preflight_rejects_without_model_or_ledger(scene, over):
    http, store, model, _, _, settings = scene
    assert submit(scene, **over).status_code == 409
    assert not model.calls and not Ledger(store, settings).entries()


def test_damaged_animation_and_large_inputs_reject_before_call(scene):
    http, store, model, payload, raw, settings = scene
    accept_upload(store, RUN, 'broken.png', b'bad bytes')
    assert submit(scene, name='broken.png', expected_sha256=hashlib.sha256(b'bad bytes').hexdigest()).status_code == 409
    animated = io.BytesIO()
    Image.new('RGB',(10,10),'red').save(animated, format='PNG',save_all=True,
        append_images=[Image.new('RGB',(10,10),'blue')])
    for name, raw in [('animated.png',animated.getvalue()), ('big.png', b'x'*(4*1024*1024+1))]:
        accept_upload(store,RUN,name,raw)
        assert submit(scene,name=name,expected_sha256=hashlib.sha256(raw).hexdigest()).status_code == 409
    assert not model.calls and not Ledger(store, settings).entries()


def test_stale_image_and_manifest_cannot_accept_old_result(scene):
    http, store, _, _, _, _ = scene
    wait_job(http, submit(scene).json())
    c = candidate(http)
    assert confirm(http,c,revision='0'*64).status_code == 409
    (store.run_dir(RUN)/'materials'/'fixture.png').write_bytes(b'changed')
    assert candidate(http)['status'] == 'stale'
    assert confirm(http,c).status_code == 409
    assert not (store.run_dir(RUN)/'evidence'/'observations.json').exists()


@pytest.mark.parametrize('selection',[[],[True],['0'],[-1],[2],[0,0]])
def test_invalid_selection_never_changes_evidence(scene, selection):
    http, store, _, _, _, _ = scene
    wait_job(http, submit(scene).json())
    assert confirm(http,candidate(http),selection).status_code in {400,422}
    assert not (store.run_dir(RUN)/'evidence'/'observations.json').exists()


@pytest.mark.parametrize('mode',['invalid_json','truncated','unknown_usage','transport_failure'])
def test_failures_keep_usage_and_never_retry_or_auto_accept(scene, mode):
    http, store, model, _, _, settings = scene
    if mode == 'invalid_json': model.turn.text = 'not json'
    if mode == 'truncated': model.turn.stop_reason = 'max_tokens'
    if mode == 'unknown_usage': model.turn.usage = Usage(None,None)
    if mode == 'transport_failure': model.error = ModelError(503,'fixture failure')
    job = wait_job(http,submit(scene).json())
    assert job['status'] == ('succeeded' if mode=='unknown_usage' else 'failed')
    rows = Ledger(store,settings).entries(RUN)
    assert len(rows)==1 and len(model.calls)==1
    assert rows[0].input_tokens == (None if mode in {'unknown_usage','transport_failure'} else 110)
    assert not (store.run_dir(RUN)/'evidence'/'observations.json').exists()
    if mode in {'unknown_usage','transport_failure'}:
        assert submit(scene,request_id='blocked').status_code==409
        assert len(model.calls)==1


def test_open_stage_attempt_blocks_call(scene):
    http, store, model, _, _, _ = scene
    Pipeline(store).start_attempt(RUN,'intake')
    assert submit(scene).status_code==409
    assert not model.calls


def test_cancel_waits_for_settlement_and_keeps_returned_candidate_without_adopting(scene):
    http, store, model, _, _, settings = scene
    model.release.clear()
    job = submit(scene).json()
    assert model.entered.wait(2)
    stopped = http.post(f"/api/runs/{RUN}/jobs/{job['job_id']}/cancel", json={})
    assert stopped.json()['status'] == 'cancelling'
    model.release.set()
    assert wait_job(http, job)['status'] == 'cancelled'
    assert candidate(http)['status'] == 'pending'
    assert Ledger(store, settings).entries(RUN)[0].usage_status == 'known'
    assert len(model.calls) == 1
    assert not (store.run_dir(RUN)/'evidence'/'observations.json').exists()


def test_protocols_receive_image_bytes_on_single_http_attempt(monkeypatch):
    from studio.agents import openai_compat
    payload={'type':'image','source':{'type':'base64','media_type':'image/png','data':'synthetic-base64'}}
    messages=[{'role':'user','content':[payload,{'type':'text','text':'observe'}]}]
    captured=[]
    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200,json={'model':'vision', 'choices':[{'message':{'content':'observed'},'finish_reason':'stop'}],
                                       'usage':{'prompt_tokens':4,'completion_tokens':2}})
    real=httpx.Client
    monkeypatch.setattr(openai_compat.httpx,'Client',lambda **kw:real(transport=httpx.MockTransport(handle),**kw))
    OpenAICompatClient('https://fixture.invalid/v1','synthetic').complete(model='vision',system='',messages=messages,tools=[])
    assert len(captured)==1
    image=captured[0]['messages'][0]['content'][0]
    assert image=={'type':'image_url','image_url':{'url':'data:image/png;base64,synthetic-base64'}}
    # The existing Anthropic payload keeps the canonical image block unchanged.
    assert AnthropicCompatClient('https://fixture.invalid','synthetic')._payload('vision','',messages,[])['messages']==messages
