"""Real ingress/producer regressions for turn freshness and question binding."""
import copy

import pytest

from fakes import ScriptedModel, final_turn, tool_turn, fixture_task_interpretation
from local_http import TestClient
from studio.agents.orchestrator import Orchestrator
from studio.conversation_input import receive, accept, start_execution, record_task_interpretation
from studio.interactions import ask, pending_question, record_answer
from studio.models import PipelineError
from studio.server import build_app
from test_conversation_journey import QUESTION, wait
from test_revision_scope import env, RUN, plan_args


def route(text, goal='writing', **extra):
    proposal = fixture_task_interpretation(text, intent=extra.pop('intent', 'new'), deliverable=goal)
    proposal.update(extra)
    return tool_turn('route', 'prepare_creative_context', {
        'intent': 'continue' if 'reuse_artifact' in proposal else 'new', 'format': 'video',
        'deliverable': goal, 'task_interpretation': proposal})


@pytest.mark.parametrize('goal', ['intake', 'direction', 'writing'])
def test_new_delivery_does_not_complete_with_old_valid_artifact(env, goal):
    store, pipe, settings, _ = env
    text = {'intake': '请重新整理需求，旧稿仅供对照，本轮重新交付。',
            'direction': '请重新做创意方向，旧稿仅供对照，本轮重新交付。',
            'writing': '请重新做一套完整视频方案，旧稿仅供对照，本轮重新交付。'}[goal]
    model = ScriptedModel([route(text, goal), final_turn('新版已完成。'), final_turn('新版已完成。')],
                          legacy_task_contracts=False)
    before = store.latest_artifact(RUN, goal)
    with TestClient(build_app(settings, client_factory=lambda: model)) as http:
        receipt = http.post(f'/api/runs/{RUN}/jobs', json={'text': text, 'request_id': 'freshness'})
        assert receipt.status_code == 200, receipt.text
        job = wait(http, RUN, receipt.json())
    assert job['status'] == 'blocked', job
    assert job['deliverable']['status'] == 'incomplete'
    assert '本轮准确新版本' in str(job['deliverable']['missing'])
    assert store.latest_artifact(RUN, goal) == before
    assert '旧稿' in model.requests_text()


def test_explicit_exact_reuse_does_not_force_rewriting(env):
    store, pipe, settings, ledger = env
    text = '继续沿用现有完整方案，原稿不用重写。'
    artifact = store.latest_artifact(RUN, 'writing')
    reuse = {'stage': 'writing', 'version': artifact.version, 'sha256': artifact.sha256, 'quote': text}
    model = ScriptedModel([route(text, intent='continue', reuse_artifact=reuse), final_turn('已保留当前方案。')],
                          legacy_task_contracts=False)
    turn = Orchestrator(store, pipe, settings, model, ledger, RUN).send(text)
    assert turn.deliverable['status'] == 'complete'
    assert store.latest_artifact(RUN, 'writing') == artifact
    assert len(model.calls) == 2


def test_new_saved_writing_completes_this_turn_and_keeps_old_version(env):
    store, pipe, settings, ledger = env
    text = '重新交付一份视频方案。'
    old = store.latest_artifact(RUN, 'writing')
    payload = copy.deepcopy(old.structured)
    payload['segments'][0]['line'] = 'A new opening'
    model = ScriptedModel([
        route(text), tool_turn('dispatch', 'dispatch', {'stage': 'writing', 'brief': text}),
        tool_turn('save', 'submit_stage', {'content': '', 'summary': 'New writing',
                  'structured': payload, 'evidence': ['fixture-source'], 'limitations': ['actual audio unverified']}),
        final_turn('新版方案已保存。')], legacy_task_contracts=False)
    turn = Orchestrator(store, pipe, settings, model, ledger, RUN).send(text)
    assert turn.deliverable['status'] == 'complete'
    assert store.latest_artifact(RUN, 'writing').version == old.version + 1
    assert store.verify_artifact(RUN, 'writing', old.version)


def test_new_whole_delivery_cannot_downgrade_to_local_revision_plan(env):
    store, pipe, settings, ledger = env
    text = '这次重新构思整个视频方案，全文重写。'
    args = plan_args(store, quote=text)
    model = ScriptedModel([route(text), tool_turn('plan', 'plan_revision', args),
                          final_turn('需要按整稿重新交付。'), final_turn('尚未交付。')],
                          legacy_task_contracts=False)
    result = Orchestrator(store, pipe, settings, model, ledger, RUN).send(text)
    assert result.deliverable['status'] == 'incomplete'
    assert any(row['tool'] == 'plan_revision' and not row['ok'] for row in result.tool_log)
    assert not any(e['kind'] == 'agent_revision_planned' for e in store.read_events(RUN))
    assert store.latest_artifact(RUN, 'writing').version == 1


@pytest.mark.parametrize('text', ['先不要选，我只是问目前做到哪一步了？', '停止', '我想先讨论为什么要问这个问题'])
def test_unrelated_message_preserves_pending_question(env, text):
    store, _, settings, _ = env
    model = ScriptedModel([tool_turn('ask', 'ask_user', QUESTION), final_turn('仍等待你的选择。')],
                          legacy_task_contracts=False)
    with TestClient(build_app(settings, client_factory=lambda: model)) as http:
        first = http.post(f'/api/runs/{RUN}/jobs', json={'text': '先确认表达', 'request_id': 'ask'})
        assert wait(http, RUN, first.json())['status'] == 'succeeded'
        question = pending_question(store, RUN)
        second = http.post(f'/api/runs/{RUN}/jobs', json={'text': text, 'request_id': 'unrelated'})
        assert wait(http, RUN, second.json())['status'] == 'succeeded'
    assert pending_question(store, RUN)['id'] == question['id']
    assert not any(e['kind'] == 'interaction_answered' for e in store.read_events(RUN))


def test_free_text_answer_is_grounded_once_without_adoption(env):
    store, pipe, settings, ledger = env
    question = ask(store, RUN, QUESTION)
    text = '更偏日常分享，但别夸张。'
    model = ScriptedModel([tool_turn('answer', 'record_question_answer', {
        'question_id': question['id'], 'quote': '更偏日常分享'}), final_turn('按日常分享继续讨论。')],
        legacy_task_contracts=False)
    before = store.list_approvals(RUN)
    Orchestrator(store, pipe, settings, model, ledger, RUN).send(text)
    assert pending_question(store, RUN) is None
    answered = [e for e in store.read_events(RUN) if e['kind'] == 'interaction_answered']
    assert len(answered) == 1 and answered[0]['text'] == '更偏日常分享'
    assert answered[0]['source_message_id']
    assert store.list_approvals(RUN) == before


def test_question_proposal_cannot_answer_later_question_or_fabricated_words(env):
    store, _, _, _ = env
    first = ask(store, RUN, QUESTION)
    receipt = receive(store, RUN, '更偏日常分享', 'answer-source')
    accept(store, RUN, receipt); start_execution(store, RUN, receipt)
    with pytest.raises(PipelineError):
        record_answer(store, RUN, receipt, question_id=first['id'], quote='直接演示')
    second = ask(store, RUN, {**QUESTION, 'question': '下一步更偏哪种表达？'})
    with pytest.raises(PipelineError):
        record_answer(store, RUN, receipt, question_id=second['id'], quote=receipt['text'])
    assert pending_question(store, RUN)['id'] == second['id']


def test_readonly_answer_proposal_cannot_consume_question(env):
    store, _, _, _ = env
    question = ask(store, RUN, QUESTION)
    receipt = receive(store, RUN, '现在进度怎么样？', 'status-source')
    accept(store, RUN, receipt); start_execution(store, RUN, receipt)
    with pytest.raises(PipelineError):
        record_answer(store, RUN, receipt, question_id=question['id'], quote=receipt['text'])
    assert pending_question(store, RUN)['id'] == question['id']


def test_analysis_choice_is_a_valid_free_text_answer(env):
    store, pipe, settings, ledger = env
    question = ask(store, RUN, {'question': '本轮只分析参考还是直接写方案？',
        'options': [{'label': '只分析参考'}, {'label': '直接写方案'}]})
    text = '只分析参考，不要开始创作'
    model = ScriptedModel([tool_turn('answer', 'record_question_answer', {
        'question_id': question['id'], 'quote': '只分析参考'}), final_turn('已明确本轮只分析。')],
        legacy_task_contracts=False)
    Orchestrator(store, pipe, settings, model, ledger, RUN).send(text)
    assert pending_question(store, RUN) is None
    answered = [e for e in store.read_events(RUN) if e['kind'] == 'interaction_answered']
    assert answered[-1]['text'] == '只分析参考'


def test_grounded_discussion_scope_cannot_answer_preference(env):
    store, _, _, _ = env
    question = ask(store, RUN, QUESTION)
    text = '暂时先别选，我只想知道这套表达为何合适。'
    receipt = receive(store, RUN, text, 'discussion-source')
    accept(store, RUN, receipt); start_execution(store, RUN, receipt)
    record_task_interpretation(store, RUN, receipt, {'task': {'quote': text,
        'mode': 'discuss', 'deliverable': 'follow_up', 'format': 'none',
        'allowed_stages': [], 'pause_after': False}, 'constraints': []})
    with pytest.raises(PipelineError):
        record_answer(store, RUN, receipt, question_id=question['id'], quote=text)
    assert pending_question(store, RUN)['id'] == question['id']
