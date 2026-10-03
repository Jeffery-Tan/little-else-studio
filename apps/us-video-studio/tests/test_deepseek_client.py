"""DeepSeek protocol regression tests. Mock HTTP only; no credentials or network."""
import copy
import json

import httpx
import pytest

from studio.agents.client import ModelError, run_agent_loop
from studio.agents.deepseek_client import DeepSeekClient, ENDPOINT, REASONING_BLOCK, _messages, _parse
from studio.agents.openai_compat import _messages as generic_messages

TOOLS = [{"name": "read_artifact", "description": "Read scoped artifact", "input_schema": {"type": "object"}}]


def response(text="Done", reasoning="private reasoning", calls=None, finish=None, usage=None):
    message = {"content": text, "reasoning_content": reasoning}
    if calls:
        message["tool_calls"] = [{"id": cid, "type": "function", "function": {
            "name": "read_artifact", "arguments": json.dumps({"stage": stage})}} for cid, stage in calls]
    return {"model": "deepseek-flash", "choices": [{"finish_reason": finish or ("tool_calls" if calls else "stop"),
        "message": message}], "usage": {"prompt_tokens": 31, "completion_tokens": 17} if usage is None else usage}


def transport(monkeypatch, replies):
    original = httpx.AsyncClient
    seen = []
    def handle(request):
        seen.append(request)
        reply = replies.pop(0)
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)
    def factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        return original(**kwargs, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


def request(client, **overrides):
    params = {"model": "deepseek-flash", "system": "Role scope", "messages": [{"role": "user", "content": "Task"}], "tools": TOOLS}
    return client.complete(**dict(params, **overrides))


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_fixed_official_endpoint_enabled_high_and_provider_output_default(monkeypatch, model):
    seen = transport(monkeypatch, [response()])
    turn = request(DeepSeekClient("synthetic-only"), model=model)
    assert len(seen) == 1 and str(seen[0].url) == ENDPOINT
    assert seen[0].headers["Authorization"] == "Bearer synthetic-only"
    payload = json.loads(seen[0].content)
    assert payload["model"] == model
    assert "max_tokens" not in payload
    assert "max_completion_tokens" not in payload
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high" and payload["stream"] is False
    assert payload["tools"][0]["function"]["parameters"] == TOOLS[0]["input_schema"]
    assert turn.text == "Done" and turn.thinking == ""
    assert turn.raw_blocks[0] == {"type": REASONING_BLOCK, "reasoning_content": "private reasoning"}


def test_explicit_low_reasoning_effort_is_sent_when_requested(monkeypatch):
    seen = transport(monkeypatch, [response()])
    request(DeepSeekClient('synthetic-only', max_tokens=16384, reasoning_effort='low'))
    payload = json.loads(seen[0].content)
    assert payload['reasoning_effort'] == 'low'
    assert payload['max_tokens'] == 16384


def test_non_thinking_tool_call_uses_public_completion_budget_and_standard_replay():
    client = DeepSeekClient('synthetic-only', max_tokens=24576, reasoning_effort='none')
    payload = client._payload('deepseek-flash', 'System',
        [{'role': 'user', 'content': 'Create three concise directions'}], TOOLS)
    assert payload['thinking'] == {'type': 'disabled'}
    assert payload['max_tokens'] == 24576
    assert 'reasoning_effort' not in payload
    assert payload['tools'][0]['function']['name'] == TOOLS[0]['name']


def test_non_thinking_tool_round_replays_without_private_reasoning(monkeypatch):
    seen = transport(monkeypatch, [
        response('', None, [('read-one', 'intake')]),
        response('三个方向已完成。', None),
    ])
    executed = []
    result = run_agent_loop(DeepSeekClient('synthetic-only', max_tokens=24576,
        reasoning_effort='none'), model='deepseek-flash', system='director scope',
        messages=[{'role': 'user', 'content': 'Draft directions'}], tools=TOOLS,
        execute_tool=lambda name, args: executed.append((name, args)) or 'Saved intake')
    assert result.text == '三个方向已完成。'
    assert executed == [('read_artifact', {'stage': 'intake'})]
    replay = json.loads(seen[1].content)['messages']
    assistant = next(row for row in replay if row['role'] == 'assistant')
    assert assistant['tool_calls'][0]['id'] == 'read-one'
    assert 'reasoning_content' not in assistant
    assert next(row for row in replay if row['role'] == 'tool')['tool_call_id'] == 'read-one'


def test_streamed_public_progress_never_exposes_reasoning_or_partial_tool_args(monkeypatch):
    def chunk(delta, finish=None, usage=None):
        return 'data: ' + json.dumps({'model': 'deepseek-flash',
            'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}], 'usage': usage}) + '\n\n'
    wire = (
        chunk({'reasoning_content': 'private reasoning '}) +
        chunk({'content': '正在核对商品证据。' * 11}) +
        chunk({'tool_calls': [{'index': 0, 'id': 'one', 'type': 'function',
               'function': {'name': 'read_artifact', 'arguments': '{"stage":'}}]}) +
        chunk({'tool_calls': [{'index': 0, 'function': {'arguments': '"intake"}'}}]},
              finish='tool_calls', usage={'prompt_tokens': 13, 'completion_tokens': 19}) +
        'data: [DONE]\n\n')
    seen = transport(monkeypatch, [httpx.Response(200, text=wire,
                                       headers={'content-type': 'text/event-stream'})])
    progress = []
    turn = DeepSeekClient('synthetic-only').complete_with_progress(model='deepseek-flash',
        system='role', messages=[{'role': 'user', 'content': 'check'}], tools=TOOLS,
        on_progress=progress.append)
    assert json.loads(seen[0].content)['stream'] is True
    assert turn.tool_calls[0].input == {'stage': 'intake'}
    assert turn.raw_blocks[0] == {'type': REASONING_BLOCK, 'reasoning_content': 'private reasoning '}
    assert turn.usage.input_tokens == 13 and turn.usage.output_tokens == 19
    assert any(p['type'] == 'agent_narration_preview' for p in progress)
    assert all(p['type'] in ('agent_narration_preview', 'agent_activity') for p in progress)
    assert 'private reasoning' not in str(progress) and 'intake' not in str(progress)


def test_stream_without_done_is_not_dispatched_or_accounted_as_final(monkeypatch):
    wire = 'data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'content': 'partial'},
                                                'finish_reason': 'stop'}],
                                    'usage': {'prompt_tokens': 4, 'completion_tokens': 6}}) + '\n\n'
    transport(monkeypatch, [httpx.Response(200, text=wire)])
    with pytest.raises(ModelError) as caught:
        DeepSeekClient('synthetic-only').complete_with_progress(model='deepseek-flash',
            system='role', messages=[{'role': 'user', 'content': 'check'}], tools=TOOLS)
    assert caught.value.usage_status == 'unknown'


def test_multi_tool_rounds_replay_exact_reasoning_and_never_display_it(monkeypatch):
    reasoning = "  私有协议字段\nsecond line\t保持字节  "
    seen = transport(monkeypatch, [
        response("检查资料", reasoning, [("a", "intake"), ("b", "direction")]),
        response("", "second private", [("c", "material_plan")]), response("公开结论", "final private"),
    ])
    executed, observed = [], []
    def execute(name, args):
        executed.append((name, args))
        return "Scoped evidence for " + args["stage"]
    result = run_agent_loop(DeepSeekClient("synthetic-only"), model="deepseek-flash", system="director scope",
        messages=[{"role": "user", "content": "Make direction"}], tools=TOOLS, execute_tool=execute,
        on_turn=lambda turn: observed.append((turn.text, turn.thinking)))
    assert result.text == "公开结论"
    assert observed[0][0] == "检查资料"  # Progress is observable, not part of the final reply.
    assert len(executed) == 3
    second = json.loads(seen[1].content)["messages"]
    assert second[2]["reasoning_content"] == reasoning
    assert [m["tool_call_id"] for m in second if m["role"] == "tool"] == ["a", "b"]
    third = json.loads(seen[2].content)["messages"]
    assert [m["reasoning_content"] for m in third if m["role"] == "assistant"] == [reasoning, "second private"]
    assert all(thinking == "" for _, thinking in observed)
    assert "private" not in result.text and reasoning not in str(result.tool_log)


def test_tool_free_truncated_response_keeps_reasoning_when_tools_are_still_requested(monkeypatch):
    seen = transport(monkeypatch, [response("开头", "private truncated", finish="length"), response("结尾", "private final")])
    result = run_agent_loop(DeepSeekClient("synthetic-only"), model="deepseek-flash", system="writer",
        messages=[{"role": "user", "content": "Write"}], tools=TOOLS, execute_tool=lambda *_: "", max_continuations=1)
    assert result.text == "开头\n结尾"
    assistant = next(m for m in json.loads(seen[1].content)["messages"] if m["role"] == "assistant")
    assert assistant["content"] == "开头"
    assert assistant["reasoning_content"] == "private truncated"
    assert "tool_calls" not in assistant


def test_tool_free_completed_response_replays_reasoning_only_if_next_request_has_tools():
    turn = _parse(response("Visible response", "Hidden reasoning"), "deepseek-flash", require_reasoning=True)
    history = [{"role": "assistant", "content": turn.raw_blocks}, {"role": "user", "content": "Next question"}]
    before = copy.deepcopy(history)
    with_tools = _messages("role", history, with_tools=True)
    without_tools = _messages("role", history, with_tools=False)
    assert with_tools[1]["reasoning_content"] == "Hidden reasoning"
    assert "reasoning_content" not in without_tools[1]
    assert without_tools[1]["content"] == "Visible response"
    assert history == before
    assert "reasoning_content" not in json.dumps(generic_messages("role", history))


@pytest.mark.parametrize("content", ["上一轮已保存的回复", [{"type": "text", "text": "上一轮已保存的回复"}]])
def test_cross_job_display_chat_is_marked_history_data_not_fabricated_reasoning(monkeypatch, content):
    seen = transport(monkeypatch, [response()])
    history = [{"role": "user", "content": "Previous task"}, {"role": "assistant", "content": content},
               {"role": "user", "content": "Current instruction"}]
    original = copy.deepcopy(history)
    request(DeepSeekClient("synthetic-only"), messages=history)
    wire = json.loads(seen[0].content)["messages"]
    assert wire[2]["role"] == "user"
    assert "不是本轮指令" in wire[2]["content"]
    assert json.loads(wire[2]["content"].split("\n", 1)[1]) == {"saved_assistant_text": "上一轮已保存的回复"}
    assert "reasoning_content" not in wire[2]
    assert wire[-1] == {"role": "user", "content": "Current instruction"}
    assert history == original


def test_missing_reasoning_on_a_tool_call_stops_before_http_with_known_zero_usage(monkeypatch):
    seen = transport(monkeypatch, [])
    history = [{"role": "assistant", "content": [{"type": "tool_use", "id": "one", "name": "read_artifact", "input": {}}]}]
    with pytest.raises(ModelError) as failure:
        request(DeepSeekClient("synthetic-only"), messages=history)
    assert seen == []
    assert failure.value.provider_invoked is False
    assert failure.value.usage.input_tokens == failure.value.usage.output_tokens == 0


def test_same_client_does_not_cache_other_roles_reasoning(monkeypatch):
    seen = transport(monkeypatch, [response("Director output", "director secret"), response("Reviewer output", "reviewer secret")])
    client = DeepSeekClient("synthetic-only")
    request(client, system="director", messages=[{"role": "user", "content": "Produce"}])
    request(client, system="reviewer", messages=[{"role": "user", "content": "Review artifact only"}])
    assert "director secret" not in seen[1].content.decode()
    assert "Director output" not in seen[1].content.decode()
    assert json.loads(seen[1].content)["messages"][0]["content"] == "reviewer"


@pytest.mark.parametrize("status", [302, 307, 401, 429, 500])
def test_http_errors_never_retry_follow_redirect_or_echo_private_body(monkeypatch, status):
    seen = transport(monkeypatch, [httpx.Response(status, headers={"Location": "https://other.invalid"},
                  text="synthetic-only private prompt reasoning")])
    with pytest.raises(ModelError) as failure:
        request(DeepSeekClient("synthetic-only"))
    assert len(seen) == 1
    assert failure.value.usage.input_tokens is None
    assert "synthetic-only" not in str(failure.value)
    assert "private prompt" not in str(failure.value)


@pytest.mark.parametrize("reasoning", [None, [], {"private": "secret"}, 123])
def test_malformed_reasoning_retains_known_usage_without_echoing_contents(reasoning):
    with pytest.raises(ModelError) as failure:
        _parse(response(reasoning=reasoning), "deepseek-flash", require_reasoning=True)
    assert failure.value.usage.input_tokens == 31 and failure.value.usage.output_tokens == 17
    assert failure.value.usage_status == 'known'
    assert "secret" not in str(failure.value)


def test_unknown_usage_remains_unknown_and_no_tools_payload_omits_tools(monkeypatch):
    seen = transport(monkeypatch, [response(usage={})])
    turn = request(DeepSeekClient("synthetic-only", max_tokens=500), tools=[])
    assert turn.usage.input_tokens is None and turn.usage.output_tokens is None
    payload = json.loads(seen[0].content)
    assert "tools" not in payload and payload["max_tokens"] == 500


@pytest.mark.parametrize("limit", [0, -1, 393217, True, 1.5, "16384"])
def test_invalid_explicit_output_limit_is_rejected_before_http(monkeypatch, limit):
    seen = transport(monkeypatch, [])
    with pytest.raises(ValueError, match="max_tokens"):
        DeepSeekClient("synthetic-only", max_tokens=limit)
    assert seen == []


@pytest.mark.parametrize("model", ["deepseek-future-2027", "deepseek-reasoner", "team/deepseek-next:preview"])
def test_discovered_future_models_and_existing_aliases_are_sent_unchanged(monkeypatch, model):
    seen = transport(monkeypatch, [response()])
    request(DeepSeekClient("synthetic-only"), model=model)
    assert len(seen) == 1
    assert json.loads(seen[0].content)["model"] == model


@pytest.mark.parametrize("model", [None, 123, "", "model\nother", "model\r", " model", "model name", "模型", "m" * 201, "model?key=value"])
def test_invalid_model_is_rejected_without_request_or_unknown_usage(monkeypatch, model):
    seen = transport(monkeypatch, [])
    with pytest.raises(ModelError) as failure:
        request(DeepSeekClient("synthetic-only"), model=model)
    assert seen == [] and failure.value.provider_invoked is False
    assert failure.value.usage.input_tokens == failure.value.usage.output_tokens == 0


@pytest.mark.parametrize("token", [None, 123, "", " ", "synthetic key", "synthetic\nkey", "synthetic\tkey", "synthetic\x00key", "synthetic\x7fkey", "synthetic-密钥"])
def test_invalid_header_key_is_rejected_before_any_request_without_echo(monkeypatch, token):
    seen = transport(monkeypatch, [])
    with pytest.raises(ValueError) as failure:
        DeepSeekClient(token)
    assert seen == []
    assert str(failure.value) == "DeepSeek API key must contain ASCII characters without whitespace or control characters"
