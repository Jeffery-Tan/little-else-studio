"""Read-only progress events must arrive before blocking work, without inputs."""
from fakes import final_turn, tool_turn
from studio.agents.client import run_agent_loop


def test_activity_precedes_model_and_tool_and_preserves_result():
    events = []
    turns = [tool_turn('c1', 'read_artifact', {'stage': 'direction', 'private_input': 'not-for-ui'}),
             final_turn('A final reply')]

    class Model:
        def complete(self, **kwargs):
            assert events[-1]['phase'] == 'thinking'
            return turns.pop(0)

    def execute(name, args):
        assert events[-1] == {'type': 'agent_activity', 'phase': 'tool',
                              'tool': name, 'args': {'stage': 'direction'}, 'round': 1}
        assert args['private_input'] == 'not-for-ui'
        return 'Original artifact'

    result = run_agent_loop(Model(), model='fake', system='test', messages=[], tools=[{'name': 'read_artifact'}],
                            execute_tool=execute, on_activity=events.append)
    assert [e['phase'] for e in events] == ['thinking', 'tool', 'thinking']
    assert 'not-for-ui' not in str(events)
    assert result.text == 'A final reply'
    assert result.tool_log[0]['result'] == 'Original artifact'


def test_cancellation_before_call_emits_no_work_started():
    import pytest
    from studio.agents.client import JobCancelled

    events = []

    def cancel():
        raise JobCancelled('Stop')

    with pytest.raises(JobCancelled):
        run_agent_loop(None, model='fake', system='test', messages=[], tools=[],
                       execute_tool=lambda *_: None, on_activity=events.append, check_cancel=cancel)
    assert events == []
