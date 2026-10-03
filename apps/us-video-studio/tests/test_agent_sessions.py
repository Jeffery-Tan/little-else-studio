"""Offline session/worker boundaries; no provider or runtime-data access."""
import json
import threading

import pytest

from fakes import ScriptedModel, final_turn, tool_turn
from studio.agent_runtime import AgentRuntime, bind_task, reset_task
from studio.agent_sessions import AgentSessions, SessionJournal
from studio.agents.client import ToolCall, Turn, Usage, run_agent_loop
from studio.conversation_input import receive
from studio.models import PipelineError
from studio.role_executor import RoleExecutor
from studio.tools.dispatch import Dispatcher
from test_revision_scope import env, RUN, QUOTE, patch, plan


def dispatcher(env, model):
    store, pipe, settings, ledger = env
    return Dispatcher(store, pipe, settings, model, ledger, RUN)


def test_journal_full_receipt_is_private_redacted_and_not_public_event(env):
    store = env[0]
    sessions = AgentSessions(store, RUN)
    sid = sessions.ensure(role='writer', stage='writing')['session_id']
    journal = SessionJournal(sessions, sid)
    output = 'receipt:' + 'x' * 6000
    model = ScriptedModel([tool_turn('r', 'list_materials', {}), final_turn('Done')])
    run_agent_loop(model, model='fake', system='Bearer top-secret-value',
        messages=[{'role': 'user', 'content': 'test'}], tools=[{'name': 'list_materials'}],
        execute_tool=lambda *_: output, session_journal=journal)
    history = sessions.history(sid)
    assert next(e for e in history if e['kind'] == 'tool_result')['output'] == output
    assert 'top-secret-value' not in json.dumps(history)
    assert output not in json.dumps(store.read_events(RUN))
    assert 'contract' not in sessions.snapshot(sid)


@pytest.mark.parametrize('private_block', [
    {'type': 'deepseek_reasoning', 'reasoning_content': 'PRIVATE_REASONING_FIXTURE'},
    {'type': 'thinking', 'thinking': 'PRIVATE_REASONING_FIXTURE', 'signature': 'PRIVATE_SIGNATURE'},
    {'type': 'redacted_thinking', 'data': 'PRIVATE_REASONING_FIXTURE'},
])
def test_reasoning_stays_in_live_protocol_but_not_session_history(env, private_block):
    from studio.agents.deepseek_client import _messages
    sessions = AgentSessions(env[0], RUN)
    sid = sessions.ensure(role='writer', stage='writing')['session_id']
    journal = SessionJournal(sessions, sid)
    turn = tool_turn('public-call', 'list_materials', {'reasoning_content': 'public argument'})
    turn.raw_blocks.insert(0, private_block)
    model = ScriptedModel([turn, final_turn('Public result')])
    run_agent_loop(model, model='fake', system='Own role',
        messages=[{'role': 'user', 'content': 'Actual current task'}],
        tools=[{'name': 'list_materials'}], execute_tool=lambda *_: 'PUBLIC_TOOL_RESULT',
        session_journal=journal)

    # Sanitizing persistent copies must not remove the provider metadata needed
    # by the next real model step in this already-running conversation.
    live = model.calls[1]['messages']
    assert private_block in next(m['content'] for m in live if m['role'] == 'assistant')
    if private_block['type'] == 'deepseek_reasoning':
        wire = _messages('', live, with_tools=True)
        assert next(m for m in wire if m['role'] == 'assistant')['reasoning_content'] == 'PRIVATE_REASONING_FIXTURE'

    history = sessions.history(sid)
    serialized = json.dumps(history)
    assert 'PRIVATE_REASONING_FIXTURE' not in serialized and 'PRIVATE_SIGNATURE' not in serialized
    assert next(e for e in history if e['kind'] == 'model_response')['private_reasoning_omitted']
    assert [e for e in history if e['kind'] == 'model_request'][1]['private_reasoning_omitted']
    assert next(e for e in history if e['kind'] == 'tool_started')['arguments'] == {'reasoning_content': 'public argument'}
    assert next(e for e in history if e['kind'] == 'tool_result')['output'] == 'PUBLIC_TOOL_RESULT'
    for entry in sessions.read_role_history(sid)['entries']:
        assert 'PRIVATE_' not in json.dumps(sessions.read_role_history(sid, entry_seq=entry['seq']))

    # Explicit continuation opens a new reasoning attempt. Exact public calls
    # and results remain available as data; old assistant/tool protocol turns
    # cannot be replayed with fabricated or missing reasoning fields.
    recovered = sessions.recovery_messages(sid)
    assert all(message['role'] == 'user' for message in recovered)
    assert 'PRIVATE_' not in json.dumps(recovered)
    assert 'public-call' in recovered[0]['content'] and 'PUBLIC_TOOL_RESULT' in recovered[0]['content']
    assert '不重放旧工具' in recovered[0]['content']
    if private_block['type'] == 'deepseek_reasoning':
        assert all(message['role'] in {'system', 'user'} for message in _messages('', recovered, with_tools=True))


def test_request_strips_assistant_reasoning_field_without_mutating_input(env):
    sessions = AgentSessions(env[0], RUN)
    sid = sessions.ensure(role='writer', stage='writing')['session_id']
    messages = [{'role': 'assistant', 'content': 'Public reply',
                 'reasoning_content': 'PRIVATE_REASONING_FIXTURE'}]
    SessionJournal(sessions, sid).request('fake', '', messages, [])
    request = sessions.history(sid)[0]
    assert messages[0]['reasoning_content'] == 'PRIVATE_REASONING_FIXTURE'
    assert request['messages'] == [{'role': 'assistant', 'content': 'Public reply'}]
    assert request['private_reasoning_omitted']
    assert 'PRIVATE_' not in json.dumps(sessions.recovery_messages(sid))


def test_deepseek_unknown_tool_recovers_public_pair_as_new_attempt_context(env):
    from studio.agents.deepseek_client import _messages
    sessions = AgentSessions(env[0], RUN)
    sid = sessions.ensure(role='writer', stage='writing')['session_id']
    journal = SessionJournal(sessions, sid)
    journal.request('deepseek-flash', '', [{'role': 'user', 'content': 'Original task'}], [])
    turn = tool_turn('unknown-save', 'submit_patch', {'changes': []})
    turn.raw_blocks.insert(0, {'type': 'deepseek_reasoning',
                               'reasoning_content': 'PRIVATE_REASONING_FIXTURE'})
    journal.response(turn)
    journal.tool_started(turn.tool_calls[0])
    recovered = sessions.recovery_messages(sid)
    history = json.loads(recovered[0]['content'].split('\n', 2)[1])
    assert history[-2]['content'][0]['id'] == 'unknown-save'
    assert history[-1]['content'][0]['tool_use_id'] == 'unknown-save'
    assert '结果未知' in history[-1]['content'][0]['content']
    assert 'PRIVATE_' not in json.dumps(recovered)
    assert all(message['role'] in {'system', 'user'} for message in _messages('', recovered, with_tools=True))


def test_received_message_is_idempotent_grounded_and_ack_after_request(env):
    store = env[0]
    sessions = AgentSessions(store, RUN)
    sid = sessions.ensure(role='writer', stage='writing')['session_id']
    receipt = receive(store, RUN, 'Use this original wording, keep all other shots.', 'inbox')
    mid = receipt['message_id']
    for _ in range(2):
        sessions.post_message(sid, mid, receipt['text'], source_message_id=mid)
    with pytest.raises(PipelineError):
        sessions.post_message(sid, mid, 'altered', source_message_id=mid)
    journal = SessionJournal(sessions, sid)
    messages = []
    journal.before_step(messages)
    assert len(sessions.pending(sid)) == 1
    journal.request('fake', '', messages, [])
    assert not sessions.pending(sid)
    assert receipt['text'] in sessions.recovery_messages(sid)[0]['content']


def test_recovery_pairs_unknown_tool_without_executing_it(env):
    sessions = AgentSessions(env[0], RUN)
    sid = sessions.ensure(role='writer', stage='writing')['session_id']
    journal = SessionJournal(sessions, sid)
    journal.request('fake', '', [{'role': 'user', 'content': 'Write'}], [])
    turn = tool_turn('uncertain', 'submit_stage', {'content': 'original', 'summary': 'original'})
    journal.response(turn)
    journal.tool_started(turn.tool_calls[0])
    restored = AgentSessions(env[0], RUN).recovery_messages(sid)
    assert restored[-1]['content'][0]['tool_use_id'] == 'uncertain'
    assert '结果未知' in restored[-1]['content'][0]['content']
    assert env[0].latest_artifact(RUN, 'writing').version == 1


def test_nonblocking_worker_different_thread_and_single_role_cancel(env):
    entered, release = threading.Event(), threading.Event()
    caller = threading.get_ident()
    seen = []
    class BlockingModel(ScriptedModel):
        def complete(self, **kwargs):
            seen.append(threading.get_ident())
            entered.set()
            assert release.wait(5)
            return super().complete(**kwargs)
    model = BlockingModel([tool_turn('save', 'submit_patch', patch())])
    dispatch = dispatcher(env, model)
    runtime = AgentRuntime(env[0], RUN)
    runtime.create_task('parent-job', role='orchestrator', parent_job_id='parent-job')
    runtime.transition('parent-job', 'running')
    token = bind_task('parent-job')
    try:
        row = dispatch.start_role('writing', QUOTE, revision_scope=plan(env))
    finally:
        reset_task(token)
    assert entered.wait(5) and seen != [caller]
    assert row['parent_job_id'] == 'parent-job'
    assert not dispatch.wait_session(row['session_id'])['done']
    dispatch.cancel_session(row['session_id'])
    release.set()
    result = dispatch.wait_session(row['session_id'], timeout_seconds=5)
    assert result['done'] and result['session']['status'] == 'cancelled'
    assert result['outcome']['status'] == 'cancelled'
    assert runtime.task('parent-job')['status'] == 'running'
    assert not runtime.task('parent-job')['stop_requested']
    assert env[1].stage_state(RUN, 'writing').open_attempt is None
    assert env[0].latest_artifact(RUN, 'writing').version == 1
    token = bind_task('parent-job')
    try:
        from studio.tools.workspace import ToolError
        with pytest.raises(ToolError, match='已经停止'):
            dispatch.start_role('writing', QUOTE, revision_scope=plan(env))
    finally:
        reset_task(token)


def test_completed_session_resume_only_returns_exact_saved_artifact(env):
    model = ScriptedModel([tool_turn('save', 'submit_patch', patch())])
    dispatch = dispatcher(env, model)
    first = dispatch.dispatch('writing', QUOTE, revision_scope=plan(env))
    assert first.submitted == 'writing-v2'
    recovered = dispatcher(env, ScriptedModel([])).resume_session(first.session_id)
    assert recovered.task_id == first.task_id
    assert recovered.artifact_sha256 == first.artifact_sha256
    assert len(env[0].list_artifacts(RUN, 'writing')) == 2


def test_inbox_blocks_stale_submit_then_interprets_exact_source(env):
    entered, release = threading.Event(), threading.Event()
    text = '只修改第一句，写得更自然，其他内容保留。'
    proposal = {'task': {'quote': text, 'mode': 'revise', 'deliverable': 'revision',
        'format': 'video', 'allowed_stages': ['writing'], 'pause_after': False}, 'constraints': []}
    class BlockingModel(ScriptedModel):
        def complete(self, **kwargs):
            if not self.calls:
                entered.set()
                assert release.wait(5)
            return super().complete(**kwargs)
    model = BlockingModel([tool_turn('old', 'submit_patch', patch(value='Stale')),
        tool_turn('interpret', 'propose_task_interpretation', proposal),
        tool_turn('new', 'submit_patch', patch(value='Fresh'))])
    dispatch = dispatcher(env, model)
    row = dispatch.start_role('writing', QUOTE, revision_scope=plan(env))
    assert entered.wait(5)
    base = env[0].latest_artifact(RUN, 'writing')
    source = receive(env[0], RUN, text, 'change-while-writing', feedback_target={
        'stage': 'writing', 'version': base.version, 'artifact_sha256': base.sha256,
        'path': ['segments', 0, 'line']})
    dispatch.message_session(row['session_id'], source['message_id'], text,
                             source_message_id=source['message_id'])
    release.set()
    result = dispatch.wait_session(row['session_id'], timeout_seconds=5)
    assert result['done'] and result['outcome']['ok']
    assert len(model.calls) == 3
    assert text in json.dumps(model.calls[1]['messages'], ensure_ascii=False)
    assert env[0].latest_artifact(RUN, 'writing').structured['segments'][0]['line'] == 'Fresh'
    history = AgentSessions(env[0], RUN).history(row['session_id'])
    old = next(item for item in history if item['kind'] == 'tool_result' and item['call_id'] == 'old')
    assert old['is_error'] and '新要求' in old['output']
    assert len(env[0].list_artifacts(RUN, 'writing')) == 2
    assert AgentSessions(env[0], RUN).snapshot(row['session_id'])['source_message_id'] == source['message_id']


def test_interruption_after_injection_before_request_does_not_drop_inbox(env):
    sessions = AgentSessions(env[0], RUN)
    sid = sessions.ensure(role='writer', stage='writing')['session_id']
    sessions.post_message(sid, 'note', 'Unacknowledged note')
    SessionJournal(sessions, sid).before_step([])
    recovered = AgentSessions(env[0], RUN)
    assert recovered.pending(sid)[0]['text'] == 'Unacknowledged note'


def test_interrupted_session_new_attempt_same_session_no_tool_replay(env):
    store, pipe, _, _ = env
    sessions = AgentSessions(store, RUN)
    scope = plan(env)
    sid = sessions.ensure(role='writer', stage='writing', contract={
        'brief': QUOTE, 'revision_scope': scope, 'turn_scope': None})['session_id']
    sessions.claim(sid)
    old = pipe.start_attempt(RUN, 'writing')
    runtime = AgentRuntime(store, RUN)
    runtime.create_task(old.key, role='writer', stage='writing', session_id=sid)
    runtime.transition(old.key, 'running')
    sessions.bind_attempt(sid, old.key)
    journal = SessionJournal(sessions, sid)
    journal.request('fake', '', [{'role': 'user', 'content': 'Original role task'}], [])
    old_turn = tool_turn('old-save', 'submit_patch', patch(value='Old unknown result'))
    journal.response(old_turn)
    journal.tool_started(old_turn.tool_calls[0])
    sessions.interrupt_open_sessions()
    model = ScriptedModel([tool_turn('new-save', 'submit_patch', patch(value='New explicit result'))])
    outcome = dispatcher(env, model).resume_session(sid)
    assert outcome.session_id == sid and outcome.task_id != old.key
    assert outcome.submitted == 'writing-v2'
    assert sessions.snapshot(sid)['turn'] == 2
    assert runtime.task(old.key)['status'] == 'interrupted'
    assert '结果未知' in model.requests_text()
    assert store.latest_artifact(RUN, 'writing').structured['segments'][0]['line'] == 'New explicit result'


def test_executor_capacity_exclusion_and_depth_bound():
    pool = RoleExecutor(max_workers=1, max_pending=1, max_depth=1)
    entered, release = threading.Event(), threading.Event()
    errors = []
    def work():
        entered.set()
        with pytest.raises(PipelineError, match='depth'):
            pool.submit('nested', lambda: None)
        assert release.wait(5)
        return 1
    try:
        future = pool.submit('one', work)
        assert entered.wait(5)
        with pytest.raises(PipelineError, match='already'):
            pool.submit('one', lambda: None)
        with pytest.raises(PipelineError, match='full'):
            pool.submit('two', lambda: None)
        release.set()
        assert future.result(5) == 1
    finally:
        release.set()
        pool.shutdown()


def test_safe_reads_parallel_and_results_keep_model_order():
    barrier = threading.Barrier(2)
    calls = [ToolCall('a', 'list_materials', {}), ToolCall('b', 'read_capabilities', {})]
    turn = Turn(tool_calls=calls, stop_reason='tool_use', usage=Usage(1, 1),
        raw_blocks=[{'type': 'tool_use', 'id': c.id, 'name': c.name, 'input': c.input} for c in calls])
    model = ScriptedModel([turn, final_turn('Done')])
    def read(name, args):
        barrier.wait(3)
        return name
    run_agent_loop(model, model='fake', system='', messages=[{'role': 'user', 'content': 'Read'}],
        tools=[{'name': c.name} for c in calls], execute_tool=read,
        parallel_read_tools=frozenset(c.name for c in calls))
    results = model.calls[-1]['messages'][-1]['content']
    assert [result['tool_use_id'] for result in results] == ['a', 'b']
    assert not any(result['is_error'] for result in results)


def test_multiple_consumed_messages_keep_turn_conditions_at_submit(env):
    from test_context_projection import clause
    texts = ['本轮这句用冷幽默。', '这句不要口头禅。']
    proposals = [{'task': {'quote': text, 'mode': 'revise', 'deliverable': 'revision',
        'format': 'video', 'allowed_stages': ['writing'], 'pause_after': False},
        'constraints': [clause(text, key=key, value=text, lifetime='turn', stages=['writing'])]}
        for text, key in zip(texts, ['voice.register', 'voice.filler'])]
    receipts = []
    handle = {}
    class InboxModel(ScriptedModel):
        def complete(self, **kwargs):
            if len(self.calls) in {0, 2}:
                index = len(receipts)
                receipt = receive(env[0], RUN, texts[index], f'multiple-{index}')
                receipts.append(receipt)
                handle['dispatch'].message_session(handle['sid'], receipt['message_id'], receipt['text'],
                                                   source_message_id=receipt['message_id'])
            return super().complete(**kwargs)
    # Hold the first request until start_role has returned its stable identity.
    entered, release = threading.Event(), threading.Event()
    class StartedModel(InboxModel):
        def complete(self, **kwargs):
            if not self.calls:
                entered.set()
                assert release.wait(5)
            return super().complete(**kwargs)
    model = StartedModel([tool_turn('old', 'submit_patch', patch()),
        tool_turn('first-condition', 'propose_task_interpretation', proposals[0]),
        final_turn('尚未提交'), tool_turn('second-condition', 'propose_task_interpretation', proposals[1]),
        tool_turn('fresh', 'submit_patch', patch(value='New line'))])
    dispatch = dispatcher(env, model)
    row = dispatch.start_role('writing', QUOTE, revision_scope=plan(env))
    assert entered.wait(5)
    handle.update(dispatch=dispatch, sid=row['session_id'])
    release.set()
    result = dispatch.wait_session(row['session_id'], timeout_seconds=5)
    assert result['done'] and result['outcome']['ok']
    assert all(text in json.dumps(model.calls[-1]['messages'], ensure_ascii=False) for text in texts)
    rows = AgentSessions(env[0], RUN).source_messages(row['session_id'])
    assert rows == [receipt['message_id'] for receipt in receipts]
