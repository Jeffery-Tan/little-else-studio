"""Owned direction candidates conclude through Mock HTTP, without real models."""
import copy
import json
from dataclasses import replace
from threading import Event

import httpx
import pytest

from studio.agent_runtime import AgentRuntime, bind_task, reset_task
from studio.agents.client import JobCancelled, ModelError, NonRetryableStageError
from studio.agents.deepseek_client import DeepSeekClient
from studio.agents.orchestrator import Orchestrator
from studio.config import Settings
from studio.conversation_input import display_context, receive
from studio.interactions import ask
from studio.ledger import Ledger
from studio.pipeline import Pipeline
from studio.store import Store
from studio.tools.registry import STRICT_ABSENT
from test_finishing_phase import mock_http, reply


TEXT = '只用已有商品信息给我三个图文创意方向，先不写完整页稿，也不生成图片或视频。'
ANSWER = '三个方向候选已保存。供应信息仍有未知项，选项差异和待确认条件请先核对；推荐不代表采用。'
OWNER = 'direction-finish-job'
RUN = 'direction-finishing-fixture'


def submission():
    return {'content': '', 'summary': 'Three synthetic direction candidates',
        'evidence': ['fixture'], 'limitations': ['Synthetic supplier facts are incomplete.'],
        'structured': {'options': [
            {'key': key, 'title': f'Synthetic direction {key}', 'claim': f'Candidate {key}',
             'hook': f'Opening {key}', 'story': f'Concept {key}', 'satisfies': 'goal.primary'}
            for key in ('A', 'B', 'C')],
            'recommended': 'A', 'recommended_because': 'Synthetic comparison only.',
            'prerequisites': ['Verify the unknown supplier information before production.'],
            'open_questions': [{'question': 'Synthetic material composition remains unknown.',
                'blocks': '', 'owner': 'user', 'timing': 'now',
                'reason': 'Only supplier evidence can settle this fact.', 'kind': 'fact'}]}}


def harness(tmp_path, monkeypatch, *, dispatch_round=11, finish=None, before_finish=None,
            check_cancel=None, independent=False, existing=False):
    store = Store(tmp_path)
    store.create_run(RUN, 'Synthetic direction candidates', TEXT)
    pipeline = Pipeline(store)
    initial = pipeline.start_attempt(RUN, 'intake')
    pipeline.submit(RUN, 'intake', initial.token, content='', summary='Synthetic facts',
        structured={'facts': [{'label': 'Product', 'value': 'Grey lunch bag', 'source': 'fixture'}]},
        limitations=['Material composition unknown.'])
    if existing:
        attempt = pipeline.start_attempt(RUN, 'direction')
        pipeline.submit(RUN, 'direction', attempt.token, **submission())
    monkeypatch.setattr('studio.learning.LearningService.prepare_context', lambda *a, **k: {
        'text': 'FIXTURE_METHOD', 'snapshot_sha256': 'f' * 64, 'evidence_ref': 'fixture-method'})
    settings = replace(Settings(tmp_path, 'https://example.invalid'), max_tool_rounds=12)
    ledger = Ledger(store, settings)
    display_id = display_context(store, pipeline, RUN,
        {'pending': None, 'questions': [], 'interaction': None})
    receipt = receive(store, RUN, TEXT, 'synthetic-direction-request', display_id)
    runtime = AgentRuntime(store, RUN)
    runtime.create_task(OWNER, role='orchestrator', parent_job_id=OWNER,
                        source_message_id=receipt['message_id'])
    runtime.transition(OWNER, 'running')
    activity, contexts, work = [], [], []
    release, started = Event(), Event()
    orch = Orchestrator(store, pipeline, settings,
        DeepSeekClient('synthetic-only', reasoning_effort='low', progress_stream=False,
                       check_cancel=check_cancel), ledger, RUN,
        on_event=activity.append, check_cancel=check_cancel)
    original_context, original_send = orch._finishing_context, orch.send

    def context():
        value = original_context()
        if value is not None:
            contexts.append(value)
        return value

    def send():
        token = bind_task(OWNER)
        try:
            return original_send(TEXT, user_input_id=receipt['message_id'])
        finally:
            reset_task(token)

    orch._finishing_context = context

    def handle(payload):
        tools = {item['function']['name'] for item in payload.get('tools', [])}
        if 'submit_delivery_check' in tools:
            return reply(('submit_delivery_check', {'verdict': 'ready',
                'summary': 'Synthetic assessment only, no external quality claim.', 'findings': []}))
        if 'submit_stage' in tools:
            if independent:
                started.set()
                assert release.wait(10), 'The test must release its own synthetic director'
            return reply(('submit_stage', submission()))
        if not tools:
            if before_finish:
                before_finish(orch)
            return finish if finish is not None else reply(text=ANSWER)
        work.append(payload)
        if len(work) == 1:
            proposal = {'task': {'quote': TEXT, 'mode': 'produce', 'deliverable': 'direction',
                'format': 'photo', 'allowed_stages': ['intake', 'direction'], 'pause_after': True},
                'constraints': []}
            declared = next(t['function'] for t in payload['tools']
                            if t['function']['name'] == 'propose_task_interpretation')
            if declared.get('strict'):
                proposal.update(reuse_artifact=STRICT_ABSENT, video_use=STRICT_ABSENT,
                                image_use=STRICT_ABSENT)
            return reply(('propose_task_interpretation', proposal))
        if len(work) == 2:
            return reply(('prepare_creative_context', {'intent': 'new', 'format': 'photo',
                'deliverable': 'direction', 'creation_mode': 'autonomous'}))
        if len(work) == dispatch_round:
            return reply(('start_role' if independent else 'dispatch',
                          {'stage': 'direction', 'brief': TEXT}))
        assert len(work) <= 12, 'Do not create another tool work round'
        return reply(('read_capabilities', {}))

    seen = mock_http(monkeypatch, handle)
    return orch, send, store, pipeline, ledger, seen, work, contexts, activity, release, started


@pytest.mark.parametrize('dispatch_round', [3, 11, 12])
def test_exact_three_direction_candidates_get_one_conclusion_without_last_artifact_read(
        tmp_path, monkeypatch, dispatch_round):
    orch, send, store, pipeline, ledger, seen, work, contexts, activity, *_ = harness(
        tmp_path, monkeypatch, dispatch_round=dispatch_round)
    before_approvals = store.list_approvals(RUN)
    before_intake = store.latest_artifact(RUN, 'intake')
    result = send()
    assert result.stopped_because == 'done' and result.rounds == dispatch_round + 2
    assert result.text == ANSWER and result.deliverable['status'] == 'complete'
    assert len(work) == dispatch_round and len(seen) == dispatch_round + 3 and len(contexts) == 1
    assert not seen[-1].get('tools') and seen[-1]['messages'][0]['content'].find('phase=finishing') >= 0
    assert not any(row['tool'] in {'read_artifact', 'observe_images'} for row in result.tool_log)
    artifact = store.latest_artifact(RUN, 'direction')
    data = json.loads(contexts[0].messages[0]['content'])
    assert data['current_direction']['structured'] == artifact.structured
    assert data['current_direction']['sha256'] == artifact.sha256
    assert len(data['current_direction']['structured']['options']) == 3
    assert data['current_direction']['structured']['open_questions'] == submission()['structured']['open_questions']
    assert data['user_text'] == TEXT and data['source_message_id'] == orch._task_user_input['message_id']
    assert data['task_constraints']['deliverable'] == 'direction'
    assert data['quality_pass_implied'] is False and data['image_review_implied'] is False
    assert data['direction_adoption_implied'] is False and data['needs_user_choice'] is True
    assert '推荐不是采用' in contexts[0].system and '不能代答或关闭' in contexts[0].system
    assert '不得宣称重新看过图片或图审通过' in contexts[0].system
    assert store.list_approvals(RUN) == before_approvals and not pipeline.is_complete(RUN, 'direction')
    assert store.latest_artifact(RUN, 'intake') == before_intake
    assert store.list_reviews(RUN, 'direction') == []
    assert not any(event['kind'] in {'agent_images_observed', 'production_attempt', 'user_decision_recorded',
        'interaction_answered', 'execution_authorized'} for event in store.read_events(RUN))
    assert len(ledger.entries(RUN)) == len(seen) and ledger.entries(RUN)[-1].stage == 'direction'
    assert any(e.get('phase') == 'finishing' and e.get('stage') == 'direction' for e in activity)


def test_old_direction_cannot_finish_a_new_request_or_extend_work_budget(tmp_path, monkeypatch):
    _, send, store, _, ledger, seen, work, contexts, *_ = harness(
        tmp_path, monkeypatch, dispatch_round=99, existing=True)
    before = store.latest_artifact(RUN, 'direction')
    result = send()
    assert result.stopped_because == 'max_rounds' and result.rounds == 12
    assert len(work) == len(seen) == len(ledger.entries(RUN)) == 12 and contexts == []
    assert store.latest_artifact(RUN, 'direction') == before


@pytest.mark.parametrize('condition', ['missing_receipt', 'wrong_stage', 'wrong_role', 'wrong_attempt',
    'wrong_hash', 'wrong_version', 'other_source', 'other_parent', 'queued', 'running', 'waiting',
    'cancelled', 'stopped', 'old_baseline', 'decision', 'not_complete', 'source', 'interaction',
    'interpretation', 'scope_missing', 'wrong_mode'])
def test_direction_context_requires_its_own_settled_new_receipt(tmp_path, monkeypatch, condition):
    orch, send, store, _, _, _, _, _, *_ = harness(tmp_path, monkeypatch, dispatch_round=3)
    send()
    rows = copy.deepcopy(orch.dispatcher.sessions())
    row = rows[0]
    if condition == 'missing_receipt': row['artifact_receipt'] = None
    elif condition == 'wrong_stage': row['stage'] = 'writing'
    elif condition == 'wrong_role': row['role'] = 'writer'
    elif condition == 'wrong_attempt': row['task_id'] = 'other-attempt'
    elif condition == 'wrong_hash': row['artifact_receipt']['sha256'] = '0' * 64
    elif condition == 'wrong_version': row['artifact_receipt']['version'] = 99
    elif condition == 'other_source': row['source_message_id'] = 'other-message'
    elif condition == 'other_parent': row['parent_job_id'] = 'other-job'
    elif condition in {'queued', 'running', 'waiting', 'cancelled'}: row['status'] = condition
    elif condition == 'stopped': row['stop_requested'] = True
    elif condition == 'old_baseline': orch._artifact_baseline['direction'] = {'version': 1}
    elif condition == 'decision': orch._decision_recorded = {'action': 'adopt'}
    elif condition == 'not_complete': monkeypatch.setattr(orch, '_deliverable_status', lambda: {'status': 'incomplete'})
    elif condition == 'source': orch._task_user_input = {**orch._task_user_input, 'text': 'Synthetic forged source'}
    elif condition == 'interpretation': orch._task_constraints['interpretation_id'] = 'other-interpretation'
    elif condition == 'scope_missing': orch._scope_selected = False
    elif condition == 'wrong_mode': orch._task_constraints['task_mode'] = 'revise'
    elif condition == 'interaction': ask(store, RUN, {'question': 'Synthetic fact still needed',
        'options': [{'label': 'Unknown', 'description': ''}, {'label': 'Known', 'description': ''}]})
    monkeypatch.setattr(orch.dispatcher, 'sessions', lambda: rows)
    token = bind_task(OWNER)
    try:
        assert orch._finishing_context() is None
    finally:
        reset_task(token)


@pytest.mark.parametrize('change', ['source', 'direction', 'intake', 'stop', 'parent_stop',
    'child_stop', 'receipt', 'interaction', 'adopt'])
def test_late_source_stop_or_reply_change_is_not_published_or_retried(tmp_path, monkeypatch, change):
    stopped = []
    def cancel():
        if stopped:
            raise JobCancelled('Synthetic stop')
    def before(orch):
        if change == 'source':
            orch._task_user_input = {**orch._task_user_input, 'message_id': 'other-source'}
        elif change == 'stop':
            stopped.append(True)
        elif change == 'parent_stop':
            AgentRuntime(orch.store, RUN).request_stop(OWNER)
        elif change in {'child_stop', 'receipt'}:
            rows = copy.deepcopy(orch.dispatcher.sessions())
            if change == 'child_stop': rows[0]['stop_requested'] = True
            else: rows[0]['artifact_receipt']['sha256'] = '0' * 64
            monkeypatch.setattr(orch.dispatcher, 'sessions', lambda: rows)
        elif change == 'interaction':
            ask(orch.store, RUN, {'question': 'Synthetic unanswered fact',
                'options': [{'label': 'Unknown', 'description': ''}, {'label': 'Known', 'description': ''}]})
        elif change == 'adopt':
            orch.pipeline.record_approval(RUN, 'direction', nonce=orch.pipeline.issue_nonce(RUN, 'direction'),
                                          decision='approved', selected_option_id='A')
        else:
            artifact = orch.store.latest_artifact(RUN, change)
            attempt = orch.pipeline.start_attempt(RUN, change)
            orch.pipeline.submit(RUN, change, attempt.token, content='', summary='Synthetic changed version',
                                 structured=artifact.structured)
    _, send, store, _, ledger, seen, work, contexts, *_ = harness(
        tmp_path, monkeypatch, dispatch_round=3, before_finish=before, check_cancel=cancel)
    with pytest.raises(JobCancelled if change in {'stop', 'parent_stop', 'child_stop'} else NonRetryableStageError):
        send()
    assert len(seen) == len(ledger.entries(RUN)) == 6 and len(work) == 3 and len(contexts) == 1
    assistants = [row['text'] for row in store.read_chat(RUN) if row['role'] == 'assistant']
    if change == 'interaction':
        assert len(assistants) == 1 and 'Synthetic unanswered fact' in assistants[0]
        assert assistants[0] != ANSWER  # The real question remains; the stale conclusion does not.
    else:
        assert assistants == []


@pytest.mark.parametrize('failure', ['empty', 'tool', 'truncated', 'invalid', 'http'])
def test_direction_finishing_failure_retains_candidate_and_usage_without_extra_request(tmp_path, monkeypatch, failure):
    final = {'empty': reply(), 'tool': reply(('read_capabilities', {})),
        'truncated': reply(text='Synthetic incomplete', finish='length'),
        'invalid': {'usage': {'prompt_tokens': 31, 'completion_tokens': 17}, 'choices': []},
        'http': httpx.Response(500, text='PRIVATE_SYNTHETIC_PROVIDER_ERROR')}[failure]
    _, send, store, pipeline, ledger, seen, work, contexts, *_ = harness(
        tmp_path, monkeypatch, dispatch_round=3, finish=final)
    with pytest.raises(ModelError):
        send()
    assert len(seen) == len(ledger.entries(RUN)) == 6 and len(work) == 3 and len(contexts) == 1
    assert store.latest_artifact(RUN, 'direction').version == 1
    assert not pipeline.is_complete(RUN, 'direction') and not store.list_reviews(RUN, 'direction')
    assert not any(row['role'] == 'assistant' for row in store.read_chat(RUN))
    assert 'PRIVATE_SYNTHETIC_PROVIDER_ERROR' not in str(store.read_events(RUN))


def test_independent_director_finishes_once_after_original_work_cap(tmp_path, monkeypatch):
    orch, send, store, _, ledger, seen, work, contexts, _, release, started = harness(
        tmp_path, monkeypatch, dispatch_round=3, independent=True)
    original_wait = orch._wait_owned_roles
    def wait():
        assert len(work) == 12 and not contexts and started.wait(2)
        assert store.latest_artifact(RUN, 'direction') is None
        release.set()
        original_wait()
    monkeypatch.setattr(orch, '_wait_owned_roles', wait)
    result = send()
    assert result.stopped_because == 'done' and result.rounds == 14
    assert len(work) == 12 and len(contexts) == 1 and len(seen) == len(ledger.entries(RUN)) == 15
    assert len([p for p in seen if not p.get('tools')]) == 1


def test_stop_after_reply_before_publication_keeps_candidate_without_assistant_chat(tmp_path, monkeypatch):
    stopped = []
    def cancel():
        if stopped:
            raise JobCancelled('Synthetic stop before publish')
    _, send, store, _, ledger, seen, _, _, *_ = harness(
        tmp_path, monkeypatch, dispatch_round=3, check_cancel=cancel)
    from studio.agents.orchestrator import creator_reply
    def late_stop(*args, **kwargs):
        text = creator_reply(*args, **kwargs)
        stopped.append(True)
        return text
    monkeypatch.setattr('studio.agents.orchestrator.creator_reply', late_stop)
    with pytest.raises(JobCancelled):
        send()
    assert store.latest_artifact(RUN, 'direction').version == 1
    assert len(seen) == len(ledger.entries(RUN)) == 6
    assert not any(row['role'] == 'assistant' for row in store.read_chat(RUN))
