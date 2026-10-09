"""Current-source condition stops through actual Mock HTTP; no live model calls."""
import copy
import json
from threading import Event

import pytest

from fakes import ScriptedModel, final_turn, fixture_task_interpretation, tool_turn
from local_http import TestClient
from studio.agents.client import JobCancelled
from studio.agents.openai_compat import OpenAICompatClient
from studio.agents.orchestrator import Orchestrator
from studio.condition_blocker import for_current_input
from studio.conversation_input import (accept, constraints_for_inputs, interpreted_task_constraints,
                                       interpretation_for, receive, record_task_interpretation,
                                       start_execution)
from studio.ledger import Ledger
from studio.models import PipelineError, STAGES
from studio.production import draft_records
from studio.server import build_app
from studio.tools.execution import ToolResult
from studio.tools.workspace import Workspace
from test_finishing_phase import mock_http, reply
from test_production_feedback_scope import selected_target
from test_revision_scope import RUN, env, image_inputs
from test_role_input_boundaries import wait_job


TEXT = '只调整所选图片的背景，其余镜头与脚本保留；先保存制作输入，不生成媒体。'
QUESTION = '背景采用室内还是室外仍未确定'


def condition(text=TEXT, *, value='背景场景', ambiguity=QUESTION, lifetime='turn',
              stages=None, paths=None, intent='instruction', **extra):
    return {'quote': text, 'key': 'visual.setting', 'value': value, 'operation': 'add',
            'polarity': 'require', 'intent': intent,
            'scope': {'lifetime': lifetime, 'stages': stages or ['writing'],
                      'paths': paths or [], 'quote': text},
            'supersedes': [], 'conflict_with': [], 'ambiguity': ambiguity, **extra}


def proposal(text=TEXT, *rows, mode='revise', goal='production_input'):
    result = fixture_task_interpretation(text, intent='edit', deliverable=goal)
    result['task'].update(mode=mode, allowed_stages=[], pause_after=True)
    result['constraints'] = list(rows) if rows else [condition(text)]
    return result


def persist(store, declared, *, request='synthetic-condition', target=None):
    receipt = receive(store, RUN, declared['task']['quote'], request, feedback_target=target)
    accept(store, RUN, receipt)
    start_execution(store, RUN, receipt)
    saved = record_task_interpretation(store, RUN, receipt, declared)
    return receipt, saved


def projection(store, receipt, saved, **scope):
    return for_current_input(receipt, interpreted_task_constraints(receipt, saved),
                             constraints_for_inputs(store, RUN, [receipt['message_id']]), **scope)


def assert_no_content_changes(store, before, drafts):
    assert {stage: store.latest_artifact(RUN, stage) for stage in STAGES} == before[0]
    assert store.list_approvals(RUN) == before[1]
    assert draft_records(store, RUN) == drafts


@pytest.mark.parametrize('entry', ['prepare_creative_context', 'propose_task_interpretation'])
@pytest.mark.parametrize('reason', ['ambiguity', 'conflict'])
def test_first_saved_condition_stops_same_batch_and_reports_actual_reason(env, monkeypatch, entry, reason):
    store, pipe, settings, _ = env
    payload, drafts = image_inputs(env)
    target = selected_target(drafts[1])
    before = ({stage: store.latest_artifact(RUN, stage) for stage in STAGES}, store.list_approvals(RUN))
    before_drafts = draft_records(store, RUN)
    if reason == 'conflict':
        old_text = '整个项目的背景统一用室内'
        _, old = persist(store, proposal(old_text, condition(old_text, value='室内', ambiguity='',
                                                            lifetime='project')), request='old-condition')
        declared = proposal(TEXT, condition(value='室外', ambiguity=''))
    else:
        declared = proposal()
    args = ({'intent': 'edit', 'format': 'video', 'deliverable': 'production_input',
             'production_kind': 'image', 'task_interpretation': declared}
            if entry == 'prepare_creative_context' else declared)
    seen = mock_http(monkeypatch, lambda _: reply(
        (entry, args),
        ('prepare_production', {**payload, 'request_id': 'must-not-save', 'shot_id': drafts[1]['shot_id'],
                                'supersedes': drafts[1]['draft_id']}),
        ('dispatch', {'stage': 'writing', 'brief': TEXT}),
        ('observe_images', {'images': [{'name': 'must-not-send.png', 'purpose': 'product'}]})))
    executed = []
    original = Orchestrator._execute
    def traced(self, name, *args):
        executed.append(name)
        return original(self, name, *args)
    monkeypatch.setattr(Orchestrator, '_execute', traced)
    model = OpenAICompatClient('https://example.invalid/v1', 'synthetic-only')
    with TestClient(build_app(settings, client_factory=lambda: model)) as http:
        response = http.post(f'/api/runs/{RUN}/jobs', json={
            'text': TEXT, 'request_id': 'first-stop', 'feedback_target': target})
        assert response.status_code == 200, response.text
        job = wait_job(http, response.json()['job_id'])
    assert job['status'] == 'blocked', job
    delivery = job['deliverable']
    assert delivery['status'] == 'incomplete'
    blocker = delivery['blocker']
    assert blocker['condition_status'] == 'needs_clarification'
    assert blocker['source_message_id'] == job['user_input_id']
    assert job['reason'] == blocker['note']
    if reason == 'ambiguity':
        assert QUESTION in job['reason']
        assert blocker['conditions'][0]['conflict_with'] == []
    else:
        assert '室内' in job['reason'] and '存在冲突' in job['reason']
        assert blocker['conditions'][0]['conflict_with'] == [old['constraints'][0]['id']]
        assert blocker['conditions'][0]['conflicts'][0]['value'] == '室内'
    assert '保留还是替换' not in job['reason']
    assert len(seen) == 1
    assert executed == ([entry, 'propose_task_interpretation'] if entry == 'prepare_creative_context' else [entry])
    log = [event['event'] for event in store.read_events(RUN)
           if event['kind'] == 'job_progress' and event['job_id'] == job['job_id']
           and event['event'].get('type') == 'agent_tool']
    assert log[0]['ok'] and all(row.get('skipped') for row in log[1:])
    done = next(event['event'] for event in store.read_events(RUN)
                if event['kind'] == 'job_progress' and event['job_id'] == job['job_id']
                and event['event'].get('type') == 'done')
    assert done['rounds'] == 1 and done['stopped_because'] == 'done'
    assert_no_content_changes(store, before, before_drafts)
    later = store.read_events(RUN)
    assert not any(e['kind'] in {'execution_authorized', 'agent_task_prepared', 'agent_images_observed'} for e in later)
    # Only the actual completed mock model usage is charged and snapshotted.
    entries = Ledger(store, settings).entries(RUN)
    assert len(entries) == 1 and (entries[0].input_tokens, entries[0].output_tokens) == (31, 17)
    assert len([e for e in later if e['kind'] == 'agent_request_snapshot'
                and e['call_id'] == entries[0].call_id]) == 1


@pytest.mark.parametrize('name', ['observe_images', 'observe_reference', 'observe_product_video'])
def test_combined_observation_returns_terminal_interpretation_before_any_observer(env, name):
    store, pipe, settings, ledger = env
    called = []
    def observer(*args, **kwargs):
        called.append(name)
        raise AssertionError('An unresolved creative input must not call a paid observer')
    model = ScriptedModel([final_turn('Synthetic setup only')], legacy_task_contracts=False)
    main = Orchestrator(store, pipe, settings, model, ledger, RUN,
                        image_observer=observer, reference_observer=observer,
                        product_video_observer=observer)
    main.send(TEXT)
    args = {'task_interpretation': proposal(), 'quote': TEXT,
            'source': 'https://www.tiktok.com/@synthetic/video/123456',
            'action': 'inspiration', 'scope': 'opening',
            'images': [{'name': 'synthetic-missing.png', 'purpose': 'product'}]}
    outcome = main._execute(name, args, Workspace(store, pipe, RUN, 'orchestrator'))
    assert isinstance(outcome, ToolResult) and outcome.concludes_turn
    assert json.loads(outcome.content)['blocker']['condition_status'] == 'needs_clarification'
    assert not called and len(model.calls) == 1
    assert main._condition_blocker()['source_message_id'] == main._current_user_input['message_id']


def test_same_bound_receipt_stays_immutable_and_no_model_is_replayed(env):
    store, pipe, settings, ledger = env
    _, drafts = image_inputs(env)
    target = selected_target(drafts[1])
    receipt, saved = persist(store, proposal(), target=target)
    original = copy.deepcopy(saved)
    model = ScriptedModel([], legacy_task_contracts=False)
    turn = Orchestrator(store, pipe, settings, model, ledger, RUN).send(
        TEXT, user_input_id=receipt['message_id'])
    assert turn.rounds == 0 and not model.calls
    assert turn.deliverable['status'] == 'incomplete' and QUESTION in turn.text
    assert interpretation_for(store, RUN, receipt['message_id']) == {k: v for k, v in original.items() if k != 'project_constraints'}
    assert len([e for e in store.read_events(RUN) if e['kind'] == 'user_task_interpretation_recorded']) == 1


def test_unresolved_revision_stops_before_target_selection_without_granting_a_stage(env):
    store, pipe, settings, ledger = env
    receipt, saved = persist(store, proposal(goal='revision'))
    before = {stage: store.latest_artifact(RUN, stage) for stage in STAGES}
    model = ScriptedModel([], legacy_task_contracts=False)
    main = Orchestrator(store, pipe, settings, model, ledger, RUN)
    turn = main.send(TEXT, user_input_id=receipt['message_id'])
    assert turn.rounds == 0 and not model.calls
    assert turn.deliverable['status'] == 'incomplete' and QUESTION in turn.text
    assert main._task_constraints['allowed_stages'] == [] and main._revision_scope is None
    assert {stage: store.latest_artifact(RUN, stage) for stage in STAGES} == before
    assert not any(e['kind'] == 'agent_revision_planned' for e in store.read_events(RUN))


def test_unresolved_revision_cannot_expand_saved_empty_stage_scope(env):
    store, _, _, _ = env
    declared = proposal(goal='revision')
    receipt, saved = persist(store, declared)
    changed = copy.deepcopy(declared)
    changed['task']['allowed_stages'] = ['writing']
    before = store.read_events(RUN)
    with pytest.raises(PipelineError):
        record_task_interpretation(store, RUN, receipt, changed)
    assert store.read_events(RUN) == before
    assert interpretation_for(store, RUN, receipt['message_id'])['task']['allowed_stages'] == []
    assert 'edit_scopes' not in saved


def test_empty_stage_revision_without_an_unresolved_question_still_requires_scope(env):
    store, _, _, _ = env
    declared = proposal(TEXT, condition(ambiguity=''), goal='revision')
    receipt = receive(store, RUN, TEXT, 'missing-edit-scope')
    accept(store, RUN, receipt)
    start_execution(store, RUN, receipt)
    before = store.read_events(RUN)
    with pytest.raises(PipelineError, match='尚未绑定编辑范围'):
        record_task_interpretation(store, RUN, receipt, declared)
    assert store.read_events(RUN) == before


@pytest.mark.parametrize('unrelated', ['stage', 'question', 'target_stage', 'reply'])
def test_unrelated_ambiguity_cannot_replace_the_current_edit_scope(env, unrelated):
    store, _, _, _ = env
    text = TEXT
    row = condition(intent='question') if unrelated == 'question' else condition(stages=['intake'])
    if unrelated == 'reply':
        text = '只改第二句，其余不变；这次回复用表格还是文字还没确定。'
        row = condition(text, key='reply.format', value='text', ambiguity='回复格式尚未确定')
        row['scope']['stages'] = []
    declared = proposal(text, row, goal='revision' if unrelated == 'target_stage' else 'writing')
    receipt = receive(store, RUN, text, 'unrelated-ambiguity',
                      feedback_target={'stage': 'writing'} if unrelated == 'target_stage' else None)
    accept(store, RUN, receipt)
    start_execution(store, RUN, receipt)
    before = store.read_events(RUN)
    with pytest.raises(PipelineError, match='尚未绑定编辑范围'):
        record_task_interpretation(store, RUN, receipt, declared)
    assert store.read_events(RUN) == before


@pytest.mark.parametrize('mode,text', [('discuss', '讨论当前图片的背景问题'), ('status', '查看当前进度')])
def test_old_unresolved_project_condition_does_not_stop_new_readonly_input(env, mode, text):
    store, pipe, settings, ledger = env
    persist(store, proposal('整个项目需要确定背景', condition('整个项目需要确定背景', lifetime='project')))
    before = len(store.read_events(RUN))
    declared = proposal(text, mode=mode, goal='follow_up')
    declared['constraints'] = []
    model = ScriptedModel([
        tool_turn('interpret-new', 'propose_task_interpretation', declared),
        final_turn('当前仅讨论或读取状态，没有修改作品。')], legacy_task_contracts=False)
    turn = Orchestrator(store, pipe, settings, model, ledger, RUN).send(text)
    assert len(model.calls) == 2
    assert 'blocker' not in turn.deliverable and turn.deliverable['status'] in {'informational', 'unspecified'}
    assert not any(e['kind'] in {'production_draft', 'artifact_written', 'execution_authorized'}
                   for e in store.read_events(RUN)[before:])


def test_scope_filters_content_and_does_not_grant_writing_permissions(env):
    store, _, _, _ = env
    receipt, saved = persist(store, proposal(TEXT, condition(paths=[['segments', 1, 'picture']])))
    original = copy.deepcopy(saved)
    assert projection(store, receipt, saved, stage='writing', paths=[['segments', 1, 'picture']])
    assert projection(store, receipt, saved, stage='writing', paths=[['segments', 0, 'line']]) is None
    assert projection(store, receipt, saved, stage='intake') is None
    assert saved == original
    assert interpreted_task_constraints(receipt, saved)['allowed_stages'] == []


@pytest.mark.parametrize('change', ['source', 'hash', 'origin', 'intent', 'active', 'unbound', 'readonly'])
def test_untrusted_or_non_instruction_rows_cannot_create_a_current_blocker(env, change):
    store, _, _, _ = env
    receipt, saved = persist(store, proposal())
    limits = interpreted_task_constraints(receipt, saved)
    rows = copy.deepcopy(saved['project_constraints'])
    if change in {'source', 'hash', 'origin', 'intent', 'active'}:
        key, value = {'source': ('source_message_id', 'other-source'),
                      'hash': ('source_sha256', '0' * 64), 'origin': ('origin', 'unverified'),
                      'intent': ('intent', 'hypothesis'), 'active': ('status', 'active')}[change]
        rows[0][key] = value
    elif change == 'unbound':
        limits.pop('interpretation_id')
    else:
        limits['readonly'] = True
    assert for_current_input(receipt, limits, rows) is None


def test_user_stop_wins_when_condition_is_bound_before_final_publication(env, monkeypatch):
    store, pipe, settings, ledger = env
    stopped = Event()
    original = Orchestrator._execute
    def bind_then_stop(self, name, *args):
        outcome = original(self, name, *args)
        if name == 'propose_task_interpretation':
            stopped.set()
        return outcome
    monkeypatch.setattr(Orchestrator, '_execute', bind_then_stop)
    def check():
        if stopped.is_set():
            raise JobCancelled('Synthetic user stop')
    model = ScriptedModel([tool_turn('bind', 'propose_task_interpretation', proposal())],
                          legacy_task_contracts=False)
    main = Orchestrator(store, pipe, settings, model, ledger, RUN, check_cancel=check)
    with pytest.raises(JobCancelled, match='user stop'):
        main.send(TEXT)
    assert len(model.calls) == 1
    assert interpretation_for(store, RUN, main._current_user_input['message_id'])
    assert not any(row['role'] == 'assistant' for row in store.read_chat(RUN))
