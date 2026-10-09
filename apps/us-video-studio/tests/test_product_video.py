"""Explicit TikTok product evidence stays separate from reference analysis."""
import copy
import hashlib
import json

from fakes import tool_turn, final_turn, fixture_task_interpretation
from studio.agents.client import Turn, Usage
from studio.creative_evidence import creative_evidence
from studio.image_analysis import candidates
from studio.materials import resolve
from test_bailian_omni import native, MODEL, RESULT
from test_image_analysis import scene, confirm
from test_natural_reference import chat, scripted, inspect
from test_reference_video import URL, downloader as reference_downloader
from test_server import RUN
from test_video_observation import video_bytes


def product_tool(quote='用这条商品视频补全商品信息', source=URL):
    return tool_turn('product', 'observe_product_video', dict(source=source, quote=quote))


def product_result():
    result = copy.deepcopy(RESULT)
    result['reference_methods'] = []
    return Turn(text=json.dumps(result), stop_reason='end_turn', model=MODEL, usage=Usage(100, 20))


def downloader(monkeypatch, raw):
    calls = []

    def fetch(url, cancel):
        cancel()
        calls.append(url)
        return raw, dict(video_id='7250000000000000000', source_url=url,
                         canonical_url=URL, sha256=hashlib.sha256(raw).hexdigest())

    monkeypatch.setattr('studio.product_video.fetch_reference', fetch)
    return calls


def test_product_link_creates_reviewable_candidate_without_reference_or_adoption(native, monkeypatch):
    raw = video_bytes(audio=True)
    downloads = downloader(monkeypatch, raw)
    scripted(native, monkeypatch, [product_tool(), product_result()])
    job = chat(native, '用这条商品视频补全商品信息 ' + URL)
    assert job['status'] == 'succeeded', job
    assert job['deliverable']['status'] == 'complete'
    assert downloads == [URL]
    source = next(e for e in native[1].read_events(RUN) if e['kind'] == 'product_video_source')
    assert resolve(native[1], RUN, source['name']).read_bytes() == raw
    rows = candidates(native[1], RUN)
    assert len(rows) == 1 and rows[0]['purpose'] == 'product' and rows[0]['status'] == 'pending'
    assert rows[0]['audiovisual']['audio_sent']
    assert not any(e['kind'].startswith('reference_video_') for e in native[1].read_events(RUN))
    assert not creative_evidence(native[1], RUN)['product_observations']
    assert not creative_evidence(native[1], RUN)['reference_observations']
    assert '待你核对并选择' in native[1].read_chat(RUN)[-1]['text']
    assert confirm(native[0], rows[0], [0]).status_code == 200
    assert len(creative_evidence(native[1], RUN)['product_observations']) == 1
    assert not creative_evidence(native[1], RUN)['reference_observations']


def test_explicit_repeat_reuses_saved_product_analysis(native, monkeypatch):
    downloads = downloader(monkeypatch, video_bytes(audio=True))
    scripted(native, monkeypatch, [product_tool(), product_result()])
    assert chat(native, '用这条商品视频补全商品信息 ' + URL)['status'] == 'succeeded'
    scripted(native, monkeypatch, [product_tool(quote='再看这条商品视频')])
    assert chat(native, '再看这条商品视频', request_id='product-again')['status'] == 'succeeded'
    assert downloads == [URL]
    assert len(native[2].calls) == 3
    assert len(candidates(native[1], RUN)) == 1


def test_canonical_alias_reuses_product_source_without_download(native, monkeypatch):
    short = 'https://vm.tiktok.com/SyntheticAlias/'
    downloads = downloader(monkeypatch, video_bytes(audio=True))
    scripted(native, monkeypatch, [product_tool(source=short), product_result()])
    assert chat(native, '用这条商品视频补全商品信息 ' + short)['status'] == 'succeeded'
    scripted(native, monkeypatch, [product_tool(quote='再看这条商品视频', source=URL)])
    assert chat(native, '再看这条商品视频 ' + URL, request_id='product-alias')['status'] == 'succeeded'
    assert downloads == [short] and len(native[2].calls) == 3
    assert len(candidates(native[1], RUN)) == 1
    assert sum(e['kind'] == 'product_video_source' for e in native[1].read_events(RUN)) == 1


def test_bare_link_cannot_trigger_product_download(native, monkeypatch):
    downloads = downloader(monkeypatch, video_bytes(audio=True))
    scripted(native, monkeypatch, [product_tool(quote=URL), final_turn('这条视频是用于补全商品信息还是拆解参考表达？')])
    assert chat(native, URL)['status'] == 'blocked'
    assert downloads == []
    assert not candidates(native[1], RUN)


def test_clarified_product_purpose_uses_previous_user_link(native, monkeypatch):
    downloads = downloader(monkeypatch, video_bytes(audio=True))
    question = tool_turn('purpose', 'ask_user', dict(
        question='这条视频用于补全商品信息还是拆解参考表达？',
        options=[dict(label='补全商品信息'), dict(label='拆解参考表达')],
        recommended=0, context=''))
    scripted(native, monkeypatch, [question])
    assert chat(native, URL)['status'] == 'succeeded'
    assert downloads == []
    scripted(native, monkeypatch, [product_tool(quote='补全商品信息'), product_result()])
    assert chat(native, '补全商品信息', request_id='product-clarified')['status'] == 'succeeded'
    assert downloads == [URL]
    assert candidates(native[1], RUN)[0]['purpose'] == 'product'


def test_product_link_cannot_be_misrouted_to_reference(native, monkeypatch):
    downloads = reference_downloader(monkeypatch, video_bytes(audio=True))
    quote = '用这条商品视频补全商品信息'
    turn = inspect('analyze', source=URL, quote=quote)
    proposal = fixture_task_interpretation(quote + ' ' + URL, deliverable='analysis')
    proposal['video_use'] = dict(source=URL, purpose='product', quote=quote)
    turn.tool_calls[0].input['task_interpretation'] = proposal
    scripted(native, monkeypatch, [turn, final_turn('请按商品用途观察。')])
    assert chat(native, quote + ' ' + URL)['status'] == 'blocked'
    assert downloads == []
    assert not candidates(native[1], RUN)


def test_product_link_without_observation_consent_stays_unfetched(native, monkeypatch):
    downloads = downloader(monkeypatch, video_bytes(audio=True))
    scripted(native, monkeypatch, [product_tool(), final_turn('本轮未启用声画观察。')])
    assert chat(native, '用这条商品视频补全商品信息 ' + URL,
                request_id='product-no-consent', consent=False)['status'] == 'blocked'
    assert downloads == []
    assert not candidates(native[1], RUN)


def test_failed_product_analysis_reuses_download_only_after_new_request(native, monkeypatch):
    downloads = downloader(monkeypatch, video_bytes(audio=True))
    bad = Turn(text='bad json', stop_reason='end_turn', model=MODEL, usage=Usage(100, 20))
    scripted(native, monkeypatch, [product_tool(), bad, final_turn('本次观察失败，请稍后明确要求重试。')])
    assert chat(native, '用这条商品视频补全商品信息 ' + URL)['status'] == 'blocked'
    assert not candidates(native[1], RUN)
    scripted(native, monkeypatch, [product_tool(quote='重试补全商品信息'), product_result()])
    assert chat(native, '重试补全商品信息', request_id='product-retry')['status'] == 'succeeded'
    assert downloads == [URL]
    assert len(candidates(native[1], RUN)) == 1
