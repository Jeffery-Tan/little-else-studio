"""Backend authority and actual HTTP/job effects, with deliberately bad models.

Scripted models do not establish general language understanding. These tests
exercise how the backend limits proposals and what its execution receipts prove.
"""
from __future__ import annotations

import threading
import time

import pytest
from local_http import TestClient

from fakes import ScriptedModel, final_turn, tool_turn
from studio.agents.orchestrator import Orchestrator
from studio.conversation_input import display_context, receive
from studio.decision import candidates, commit, DecisionError, resolve
from studio.ledger import Ledger
from studio.pipeline import Pipeline
from studio.server import build_app
from studio.store import Store
from studio.timeline import build_timeline
from test_conversational_decision import RUN, seed_direction, settings_for
from test_decision import received_message


def proposal(store, pipeline, option='B', **extra):
    card = candidates(store, pipeline, RUN)[0]
    return {'action': 'select_direction', 'target': card['target'],
            'option_id': option, **extra}


def perform(store, pipeline, text, offered, *, receipt=None):
    receipt = receipt or received_message(store, pipeline, RUN, text)
    model = ScriptedModel([tool_turn('decision', 'propose_user_action', offered), final_turn('模型声称已开始。')])
    settings = settings_for(store.root)
    orch = Orchestrator(store, pipeline, settings, model, Ledger(store, settings), RUN)
    result = orch.send(text, user_input_id=receipt['message_id'])
    return result, receipt, model


@pytest.mark.parametrize('text', [
    '不要采用 B', '如果采用 B，会怎么样？', '同事说“采用 B”，你怎么看？',
    '采用 C', '采用 A 和 B', 'B 的材料费用是多少', '先把 B 的开头改一下',
    '还没决定采用 B', 'B 不合适', '聊聊 B',
])
def test_wrong_model_proposals_cannot_convert_other_speech_into_adoption(tmp_path, text):
    store, pipeline = seed_direction(tmp_path)
    _, _, model = perform(store, pipeline, text, proposal(store, pipeline))
    assert store.list_approvals(RUN) == []
    assert not any(e['kind'] == 'decision_followup_requested' for e in store.read_events(RUN))
    assert len(model.calls) == 2


@pytest.mark.parametrize('reply', ['行。', '不行', '不要', '我想想', '再说', '停一下'])
def test_abc_card_alone_does_not_supply_a_short_reply_focus(tmp_path, reply):
    store, pipeline = seed_direction(tmp_path)
    perform(store, pipeline, reply, proposal(store, pipeline))
    assert store.list_approvals(RUN) == []


@pytest.mark.parametrize('option,reply,approved', [('B', '行。', True), ('C', '行。', False),
                                               ('B', '不行', False), ('B', '我想想', False)])
def test_short_reply_binds_to_saved_explicit_adjacent_question(tmp_path, option, reply, approved):
    store, pipeline = seed_direction(tmp_path)
    store.append_chat(RUN, 'assistant', '是否采用方向 B？')
    perform(store, pipeline, reply, proposal(store, pipeline, option))
    assert bool(store.list_approvals(RUN)) is approved


@pytest.mark.parametrize('text', ['先采用 B，但暂时别继续', '采用 B，暂时不要往下做',
                                  '采用 B，等我通知再继续', '采用 B，不要自动接续',
                                  '采用 B，先停一下'])
def test_adoption_and_continuation_are_separate(tmp_path, text):
    store, pipeline = seed_direction(tmp_path)
    result, _, _ = perform(store, pipeline, text, proposal(store, pipeline))
    approval = store.list_approvals(RUN)[0]
    assert approval.selected_option_id == 'B'
    assert approval.continue_after is False
    assert '暂停' in result.text
    assert not any(e['kind'] == 'decision_followup_requested' for e in store.read_events(RUN))


def test_actual_message_ids_display_and_single_consumption_are_checked(tmp_path):
    store, pipeline = seed_direction(tmp_path)
    receipt = received_message(store, pipeline, RUN, '采用 B')
    offered = proposal(store, pipeline, message_id='wrong-message')
    assert resolve(candidates(store, pipeline, RUN), offered, receipt=receipt)['status'] != 'commit'
    offered = proposal(store, pipeline, display_context_id='wrong-context')
    assert resolve(candidates(store, pipeline, RUN), offered, receipt=receipt)['status'] != 'commit'
    offered = proposal(store, pipeline, evidence_quote='采用 C')
    assert resolve(candidates(store, pipeline, RUN), offered, receipt=receipt)['status'] != 'commit'
    verdict = resolve(candidates(store, pipeline, RUN), proposal(store, pipeline), receipt=receipt)
    result = commit(store, pipeline, RUN, verdict['action'], source='conversation',
                    user_message='model forged: 采用 C', request_id=receipt['request_id'])
    assert result['selected_option_id'] == 'B'
    with pytest.raises(DecisionError, match='message_consumed'):
        commit(store, pipeline, RUN, verdict['action'], source='conversation')
    assert len(store.list_approvals(RUN)) == 1


def test_valid_but_never_displayed_current_version_is_rejected(tmp_path):
    store, pipeline = seed_direction(tmp_path)
    receipt = received_message(store, pipeline, RUN, '采用 B')
    old = pipeline.stage_state(RUN, 'direction').latest
    pipeline.record_approval(RUN, 'direction', nonce=pipeline.issue_nonce(RUN, 'direction'),
                             decision='returned', note='改一下')
    attempt = pipeline.start_attempt(RUN, 'direction')
    pipeline.submit(RUN, 'direction', attempt.token, content='new', summary='new', structured=old.structured)
    perform(store, pipeline, '采用 B', proposal(store, pipeline), receipt=receipt)
    assert len(store.list_approvals(RUN)) == 1
    assert store.list_approvals(RUN)[0].decision == 'returned'


def test_missing_or_foreign_view_cannot_become_authority(tmp_path):
    store, pipeline = seed_direction(tmp_path)
    receipt = receive(store, RUN, '采用 B', 'without-view')
    perform(store, pipeline, '采用 B', proposal(store, pipeline), receipt=receipt)
    assert store.list_approvals(RUN) == []
    store.create_run('other-run', 'Other', 'Other')
    with pytest.raises(Exception, match='display_context'):
        receive(store, 'other-run', '采用 B', 'cross-run', received_message(store, pipeline, RUN, '采用 B')['display_context_id'])


def wait_jobs(app, run_id=RUN, *, timeout=10):
    deadline = time.monotonic() + timeout
    while app.state.jobs.busy(run_id) and time.monotonic() < deadline:
        time.sleep(.01)
    if app.state.jobs.busy(run_id):
        raise AssertionError(
            f'isolated jobs did not settle within {timeout}s: '
            f'{app.state.jobs.list(run_id)}'
        )


def test_http_decision_runs_one_successor_and_produces_real_downstream_artifact(tmp_path):
    store, pipeline = seed_direction(tmp_path)
    directive = proposal(store, pipeline, continuation_note='开头生活化一点')
    models = [ScriptedModel([tool_turn('decision', 'propose_user_action', directive),
                             final_turn('已经开始。')]),
              ScriptedModel([
                  tool_turn('context', 'prepare_creative_context', {'intent': 'edit', 'format': 'video', 'deliverable': 'writing'}),
                  tool_turn('materials', 'dispatch', {'stage': 'material_plan', 'brief': '按 B 整理素材'}),
                  tool_turn('material-doc', 'submit_stage', {'stage': 'material_plan', 'content': 'Synthetic material plan', 'summary': 'material'}),
                  tool_turn('writing', 'dispatch', {'stage': 'writing', 'brief': '开头生活化一点'}),
                  tool_turn('writing-doc', 'submit_stage', {'stage': 'writing', 'content': 'Synthetic writing', 'summary': 'writing',
                      'structured': {'segments': [{'tc': '0-2s', 'picture': 'office lunch', 'line': 'Everyday.', 'sound': 'room', 'duration': 2}], 'total_duration': 2}}),
                  tool_turn('delivery-check', 'submit_delivery_check', {'verdict': 'ready',
                      'summary': 'Synthetic saved script check for this offline lifecycle.', 'findings': []}),
                  final_turn('当前完整方案已保存，请阅读。')])]
    factory_calls = []
    def factory():
        factory_calls.append(1)
        return models[len(factory_calls) - 1]
    provider_calls = []
    def provider():
        provider_calls.append(1)
        raise AssertionError('conversation must not call media providers')
    app = build_app(settings_for(tmp_path), client_factory=factory,
                    production_image_factory=provider, production_video_factory=provider)
    with TestClient(app) as client:
        first = client.get(f'/api/runs/{RUN}/timeline').json()
        same = client.get(f'/api/runs/{RUN}/timeline').json()
        assert same['display_context_id'] == first['display_context_id']
        payload = {'text': '采用 B，开头生活化一点，然后继续。', 'request_id': 'http-choose-b',
                   'display_context_id': first['display_context_id']}
        response = client.post(f'/api/runs/{RUN}/jobs', json=payload)
        assert response.status_code == 200, response.text
        parent_id = response.json()['job_id']
        # Wait for both the decision job and the material/writing successor.
        # Durable offline execution is not a ten-second performance contract.
        wait_jobs(app, timeout=30)
        parent = client.get(f'/api/runs/{RUN}/jobs/{parent_id}').json()
        assert parent['status'] == 'succeeded' and not parent['reason'], parent
        assert parent['successor_job_id']
        successor = client.get(f"/api/runs/{RUN}/jobs/{parent['successor_job_id']}").json()
        assert successor['status'] == 'succeeded', successor
        assert pipeline.stage_state(RUN, 'writing').latest
        assert '生活化' in successor['text']
        retry = client.post(f'/api/runs/{RUN}/jobs', json=payload)
        assert retry.status_code == 200 and retry.json()['job_id'] == parent_id
        approval = store.list_approvals(RUN, 'direction')[0]
        # The UI recovery route resolves the very same successor, even after
        # the next artifact exists. No second job/model call is bought.
        continued = client.post(f'/api/runs/{RUN}/decision-continuation', json={
            'stage': 'direction', 'expected_version': approval.artifact_version,
            'expected_artifact_sha256': approval.artifact_sha256})
        assert continued.status_code == 200, continued.text
        assert continued.json()['job_id'] == successor['job_id']
        assert len(factory_calls) == 2 and provider_calls == []
        assert models[1].calls[-2]['tool_names'] == ['submit_delivery_check']
        assert models[1].calls[-1]['tool_names'] == []
        assert len([e for e in store.read_events(RUN) if e['kind'] == 'delivery_self_check']) == 1
        assert len([e for e in store.read_events(RUN) if e['kind'] == 'decision_followup_consumed']) == 1
        assert len([e for e in store.read_events(RUN) if e['kind'] == 'user_message_received']) == 1
        assert [e['text'] for e in store.read_chat(RUN) if e['role'] == 'user'] == [payload['text']]
        assert not client.get(f'/api/runs/{RUN}/timeline').json()['pending_continuation']


def test_http_freezes_view_at_ingress_and_rejected_requests_do_not_enter_chat(tmp_path):
    store, pipeline = seed_direction(tmp_path)
    entered, release = threading.Event(), threading.Event()
    class Slow(ScriptedModel):
        def complete(self, **kwargs):
            entered.set()
            assert release.wait(5)
            return super().complete(**kwargs)
    model = Slow([final_turn('已回答。')])
    app = build_app(settings_for(tmp_path), client_factory=lambda: model)
    with TestClient(app) as client:
        view = client.get(f'/api/runs/{RUN}/timeline').json()
        accepted = client.post(f'/api/runs/{RUN}/jobs', json={
            'text': '进度怎样', 'request_id': 'accepted', 'display_context_id': view['display_context_id']})
        assert accepted.status_code == 200 and entered.wait(2)
        rejected = client.post(f'/api/runs/{RUN}/jobs', json={
            'text': '采用 B', 'request_id': 'busy-rejected', 'display_context_id': view['display_context_id']})
        assert rejected.status_code == 409
        assert '采用 B' not in [e['text'] for e in store.read_chat(RUN)]
        receipts = [e for e in store.read_events(RUN) if e['kind'] == 'user_message_received']
        assert receipts[0]['displayed']['decisions'][0]['version'] == view['pending']['version']
        assert receipts[0]['displayed']['decisions'][0]['artifact_sha256'] == view['pending']['artifact_sha256']
        release.set()
        wait_jobs(app)


def amendment_scene(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Amend', 'Synthetic goal')
    pipeline = Pipeline(store)
    for stage in ('intake', 'direction'):
        attempt = pipeline.start_attempt(RUN, stage)
        structured = {'options': [{'key': k, 'claim': k, 'amend_clause': 'goal.primary',
                                   'amend_proposal': 'New synthetic goal'} for k in 'ABC'], 'recommended': '推荐 B'} if stage == 'direction' else {}
        pipeline.submit(RUN, stage, attempt.token, content=f'Synthetic {stage}', summary=stage, structured=structured)
    card = candidates(store, pipeline, RUN)[0]
    assert card['kind'] == 'brief_amendment'
    amendment = next(a for a in card['answers'] if a['action'] == 'amend_brief')
    return store, pipeline, card, amendment


def test_amendment_does_not_first_approve_invalid_direction(tmp_path):
    from studio.active_brief import load_amendments
    store, pipeline, card, amendment = amendment_scene(tmp_path)
    perform(store, pipeline, '按这份建议修订简报，但暂时别继续',
            {**amendment, 'target': card['target'], 'payload': {'text': 'model forged'}})
    assert store.list_approvals(RUN) == []
    rows = load_amendments(store, RUN)
    assert len(rows) == 1 and rows[0]['status'] == 'approved'
    assert rows[0]['changes'][0]['text'] == 'New synthetic goal'
    assert pipeline.stage_state(RUN, 'direction').stale
    assert not any(e['kind'] == 'decision_followup_requested' for e in store.read_events(RUN))


def test_http_amendment_runs_its_own_redo_once_without_invalid_direction_approval(tmp_path):
    from studio.active_brief import load_amendments, fingerprint_for
    store, pipeline, card, amendment = amendment_scene(tmp_path)
    old = store.read_artifact_text(RUN, 'direction', 1)
    models = [ScriptedModel([tool_turn('amend', 'propose_user_action',
                                     {**amendment, 'target': card['target']}), final_turn('修订保存。')]),
              ScriptedModel([
                  tool_turn('context', 'prepare_creative_context', {'intent': 'edit', 'format': 'video', 'deliverable': 'direction'}),
                  final_turn('按新简报重做。'),
                  tool_turn('new-directions', 'submit_stage', {'content': 'Synthetic revised direction', 'summary': 'direction',
                      'structured': {'options': [{'key': k, 'claim': k, 'satisfies': 'goal.primary'} for k in 'ABC']}}),
              ])]
    factory_calls = []
    def factory():
        factory_calls.append(1)
        return models[len(factory_calls) - 1]
    app = build_app(settings_for(tmp_path), client_factory=factory)
    with TestClient(app) as client:
        view = client.get(f'/api/runs/{RUN}/timeline').json()
        payload = {'text': '按这份建议修订简报，然后重做方向', 'request_id': 'amend-http',
                   'display_context_id': view['display_context_id']}
        saved = client.post(f'/api/runs/{RUN}/jobs', json=payload)
        assert saved.status_code == 200, saved.text
        wait_jobs(app)
        parent = client.get(f"/api/runs/{RUN}/jobs/{saved.json()['job_id']}").json()
        assert parent['status'] == 'succeeded' and not parent['reason'], parent
        assert parent['successor_job_id']
        successor = client.get(f"/api/runs/{RUN}/jobs/{parent['successor_job_id']}").json()
        assert successor['status'] == 'succeeded', successor
        assert store.list_approvals(RUN) == []
        assert len(load_amendments(store, RUN)) == 1
        latest = pipeline.stage_state(RUN, 'direction').latest
        assert latest.version == 2 and latest.inputs['brief:snapshot'] == fingerprint_for(store, RUN)
        assert store.read_artifact_text(RUN, 'direction', 1) == old
        again = client.post(f'/api/runs/{RUN}/jobs', json=payload)
        assert again.status_code == 200 and again.json()['job_id'] == parent['job_id']
        assert len(factory_calls) == 2
        assert len([e for e in store.read_events(RUN) if e['kind'] == 'decision_followup_consumed']) == 1
