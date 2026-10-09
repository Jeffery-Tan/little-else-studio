"""Request/context assembly only; synthetic pixels and mock replies, no model-quality claim."""
import base64
import hashlib
import json

import pytest

from fakes import ScriptedModel, final_turn
from studio.agent_images import observe, saved_observations
from studio.agents.orchestrator import Orchestrator
from studio.creative_evidence import creative_evidence
from studio.ledger import Ledger
from studio.pipeline import Pipeline
from test_agent_images import call as bailian_call, result
from test_bailian_omni import native
from test_deepseek_client import transport
from test_image_analysis import scene, submit, wait_job
from test_image_understanding import configure, frozen, answer
from test_server import RUN


def assert_expression_boundary(system):
    # These assertions prove the first request receives the distinctions;
    # the synthetic response deliberately provides no evidence of compliance.
    for form in ('钩扣形状', '灰色光泽', '黑色绑带', '分离肩带图示'):
        assert form in system
    assert '未操作拉伸或拆卸' in system
    assert '不能断言弹性或可拆卸' in system
    assert '光泽与形状不能确认金属或塑料' in system
    assert '图中文字' in system
    assert '已验证' in system
    assert '数量或细节' in system
    assert '正文' in system and '泛化免责声明抵消' in system


def assert_bound_image_message(text):
    assert '文件名只是索引，不是事实：fixture.png' in text
    assert '图中文字注明为文字声明' in text
    assert '不能确认的功能、材质、数量或细节明确未知' in text
    assert '不借其他图片或文件名补齐' in text


@pytest.mark.parametrize('purpose', ['product', 'reference'])
def test_mimo_first_json_request_receives_shape_function_and_claim_boundaries(scene, monkeypatch, purpose):
    connections, _ = configure(scene)
    route = frozen(scene, connections)
    seen = transport(monkeypatch, [answer()])
    observed = observe(scene[1], scene[5], RUN, [dict(name='fixture.png', purpose=purpose)],
        endpoint=None, emit=lambda _: None, check_cancel=lambda: None, freeze_route=lambda: route)
    assert len(seen) == 1
    payload = json.loads(seen[0].content)
    assert payload['response_format'] == {'type': 'json_object'}
    assert 'tools' not in payload and 'tool_choice' not in payload
    assert_expression_boundary(payload['messages'][0]['content'])
    parts = payload['messages'][1]['content']
    assert_bound_image_message(next(part['text'] for part in parts if part['type'] == 'text'))
    pixels = [part['image_url']['url'] for part in parts if part['type'] == 'image_url']
    assert len(pixels) == 1 and base64.b64decode(pixels[0].split(',', 1)[1]) == scene[4]
    row = observed['observations'][0]
    assert row['sha256'] == hashlib.sha256(scene[4]).hexdigest() and row['purpose'] == purpose
    assert row['findings'] == ['visible panel 0']
    assert row['source_kind'] == 'agent_model_observation' and 'confirmed_at' not in row


@pytest.mark.parametrize('purpose', ['product', 'reference'])
def test_bailian_first_batch_request_receives_same_expression_boundary(native, purpose):
    native[2].turn = result()
    observed = bailian_call(native, [dict(name='fixture.png', purpose=purpose)])
    assert len(native[2].calls) == 1
    request = native[2].calls[0]
    assert_expression_boundary(request['system'])
    assert '"images"' in request['system'] and request['tools'] == []
    parts = request['messages'][0]['content']
    assert_bound_image_message(next(part['text'] for part in parts if part['type'] == 'text'))
    pixels = [part['source']['data'] for part in parts if part['type'] == 'image']
    assert len(pixels) == 1 and base64.b64decode(pixels[0]) == native[4]
    row = observed['observations'][0]
    assert row['sha256'] == hashlib.sha256(native[4]).hexdigest() and row['purpose'] == purpose
    assert row['findings'] == ['visible panel 0']


@pytest.mark.parametrize('purpose', ['product', 'reference'])
def test_manual_first_image_request_uses_shared_expression_boundary(scene, purpose):
    response = submit(scene, purpose=purpose)
    assert response.status_code == 200, response.text
    assert wait_job(scene[0], response.json())['status'] == 'succeeded'
    assert len(scene[2].calls) == 1
    request = scene[2].calls[0]
    assert_expression_boundary(request['system'])
    assert '"findings"' in request['system'] and '"limitations"' in request['system']
    assert request['tools'] == []
    image = next(part['source']['data'] for part in request['messages'][0]['content'] if part['type'] == 'image')
    assert base64.b64decode(image) == scene[4]
    assert scene[1].list_approvals(RUN) == []
    assert not creative_evidence(scene[1], RUN)['product_observations']


def test_producer_first_context_preserves_old_report_and_restricts_claims_without_reobservation(native):
    # An intentionally overconfident historical model report remains readable;
    # request instructions must not rewrite it into a fabricated fact or proof.
    native[2].turn = result()
    report = json.loads(native[2].turn.text)
    report['images'][0]['findings'] = ['黑色塑料可拆卸肩带', '黑色弹性绑带', '两端金属钩扣']
    native[2].turn.text = json.dumps(report, ensure_ascii=False)
    bailian_call(native, [dict(name='fixture.png', purpose='product')])
    before = saved_observations(native[1], RUN)
    original_event = next(event for event in native[1].read_events(RUN) if event['kind'] == 'agent_images_observed')
    model = ScriptedModel([final_turn('这里只汇总已有记录。')])
    orchestrator = Orchestrator(native[1], Pipeline(native[1]), native[5], model,
        Ledger(native[1], native[5]), RUN)
    orchestrator.send('现在进度怎样？')
    assert len(model.calls) == 1 and len(native[2].calls) == 1
    system = model.calls[0]['system']
    assert_expression_boundary(system)
    assert '包括缓存或历史观察' in system
    assert '无其他当前已采用且可核对的依据时' in system
    assert '不把观察中的功能、材质或宣传说法升级为确定商品事实' in system
    for original in report['images'][0]['findings']:
        assert original in system
    assert 'agent_model_observation' in system and before[0]['sha256'] in system
    assert saved_observations(native[1], RUN) == before
    observations = [event for event in native[1].read_events(RUN) if event['kind'] == 'agent_images_observed']
    assert observations == [original_event]
    assert native[1].list_approvals(RUN) == []
    assert not any('confirmed_at' in row for row in creative_evidence(native[1], RUN)['product_observations'])
