"""One real Mock HTTP conclusion after an exact script; no paid network calls."""
import copy
import json
from dataclasses import replace
from threading import Event

import httpx
import pytest

from studio.agents.budget import BudgetedClient
from studio.agents.client import (FinishingContext, JobCancelled, ModelError,
                                 NonRetryableStageError, run_agent_loop)
from studio.agents.deepseek_client import DeepSeekClient
from studio.agents.orchestrator import Orchestrator
from studio.agent_runtime import AgentRuntime, bind_task, reset_task
from studio.conversation_input import receive
from studio.ledger import Ledger
from studio.tools.execution import ToolResult
from test_writing_tool_schema import seed, submission, wire
from test_revision_scope import revision_meaning


TEXT = 'Write a complete video script using the adopted direction A'
LOCAL = '只把第一句改得自然一点，其他内容保留'
ANSWER = '草稿已保存。第二镜动作是否能在三秒内完成仍需复核，当前未采用或生成媒体。'


def reply(*calls, text='', finish=None):
    message = {'content': text, 'reasoning_content': 'SYNTHETIC_PRIVATE_REASONING'}
    if calls:
        message['tool_calls'] = [{'id': f't-{index}', 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}
            for index, (name, args) in enumerate(calls)]
    return {'model': 'deepseek-flash', 'usage': {'prompt_tokens': 31, 'completion_tokens': 17},
            'choices': [{'finish_reason': finish or ('tool_calls' if calls else 'stop'),
                         'message': message}]}


def mock_http(monkeypatch, handle):
    original = httpx.AsyncClient
    seen = []
    def wrapped(request):
        payload = json.loads(request.content)
        seen.append(payload)
        value = handle(payload)
        return value if isinstance(value, httpx.Response) else httpx.Response(200, json=value)
    def factory(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(wrapped))
    monkeypatch.setattr(httpx, 'AsyncClient', factory)
    return seen


def harness(tmp_path, monkeypatch, *, dispatch_round=2, finish=None, check_cancel=None,
            before_finish=None, local=False, independent=False, legacy_intake=False, source_text=None,
            reply_rows=()):
    store, pipe, settings, ledger, run_id = seed(tmp_path, monkeypatch)
    if legacy_intake:
        saved = {stage: store.latest_artifact(run_id, stage) for stage in ('direction', 'material_plan')}
        attempt = pipe.start_attempt(run_id, 'intake')
        pipe.submit(run_id, 'intake', attempt.token, content='Legacy actual facts: grey bag for lunch.',
                    summary='Legacy brief', structured={'constraints': ['Keep supplied appearance']})
        for stage, artifact in saved.items():
            attempt = pipe.start_attempt(run_id, stage)
            pipe.submit(run_id, stage, attempt.token, content='', summary=stage, structured=artifact.structured)
            if stage == 'direction':
                pipe.record_approval(run_id, stage, nonce=pipe.issue_nonce(run_id, stage),
                                     decision='approved', selected_option_id='A')
    settings = replace(settings, max_tool_rounds=12)
    ledger = Ledger(store, settings)
    message = source_text if source_text is not None else LOCAL if local else TEXT
    if local:
        attempt = pipe.start_attempt(run_id, 'writing')
        pipe.submit(run_id, 'writing', attempt.token, **submission())
        pipe.record_review(run_id, 'writing', verdict='pass', findings=[])
        pipe.record_approval(run_id, 'writing', nonce=pipe.issue_nonce(run_id, 'writing'), decision='approved')
    receipt = receive(store, run_id, message, 'synthetic-message')
    runtime = AgentRuntime(store, run_id)
    runtime.create_task('finish-job', role='orchestrator', parent_job_id='finish-job',
                        source_message_id=receipt['message_id'])
    runtime.transition('finish-job', 'running')
    activity = []
    orch = Orchestrator(store, pipe, settings,
        DeepSeekClient('synthetic-only', reasoning_effort='low', progress_stream=False,
                       check_cancel=check_cancel), ledger, run_id,
        on_event=activity.append, check_cancel=check_cancel)
    captured, work = [], []
    release_role, started_role = Event(), Event()
    orch._test_release_role, orch._test_started_role = release_role, started_role
    original_context = orch._finishing_context
    def context():
        token = bind_task('finish-job')
        try:
            value = original_context()
            if value is not None:
                captured.append(value)
            return value
        finally:
            reset_task(token)
    orch._finishing_context = context
    original_send = orch.send
    def send(text, **kwargs):
        token = bind_task('finish-job')
        try:
            return original_send(text, user_input_id=receipt['message_id'], **kwargs)
        finally:
            reset_task(token)
    orch.send = send
    def handle(payload):
        tools = {item['function']['name'] for item in payload.get('tools', [])}
        if 'submit_stage' in tools:
            if independent:
                started_role.set()
                assert release_role.wait(10), 'Synthetic worker must be released by its owning test'
            return wire(submission())
        if 'submit_patch' in tools and 'dispatch' not in tools:
            return reply(('submit_patch', {'changes': [{'path': ['segments', 0, 'line'],
                                                       'value': 'Pack a little room for your day.'}],
                                           'summary': 'One precise line changed'}))
        if not tools:
            if before_finish:
                before_finish(orch)
            return finish if finish is not None else reply(text=ANSWER)
        work.append(payload)
        if len(work) == 1:
            proposal = {'task': {'quote': message, 'mode': 'revise' if local else 'produce',
                'deliverable': 'revision' if local else 'writing',
                'format': 'video', 'allowed_stages': ['writing'], 'pause_after': True},
                'constraints': copy.deepcopy(list(reply_rows))}
            if local:
                proposal['constraints'].append(revision_meaning(message)['constraints'][0])
            return reply(('propose_task_interpretation', proposal),
                ('prepare_creative_context', {'intent': 'edit' if local else 'continue', 'format': 'video',
                                              'deliverable': 'revision' if local else 'writing'}))
        if local and len(work) == 2:
            base = store.latest_artifact(run_id, 'writing')
            return reply(('plan_revision', {'stage': 'writing', 'version': base.version, 'sha256': base.sha256,
                'mode': 'local', 'quote': LOCAL, 'reason': 'Keep all other content',
                'fields': [{'path': ['segments', 0, 'line'], 'instruction': 'Make this line conversational'}]}))
        if len(work) == dispatch_round:
            scope = ({'plan_id': next(e['scope']['plan_id'] for e in reversed(store.read_events(run_id))
                                     if e['kind'] == 'agent_revision_planned')} if local else {})
            return reply(('start_role' if independent else 'dispatch',
                          {'stage': 'writing', 'brief': message, **scope}))
        assert len(work) <= 12, 'finishing must not add another work call'
        return reply(('read_capabilities', {}))
    seen = mock_http(monkeypatch, handle)
    return orch, store, pipe, ledger, run_id, seen, work, captured, activity


@pytest.mark.parametrize('dispatch_round', [2, 12])
def test_exact_saved_script_gets_one_tool_free_http_conclusion_even_on_round_twelve(
        tmp_path, monkeypatch, dispatch_round):
    orch, store, pipe, ledger, run, seen, work, contexts, activity = harness(
        tmp_path, monkeypatch, dispatch_round=dispatch_round)
    approvals = store.list_approvals(run)
    upstream = {s: store.latest_artifact(run, s) for s in ('intake', 'direction', 'material_plan')}
    result = orch.send(TEXT)
    assert result.rounds == dispatch_round + 1 and result.stopped_because == 'done'
    assert result.text == ANSWER and result.deliverable['status'] == 'complete'
    assert len(work) == dispatch_round and len(contexts) == 1
    assert len(seen) == dispatch_round + 2  # work + writer + one finish
    final = seen[-1]
    assert not final.get('tools')
    assert 'phase=finishing' in final['messages'][0]['content']
    assert '不代表内容质量' in final['messages'][0]['content']
    system = final['messages'][0]['content']
    # Prompt assembly only: a synthetic fixed answer is not natural-model proof.
    assert '前 2–3 句话' in system and '最影响采用的已观察内容问题' in system
    assert '短段落补充必要依据' in system and '不把重点拆散成多个小节' in system
    assert '用户未明确索要技术细节时，不解释 complete、不列哈希、内部状态或字段路径' in system
    assert '只有本轮明确要求技术记录、字段路径或哈希时' in system
    assert '不堆砌低相关的假想警告' in system and '其他真实问题与未核实项仍如实保留' in system
    data = json.loads(final['messages'][-1]['content'])
    artifact = store.latest_artifact(run, 'writing')
    assert data['current_script']['structured'] == artifact.structured
    assert data['current_script']['sha256'] == artifact.sha256
    assert data['quality_pass_implied'] is False and data['user_adopted'] is False
    assert data['adopted_direction']['selected_option_id'] == 'A'
    assert data['source_message_id'] == orch._task_user_input['message_id'] and data['user_text'] == TEXT
    assert store.list_approvals(run) == approvals and not pipe.is_complete(run, 'writing')
    assert all(store.latest_artifact(run, s) == a for s, a in upstream.items())
    assert not any(e['kind'] in {'production_submitted', 'execution_authorized', 'user_decision_recorded'}
                   for e in store.read_events(run))
    entries = ledger.entries(run)
    assert len(entries) == len(seen) and entries[-1].role == 'orchestrator'
    assert entries[-1].stage == 'writing' and entries[-1].input_tokens == 31
    snapshots = list((store.run_dir(run) / 'requests').glob('*.json'))
    finishing = [json.loads(p.read_text('utf-8')) for p in snapshots
                 if 'phase=finishing' in p.read_text('utf-8')]
    assert len(finishing) == 1 and finishing[0]['request']['tools'] == []
    assert any(e.get('phase') == 'finishing' and e.get('stage') == 'writing' for e in activity)


@pytest.mark.parametrize('failure', ['empty', 'tool', 'truncated', 'invalid', 'http'])
def test_bad_finishing_keeps_script_and_actual_usage_without_continuation_or_tools(
        tmp_path, monkeypatch, failure):
    final = {'empty': reply(), 'tool': reply(('read_capabilities', {})),
             'truncated': reply(text='Incomplete', finish='length'),
             'invalid': {'usage': {'prompt_tokens': 31, 'completion_tokens': 17}, 'choices': []},
             'http': httpx.Response(500, text='PRIVATE_PROVIDER_ERROR')}[failure]
    orch, store, pipe, ledger, run, seen, work, contexts, _ = harness(
        tmp_path, monkeypatch, finish=final)
    approvals = store.list_approvals(run)
    with pytest.raises(ModelError):
        orch.send(TEXT)
    assert len(seen) == 4 and len(work) == 2 and len(contexts) == 1
    assert store.latest_artifact(run, 'writing').version == 1
    assert store.list_approvals(run) == approvals and not pipe.is_complete(run, 'writing')
    assert not any(row['role'] == 'assistant' for row in store.read_chat(run))
    entry = ledger.entries(run)[-1]
    assert entry.stage == 'writing'
    assert (entry.input_tokens, entry.output_tokens) == ((None, None) if failure == 'http' else (31, 17))
    assert 'PRIVATE_PROVIDER_ERROR' not in str(store.read_events(run))


def test_finishing_assembly_keeps_explicit_technical_request_and_exact_sources_available(tmp_path, monkeypatch):
    text = TEXT + '; please include the saved sha256 and changed field paths in the technical record.'
    orch, store, _, _, run, seen, _, contexts, _ = harness(tmp_path, monkeypatch, source_text=text)
    orch.send(text)
    data = json.loads(contexts[0].messages[0]['content'])
    assert data['user_text'] == text
    assert data['current_script']['sha256'] == store.latest_artifact(run, 'writing').sha256
    assert '只有本轮明确要求技术记录、字段路径或哈希时，才按要求给这些细节' in contexts[0].system
    assert not seen[-1].get('tools')


@pytest.mark.parametrize('change', ['source', 'script', 'upstream', 'cancel'])
def test_late_change_or_stop_discards_finishing_response_without_replay(tmp_path, monkeypatch, change):
    stopped = []
    def cancel():
        if stopped:
            raise JobCancelled('Stopped in synthetic test')
    def before(orch):
        if change == 'source':
            orch._task_user_input = {**orch._task_user_input, 'message_id': 'another-source'}
        elif change == 'cancel':
            stopped.append(True)
        else:
            stage = 'writing' if change == 'script' else 'intake'
            current = orch.store.latest_artifact(orch.run_id, stage)
            attempt = orch.pipeline.start_attempt(orch.run_id, stage)
            orch.pipeline.submit(orch.run_id, stage, attempt.token, content='', summary='Changed offline source',
                                 structured=current.structured)
    orch, store, _, ledger, run, seen, _, _, _ = harness(
        tmp_path, monkeypatch, check_cancel=cancel, before_finish=before)
    with pytest.raises(JobCancelled if change == 'cancel' else NonRetryableStageError):
        orch.send(TEXT)
    assert len(seen) == 4 and store.latest_artifact(run, 'writing') is not None
    assert not any(row['role'] == 'assistant' for row in store.read_chat(run))
    assert len(ledger.entries(run)) == 4


def test_cancel_after_http_before_chat_commit_keeps_usage_and_draft(tmp_path, monkeypatch):
    stopped = []
    def cancel():
        if stopped:
            raise JobCancelled('Stopped before publish')
    orch, store, _, ledger, run, seen, _, _, _ = harness(tmp_path, monkeypatch, check_cancel=cancel)
    from studio.agents.orchestrator import creator_reply
    def late_stop(*args, **kwargs):
        text = creator_reply(*args, **kwargs)
        stopped.append(True)
        return text
    monkeypatch.setattr('studio.agents.orchestrator.creator_reply', late_stop)
    with pytest.raises(JobCancelled):
        orch.send(TEXT)
    assert len(seen) == 4 and ledger.entries(run)[-1].input_tokens == 31
    assert store.latest_artifact(run, 'writing').version == 1
    assert not any(row['role'] == 'assistant' for row in store.read_chat(run))


@pytest.mark.parametrize('condition', ['missing_receipt', 'wrong_attempt', 'wrong_hash', 'wrong_version',
    'other_source', 'other_parent', 'queued', 'running', 'waiting', 'stopped', 'cancelled', 'decision', 'scope_missing',
    'revision_wrong_stage', 'not_complete', 'baseline_not_new', 'media_scope'])
def test_only_this_settled_accurate_new_script_may_enter_finishing(tmp_path, monkeypatch, condition):
    orch, store, _, _, run, _, _, _, _ = harness(tmp_path, monkeypatch)
    orch.send(TEXT)
    rows = copy.deepcopy(orch.dispatcher.sessions())
    assert len(rows) == 1 and rows[0]['status'] == 'completed'
    row = rows[0]
    if condition == 'missing_receipt': row['artifact_receipt'] = None
    elif condition == 'wrong_attempt': row['task_id'] = 'not-the-attempt'
    elif condition == 'wrong_hash': row['artifact_receipt']['sha256'] = '0' * 64
    elif condition == 'wrong_version': row['artifact_receipt']['version'] = 100
    elif condition == 'other_source': row['source_message_id'] = 'another-user'
    elif condition == 'other_parent': row['parent_job_id'] = 'another-job'
    elif condition in {'queued', 'running', 'waiting', 'cancelled'}: row['status'] = condition
    elif condition == 'stopped': row['stop_requested'] = True
    elif condition == 'decision': orch._decision_recorded = {'action': 'adopt'}
    elif condition == 'scope_missing': orch._scope_selected = False
    elif condition == 'revision_wrong_stage':
        orch._task_constraints['deliverable'] = 'revision'
        orch._revision_scope = {'stage': 'direction'}
    elif condition == 'not_complete': monkeypatch.setattr(orch, '_deliverable_status', lambda: {'status': 'missing'})
    elif condition == 'baseline_not_new': orch._artifact_baseline['writing'] = {'version': 1}
    elif condition == 'media_scope': orch._production_scope = {'kind': 'image'}
    monkeypatch.setattr(orch.dispatcher, 'sessions', lambda: rows)
    assert orch._finishing_context() is None


def test_no_accurate_draft_never_extends_twelve_work_rounds(tmp_path, monkeypatch):
    orch, store, _, ledger, run, seen, work, contexts, _ = harness(
        tmp_path, monkeypatch, dispatch_round=99)
    result = orch.send(TEXT)
    assert result.stopped_because == 'max_rounds' and result.rounds == 12
    assert len(work) == len(seen) == len(ledger.entries(run)) == 12 and contexts == []
    assert store.latest_artifact(run, 'writing') is None


def test_local_revision_finishing_reads_entire_old_and_new_scripts_and_actual_paths(tmp_path, monkeypatch):
    orch, store, pipe, ledger, run, seen, _, contexts, _ = harness(
        tmp_path, monkeypatch, local=True, dispatch_round=3)
    old = store.latest_artifact(run, 'writing')
    approvals = store.list_approvals(run)
    result = orch.send(LOCAL)
    assert result.stopped_because == 'done' and result.rounds == 4 and len(seen) == 5
    assert result.deliverable['status'] == 'complete' and len(contexts) == 1
    data = json.loads(contexts[0].messages[0]['content'])
    new = store.latest_artifact(run, 'writing')
    assert data['old_baseline']['structured'] == old.structured
    assert data['old_baseline']['sha256'] == old.sha256
    assert data['current_script']['structured'] == new.structured
    assert data['changed_paths'] == [['segments', 0, 'line']]
    expected = copy.deepcopy(old.structured)
    expected['segments'][0]['line'] = 'Pack a little room for your day.'
    assert new.structured == expected and new.version == 2
    assert store.list_approvals(run) == approvals and not pipe.is_complete(run, 'writing')
    assert ledger.entries(run)[-1].stage == 'writing'
    # The old draft is a separate immutable source, checked again after HTTP.
    (store.run_dir(run) / 'artifacts' / old.filename).write_text('Tampered offline fixture', encoding='utf-8')
    token = bind_task('finish-job')
    try:
        with pytest.raises(NonRetryableStageError, match='旧稿基线'):
            contexts[0].check_current()
    finally:
        reset_task(token)


def test_finishing_preserves_legacy_upstream_prose_when_partial_structure_has_no_core(tmp_path, monkeypatch):
    orch, store, _, _, run, _, _, contexts, _ = harness(tmp_path, monkeypatch, legacy_intake=True)
    result = orch.send(TEXT)
    assert result.stopped_because == 'done' and len(contexts) == 1
    data = json.loads(contexts[0].messages[0]['content'])
    intake = next(item for item in data['upstreams'] if item['stage'] == 'intake')
    assert intake['body'] == store.read_artifact_text(run, 'intake', 2)
    assert intake['body'] == 'Legacy actual facts: grey bag for lunch.'
    assert intake['structured']['constraints'] == ['Keep supplied appearance']


def test_same_source_old_cancelled_job_does_not_block_current_owner_or_lend_receipt(tmp_path, monkeypatch):
    orch, _, _, _, _, _, _, _, _ = harness(tmp_path, monkeypatch)
    orch.send(TEXT)
    rows = copy.deepcopy(orch.dispatcher.sessions())
    old = {**copy.deepcopy(rows[0]), 'session_id': 'old-session', 'parent_job_id': 'old-job',
           'status': 'cancelled', 'stop_requested': True}
    monkeypatch.setattr(orch.dispatcher, 'sessions', lambda: [old, *rows])
    assert orch._finishing_context() is not None
    # Same source and matching artifact alone cannot borrow another Job's result.
    monkeypatch.setattr(orch.dispatcher, 'sessions', lambda: [{**rows[0], 'parent_job_id': 'old-job'}])
    assert orch._finishing_context() is None
    # Outside the original parent scope, even its saved complete draft is insufficient.
    assert Orchestrator._finishing_context(orch) is None


@pytest.mark.parametrize('owner', ['missing-owner', 'foreign-owner'])
def test_unregistered_or_other_run_parent_skips_finishing_without_breaking_saved_task(tmp_path, monkeypatch, owner):
    orch, store, _, _, run, _, _, _, _ = harness(tmp_path, monkeypatch)
    orch.send(TEXT)
    if owner == 'foreign-owner':
        store.create_run('foreign-run', 'Isolated foreign owner', 'Synthetic brief')
        runtime = AgentRuntime(store, 'foreign-run')
        runtime.create_task(owner, role='orchestrator', parent_job_id=owner,
                            source_message_id=orch._task_user_input['message_id'])
        runtime.transition(owner, 'running')
    token = bind_task(owner)
    try:
        assert Orchestrator._finishing_context(orch) is None
    finally:
        reset_task(token)
    assert store.latest_artifact(run, 'writing').version == 1


def test_independent_writer_settled_after_cap_uses_same_single_finishing_call(tmp_path, monkeypatch):
    orch, store, pipe, ledger, run, seen, work, contexts, _ = harness(
        tmp_path, monkeypatch, independent=True)
    original_wait = orch._wait_owned_roles
    waited = []
    def wait():
        # All 12 work calls ended before releasing the real dispatcher worker.
        assert len(work) == 12 and contexts == []
        assert orch._test_started_role.wait(2)
        assert store.latest_artifact(run, 'writing') is None
        waited.append(True)
        orch._test_release_role.set()
        original_wait()
    monkeypatch.setattr(orch, '_wait_owned_roles', wait)
    result = orch.send(TEXT)
    assert waited == [True] and result.rounds == 13 and result.stopped_because == 'done'
    assert result.text == ANSWER and len(contexts) == 1
    assert len(seen) == len(ledger.entries(run)) == 14 and len(work) == 12
    assert len([p for p in seen if not p.get('tools')]) == 1
    assert store.latest_artifact(run, 'writing').version == 1 and not pipe.is_complete(run, 'writing')


@pytest.mark.parametrize('status', ['running', 'waiting', 'cancelled'])
def test_role_change_after_context_freeze_blocks_stale_conclusion(tmp_path, monkeypatch, status):
    orch, _, _, _, _, _, _, contexts, _ = harness(tmp_path, monkeypatch)
    orch.send(TEXT)
    rows = copy.deepcopy(orch.dispatcher.sessions())
    rows[0]['status'] = status
    monkeypatch.setattr(orch.dispatcher, 'sessions', lambda: rows)
    token = bind_task('finish-job')
    try:
        with pytest.raises(JobCancelled if status == 'cancelled' else NonRetryableStageError):
            contexts[0].check_current()
    finally:
        reset_task(token)


def test_cancel_before_finishing_has_zero_extra_http_calls(tmp_path, monkeypatch):
    store, _, settings, ledger, run = seed(tmp_path, monkeypatch)
    seen = mock_http(monkeypatch, lambda _: reply(('read_capabilities', {})))
    client = BudgetedClient(DeepSeekClient('synthetic-only', progress_stream=False), ledger,
                            run_id=run, role='orchestrator', stage='intake')
    def stopped():
        raise JobCancelled('Stopped after synthetic work result')
    with pytest.raises(JobCancelled):
        run_agent_loop(client, model='deepseek-flash', system='Synthetic work', messages=[],
            tools=[{'name': 'read_capabilities', 'input_schema': {'type': 'object'}}],
            execute_tool=lambda *_: 'Saved synthetic work',
            finishing_context=lambda: FinishingContext('Synthetic finish', [], stopped), max_rounds=12)
    assert len(seen) == len(ledger.entries(run)) == 1


@pytest.mark.parametrize('terminal', ['ask_user', 'decision', 'media_blocker'])
def test_existing_terminal_tool_priority_skips_finishing(tmp_path, monkeypatch, terminal):
    store, _, settings, ledger, run = seed(tmp_path, monkeypatch)
    seen = mock_http(monkeypatch, lambda _: reply(('read_capabilities', {})))
    factory_calls = []
    client = BudgetedClient(DeepSeekClient('synthetic-only', progress_stream=False), ledger,
                            run_id=run, role='orchestrator', stage='intake')
    result = run_agent_loop(client, model='deepseek-flash', system='Synthetic work', messages=[],
        tools=[{'name': 'read_capabilities', 'description': 'Synthetic only', 'input_schema': {'type': 'object'}}],
        execute_tool=lambda *_: ToolResult(terminal, concludes_turn=terminal != 'media_blocker'),
        stop_when=lambda: terminal == 'media_blocker',
        finishing_context=lambda: factory_calls.append(True), max_rounds=12)
    assert result.stopped_because == 'done' and len(seen) == 1 and not factory_calls
