"""Compacted own-role history remains readable without broad journal access."""
import json

import pytest

from fakes import ScriptedModel, final_turn, tool_turn
from studio.agent_sessions import AgentSessions, SessionJournal
from studio.agents.client import run_agent_loop
from studio.models import PipelineError
from studio.tools.dispatch import Dispatcher
from studio.tools.registry import tools_for
from test_revision_scope import env, RUN, QUOTE, plan, patch


def test_reader_pages_exact_outputs_and_excludes_private_requests(env):
    store = env[0]
    sessions = AgentSessions(store, RUN)
    sid = sessions.ensure(role='writer', stage='writing')['session_id']
    private = sessions.append(sid, 'model_request', system='PRIVATE_SYSTEM', messages=[
        {'role': 'user', 'content': 'PRIVATE_REQUEST'}], tools=[])
    outputs = []
    for index in range(15):
        outputs.append(sessions.append(sid, 'tool_result', step_id=str(index), call_id=str(index),
            name='read_artifact', output=f'EXACT_{index}_' + 'x' * 13000, is_error=False, skipped=False))
    page = sessions.read_role_history(sid)
    assert len(page['entries']) == 12 and page['next_before']
    older = sessions.read_role_history(sid, before_seq=page['next_before'])
    assert len(older['entries']) == 3 and older['next_before'] is None
    body, offset, chunks = outputs[0]['output'], 0, []
    while offset is not None:
        part = sessions.read_role_history(sid, entry_seq=outputs[0]['seq'], offset=offset, limit=1000)
        chunks.append(part['text'])
        offset = part['next_offset']
    assert ''.join(chunks) == body
    assert 'PRIVATE' not in json.dumps(page)
    with pytest.raises(PipelineError):
        sessions.read_role_history(sid, entry_seq=private['seq'])
    for args in ({'limit': 12001}, {'entry_seq': True}, {'offset': -1}, {'before_seq': 0}):
        with pytest.raises(PipelineError):
            sessions.read_role_history(sid, **args)


def test_after_actual_loop_compaction_model_can_retrieve_old_tool_result(env):
    sessions = AgentSessions(env[0], RUN)
    sid = sessions.ensure(role='writer', stage='writing')['session_id']
    journal = SessionJournal(sessions, sid)
    tools = tools_for('writer', local_revision=True)
    marker = 'OLD_MEASURED_RESULT'
    fetched = []
    class ReadingModel:
        calls = []
        def complete(self, **kwargs):
            self.calls.append(json.loads(json.dumps(kwargs)))
            number = len(self.calls)
            if number == 1:
                return tool_turn('large', 'read_artifact', {'stage': 'writing'})
            if number <= 5:
                return tool_turn(f'another-{number}', 'list_materials', {})
            if number == 6:
                text = '\n'.join(m['content'] for m in kwargs['messages'] if isinstance(m.get('content'), str))
                assert 'read_role_history' in text and 'historical_context' in text
                assert 'x' * 26000 not in str(kwargs['messages'])
                return tool_turn('history', 'read_role_history', {'entry_seq': fetched[0], 'limit': 12000})
            assert marker in str(kwargs['messages'])
            return final_turn('Found original result without replaying a write')
    model = ReadingModel()
    def execute(name, args):
        if name == 'read_artifact':
            return marker + 'x' * 26000
        if name == 'list_materials':
            fetched.append(next(e['seq'] for e in sessions.history(sid)
                                if e['kind'] == 'tool_result' and e['name'] == 'read_artifact'))
            return 'Small current result'
        return json.dumps(sessions.read_role_history(sid, **args))
    result = run_agent_loop(model, model='fake', system='Own role',
        messages=[{'role': 'user', 'content': 'Exact original task'}], tools=tools,
        execute_tool=execute, session_journal=journal)
    assert result.stopped_because == 'done' and len(model.calls) == 7
    assert any(e['kind'] == 'context_projected' for e in sessions.history(sid))
    recovered = sessions.recovery_messages(sid)
    assert 'read_role_history' in str(recovered) and marker in str(recovered)


def test_dispatch_reader_is_bound_to_own_session_and_cannot_choose_another(env):
    store, pipe, settings, ledger = env
    sessions = AgentSessions(store, RUN)
    other = sessions.ensure(role='writer', stage='writing')['session_id']
    sessions.append(other, 'tool_result', name='read_artifact', step_id='other', call_id='x',
                    output='OTHER_SESSION_SECRET', is_error=False, skipped=False)
    model = ScriptedModel([
        tool_turn('index', 'read_role_history', {}),
        tool_turn('cross', 'read_role_history', {'session_id': other}),
        tool_turn('save', 'submit_patch', patch()),
    ])
    result = Dispatcher(store, pipe, settings, model, ledger, RUN).dispatch(
        'writing', QUOTE, revision_scope=plan(env))
    assert result.submitted
    assert 'OTHER_SESSION_SECRET' not in model.requests_text()
    cross = model.calls[2]['messages'][-1]['content'][0]
    assert cross['is_error'] is True
