"""Offline app-server fixtures. Never invoke Codex, credentials or a model."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from studio.agents import codex_client as codex
from studio.agents.client import ModelError, Usage


TOOLS = [{"name": "dispatch", "description": "Delegate a Studio role",
          "input_schema": {"type": "object", "properties": {"role": {"type": "string"}}}}]


def policy(names=()):
    return {"mcp_enabled": dict.fromkeys(names, False),
            "features": dict.fromkeys(codex.DISABLED_FEATURES, False),
            "skip_host_skill_discovery": True, "web_search": "disabled", "sandbox_mode": "read-only",
            "approval_policy": "never", "model_provider": "openai", "forced_login_method": "chatgpt",
            "project_doc_max_bytes": 0, "developer_instructions_empty": True, "agents_disabled": True}


def event(method, **params):
    return {"method": method, "params": {"threadId": "isolated-thread", "turnId": "one-turn", **params}}


def finished(text=None, *, calls=None, phase="final_answer", usage=True):
    answer = json.dumps({"text": text or "Prepared.", "tool_calls": calls or []})
    result = [event("item/completed", item={"id": "answer", "type": "agentMessage", "phase": phase, "text": answer})]
    if usage:
        result += [event("thread/tokenUsage/updated", tokenUsage={
            "last": {"inputTokens": 101, "outputTokens": 17},
            "total": {"inputTokens": 99999, "outputTokens": 99999}})]
    result += [event("turn/completed", turn={"id": "one-turn", "status": "completed"})]
    return result


class FakeSession:
    instances = []
    events = []
    thread_change = {}
    metadata = None
    denied_request = False

    def __init__(self, command, cwd, config, timeout):
        self.command, self.cwd, self.config = command, cwd, config
        self.sent = []
        self.closed = False
        self.deadline = 0
        self.stream = iter(copy.deepcopy(type(self).events))
        type(self).instances.append(self)

    def initialize(self):
        self.sent.append(("initialize", {}))
        return {"userAgent": "fixture", "codexHome": str(self.cwd / "synthetic-codex-home")}

    def policy_metadata(self):
        return copy.deepcopy(type(self).metadata or policy())

    def request(self, method, params):
        self.sent.append((method, params))
        if method == "thread/start":
            result = {"sandbox": {"type": "readOnly", "networkAccess": False},
                      "approvalPolicy": "never", "modelProvider": "openai", "model": params.get("model", "cli-default-model"),
                      "cwd": str(self.cwd), "runtimeWorkspaceRoots": [str(self.cwd)], "instructionSources": [],
                      "thread": {"id": "isolated-thread", "ephemeral": True, "environments": [], "turns": []}}
            result.update(copy.deepcopy(type(self).thread_change))
            return result
        if method == "turn/start":
            return {"turn": {"id": "one-turn", "status": "inProgress"}}
        raise AssertionError("Unexpected RPC")

    def event(self):
        try:
            return next(self.stream)
        except StopIteration:
            raise ModelError(0, "fixture stream interrupted")

    def close(self):
        self.closed = True


@pytest.fixture
def client(monkeypatch, tmp_path):
    FakeSession.instances, FakeSession.events = [], finished()
    FakeSession.thread_change, FakeSession.metadata = {}, None
    FakeSession.denied_request = False
    monkeypatch.setattr(codex, "_StdioSession", FakeSession)
    result = codex.CodexClient(run_id="unit-run", role="orchestrator", data_root=tmp_path)
    result._mcp_names = ("configured_mcp",)
    return result


def complete(client, **kwargs):
    return client.complete(model="fixture-model", system="Only this role context.",
                           messages=[{"role": "user", "content": "Explicit Studio message"}], tools=TOOLS, **kwargs)


def test_fresh_ephemeral_context_and_only_dispatch_requests(client):
    FakeSession.events = finished(calls=[{"id": "d1", "name": "dispatch", "arguments_json": '{"role":"director"}'}])
    first = complete(client)
    second = complete(client)
    assert first.tool_calls[0].name == "dispatch"
    assert first.tool_calls[0].input == {"role": "director"}
    assert first.raw_blocks[-1] == {"type": "tool_use", "id": "d1", "name": "dispatch", "input": {"role": "director"}}
    assert first.stop_reason == "tool_use"
    assert first.usage == Usage(101, 17)  # per-turn last, never cumulative total
    assert second.tool_calls == first.tool_calls
    assert FakeSession.instances[0].cwd != FakeSession.instances[1].cwd
    for session in FakeSession.instances:
        assert session.closed
        assert session.cwd != client.data_root
        start = next(p for m, p in session.sent if m == "thread/start")
        assert start["ephemeral"] and start["environments"] == []
        assert start["dynamicTools"] == [] and start["selectedCapabilityRoots"] == []
        assert start["baseInstructions"].endswith("Only this role context.")
        assert start["developerInstructions"] == ""
        assert start["allowProviderModelFallback"] is False
        turn = next(p for m, p in session.sent if m == "turn/start")
        explicit = json.loads(turn["input"][0]["text"])
        assert explicit["messages"] == [{"role": "user", "content": "Explicit Studio message"}]
        assert turn["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
        assert session.config["mcp_servers.configured_mcp.enabled"] is False
        assert not any(m in {"thread/resume", "thread/fork", "command/exec"} for m, _ in session.sent)


@pytest.mark.parametrize("changes", [
    {"sandbox": {"type": "dangerFullAccess"}},
    {"sandbox": {"type": "readOnly", "networkAccess": True}},
    {"approvalPolicy": "on-request"}, {"instructionSources": ["development/AGENTS.md"]},
    {"model": "different-model"}, {"runtimeWorkspaceRoots": ["J:/E-Business"]},
    {"thread": {"id": "old", "ephemeral": False, "environments": [], "turns": []}},
    {"thread": {"id": "old", "ephemeral": True, "environments": [{"cwd": "J:/E-Business"}], "turns": []}},
])
def test_thread_policy_drift_refused_before_model_call(client, changes):
    FakeSession.thread_change = changes
    with pytest.raises(codex.CodexUnavailable):
        complete(client)
    assert not any(m == "turn/start" for m, _ in FakeSession.instances[0].sent)


def test_new_inherited_mcp_is_not_silently_allowed(client):
    FakeSession.metadata = policy(("newly_enabled_server",))
    FakeSession.metadata["mcp_enabled"]["newly_enabled_server"] = True
    with pytest.raises(codex.CodexUnavailable, match="policy differs"):
        complete(client)
    assert not any(m == "thread/start" for m, _ in FakeSession.instances[0].sent)


def test_unknown_usage_stays_unknown(client):
    FakeSession.events = finished(usage=False)
    assert complete(client).usage == Usage(None, None)


def test_unphased_legacy_answer_is_checked_only_after_completed_turn(client):
    FakeSession.events = finished(phase=None)
    assert complete(client).text == "Prepared."


def test_partial_usage_survives_protocol_failure_and_does_not_retry(client):
    FakeSession.events = [event("thread/tokenUsage/updated", tokenUsage={"last": {"inputTokens": 77}})]
    with pytest.raises(ModelError) as caught:
        complete(client)
    assert caught.value.usage == Usage(77, None)
    assert len(FakeSession.instances) == 1


def test_foreign_events_cannot_supply_answer_or_usage(client):
    wrong = event("thread/tokenUsage/updated", tokenUsage={"last": {"inputTokens": 500}})
    wrong["params"]["threadId"] = "development-thread"
    FakeSession.events = [wrong] + finished(usage=False)
    assert complete(client).usage == Usage(None, None)


@pytest.mark.parametrize("item_type", ["commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall", "webSearch"])
def test_native_tool_attempt_invalidates_turn_without_dispatch(client, item_type):
    FakeSession.events = [event("item/started", item={"type": item_type})] + finished()
    with pytest.raises(ModelError, match="native tool") as caught:
        complete(client)
    assert caught.value.usage == Usage(101, 17)


def test_denied_native_request_invalidates_answer(client):
    FakeSession.denied_request = True
    with pytest.raises(codex.CodexUnavailable, match="unsupported action during setup"):
        complete(client)
    assert not any(m == "turn/start" for m, _ in FakeSession.instances[0].sent)


@pytest.mark.parametrize("calls", [
    [{"id": "x", "name": "exec_command", "arguments_json": '{}'}],
    [{"id": "x", "name": "dispatch", "arguments_json": '[]'}],
    [{"id": "x", "name": "dispatch", "arguments_json": '{invalid'}],
    [{"id": "x", "name": "dispatch", "arguments_json": '{}'}] * 2,
])
def test_invalid_structured_requests_do_not_escape_to_tools(client, calls):
    FakeSession.events = finished(calls=calls)
    with pytest.raises(ModelError, match="structured output is invalid"):
        complete(client)


def test_cancellation_before_turn_does_not_start_model(client):
    calls = []
    def check():
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("cancelled")
    client.check_cancel = check
    with pytest.raises(RuntimeError, match="cancelled"):
        complete(client)
    assert not any(m == "turn/start" for m, _ in FakeSession.instances[0].sent)


def test_successful_preflight_is_local_only_and_discovers_mcp_names(client, monkeypatch):
    monkeypatch.setattr(codex.shutil, "which", lambda _: "codex.exe")
    monkeypatch.setattr(codex.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=f"codex-cli {codex.AUDITED_VERSION}\n"))
    monkeypatch.setattr(codex, "_verify_schema", lambda *a: None)
    FakeSession.metadata = policy(("local_mcp",))
    result = client.preflight()
    assert result["model_invoked"] is False
    assert client._mcp_names == ("local_mcp",)
    assert FakeSession.instances[-1].config["mcp_servers.local_mcp.enabled"] is False
    assert all(s.sent == [("initialize", {})] for s in FakeSession.instances)


def test_desktop_cli_discovery_without_terminal_path(client, monkeypatch, tmp_path):
    monkeypatch.setattr(codex.sys, "platform", "win32")
    monkeypatch.setattr(codex.shutil, "which", lambda _: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    root = tmp_path / "OpenAI" / "Codex" / "bin"
    old = root / "aaaaaaaaaaaaaaaa" / "codex.exe"
    latest = root / "bbbbbbbbbbbbbbbb" / "codex.exe"
    for i, path in enumerate((old, latest)):
        path.parent.mkdir(parents=True)
        path.write_text("synthetic executable, never run")
        os.utime(path, (100 + i, 100 + i))
    (root / "codex.exe").write_text("legacy flat executable, never run")
    (root / "cccccccccccccccc").mkdir()  # An incomplete update is ignored.
    commands = []
    def version(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=0, stdout=f"codex-cli {codex.AUDITED_VERSION}\n")
    monkeypatch.setattr(codex.subprocess, "run", version)
    monkeypatch.setattr(codex, "_verify_schema", lambda *a: None)
    result = client.preflight()
    assert client.command == str(latest)
    assert commands == [[str(latest), "--version"]]
    assert result["model_invoked"] is False
    assert all(s.sent == [("initialize", {})] for s in FakeSession.instances)


def test_explicit_cli_on_path_wins_over_desktop(monkeypatch):
    monkeypatch.setattr(codex.shutil, "which", lambda _: "chosen-codex.exe")
    monkeypatch.setattr(codex, "_desktop_codex_candidates", lambda: pytest.fail("must not scan"))
    assert codex.resolve_codex_command() == "chosen-codex.exe"


def test_missing_custom_cli_does_not_fall_back(monkeypatch):
    monkeypatch.setattr(codex.sys, "platform", "win32")
    monkeypatch.setattr(codex.shutil, "which", lambda _: None)
    monkeypatch.setattr(codex, "_desktop_codex_candidates", lambda: pytest.fail("must not scan"))
    with pytest.raises(codex.CodexUnavailable, match="not installed"):
        codex.resolve_codex_command("explicit-missing-codex")


def test_absent_desktop_installation_fails_before_model(client, monkeypatch, tmp_path):
    monkeypatch.setattr(codex.sys, "platform", "win32")
    monkeypatch.setattr(codex.shutil, "which", lambda _: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    with pytest.raises(codex.CodexUnavailable) as error:
        client.preflight()
    assert error.value.provider_invoked is False
    assert FakeSession.instances == []


def test_flat_desktop_layout_and_non_windows_resolution(monkeypatch, tmp_path):
    monkeypatch.setattr(codex.sys, "platform", "win32")
    monkeypatch.setattr(codex.shutil, "which", lambda _: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    flat = tmp_path / "OpenAI" / "Codex" / "bin" / "codex.exe"
    flat.parent.mkdir(parents=True)
    flat.write_text("synthetic, never run")
    assert codex.resolve_codex_command() == str(flat)
    monkeypatch.setattr(codex.sys, "platform", "linux")
    with pytest.raises(codex.CodexUnavailable):
        codex.resolve_codex_command()


def test_unrecognized_cli_fails_before_protocol_or_model(client, monkeypatch):
    monkeypatch.setattr(codex.shutil, "which", lambda _: "codex.exe")
    monkeypatch.setattr(codex.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="not codex"))
    with pytest.raises(codex.CodexUnavailable, match="version"):
        client.preflight()
    assert FakeSession.instances == []


def test_default_model_uses_reported_cli_selection(client):
    turn = client.complete(model="", system="role", messages=[], tools=TOOLS)
    session = FakeSession.instances[0]
    assert "model" not in next(p for m, p in session.sent if m == "thread/start")
    assert next(p for m, p in session.sent if m == "turn/start")["model"] == "cli-default-model"
    assert turn.model == "cli-default-model"


def test_explicitly_uninvoked_failures_are_known_zero(client):
    FakeSession.thread_change = {"sandbox": {"type": "dangerFullAccess"}}
    with pytest.raises(ModelError) as caught:
        complete(client)
    assert caught.value.provider_invoked is False
    assert caught.value.usage == Usage(0, 0)
    assert caught.value.usage_status == "known"


def test_protocol_compatible_new_cli_version_is_not_number_blocked(client, monkeypatch):
    monkeypatch.setattr(codex.shutil, "which", lambda _: "codex.exe")
    monkeypatch.setattr(codex.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="codex-cli 0.156.0"))
    checks = []
    monkeypatch.setattr(codex, "_verify_schema", lambda *a: checks.append(True))
    assert client.preflight()["version"] == "0.156.0"
    assert checks == [True]


def test_global_instruction_source_is_explicitly_whitelisted_but_repository_is_not(tmp_path):
    cwd = tmp_path / "empty"
    global_file = tmp_path / "codex-home" / "AGENTS.md"
    result = {"sandbox": {"type": "readOnly"}, "approvalPolicy": "never", "modelProvider": "openai", "model": "fixture",
              "cwd": str(cwd), "runtimeWorkspaceRoots": [], "instructionSources": [str(global_file)],
              "thread": {"id": "fresh", "ephemeral": True, "environments": [], "turns": []}}
    assert codex._verify_thread(result, cwd, "", {global_file.resolve()}) == "fresh"
    result["instructionSources"].append(str(tmp_path / "repo" / "AGENTS.md"))
    with pytest.raises(codex.CodexUnavailable):
        codex._verify_thread(result, cwd, "", {global_file.resolve()})


def test_schema_missing_isolation_field_fails_closed(tmp_path, monkeypatch):
    schema_path = tmp_path / "schema" / "v2"
    schema_path.mkdir(parents=True)
    (schema_path / "ThreadStartParams.json").write_text('{"properties":{"model":{}}}', encoding="utf-8")
    monkeypatch.setattr(codex.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    with pytest.raises(codex.CodexUnavailable, match="required isolation"):
        codex._verify_schema("codex", tmp_path)


def test_metadata_projection_never_returns_credentials():
    result = codex._metadata({"api_key": "SECRET", "mcp_servers": {
        "local": {"enabled": False, "env": {"TOKEN": "SECRET"}, "url": "SECRET"}},
        "developer_instructions": "SECRET", "features": {}})
    assert "SECRET" not in json.dumps(result)
    assert result["mcp_enabled"] == {"local": False}


@pytest.mark.parametrize("name", ['name.with.dot', 'name"quote', 'name\\path'])
def test_unsafe_mcp_override_path_refused(name):
    with pytest.raises(codex.CodexUnavailable):
        codex._overrides((name,))


def test_every_server_request_is_declined_without_echoing_payload():
    instance = codex._StdioSession.__new__(codex._StdioSession)
    sent = []
    instance.send = sent.append
    for method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/tool/call", "unknown"):
        instance._deny({"id": 7, "method": method, "params": {"secret": "DO_NOT_ECHO"}})
    assert all(x.get("result", {}).get("decision") == "decline" or x.get("error", {}).get("code") == -32601 for x in sent)
    assert "DO_NOT_ECHO" not in json.dumps(sent)
    assert instance.denied_request


def test_stdio_protocol_correlates_responses_denies_callbacks_and_strips_development_env(monkeypatch, tmp_path):
    messages = [
        {"method": "thread/started", "params": {"threadId": "unrelated"}},
        {"id": 99, "method": "item/commandExecution/requestApproval", "params": {"command": "do not execute"}},
        {"id": 1, "result": {"userAgent": "synthetic"}},
    ]
    stdout = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n")
    stdin = io.StringIO()
    captured = {}
    def popen(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return SimpleNamespace(stdin=stdin, stdout=stdout, poll=lambda: 0, wait=lambda **k: 0)
    monkeypatch.setattr(codex.subprocess, "Popen", popen)
    monkeypatch.setenv("CODEX_THREAD_ID", "development-thread")
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-forward")
    session = codex._StdioSession("codex", tmp_path, codex._overrides(("local",)), 1)
    try:
        assert session.initialize() == {"userAgent": "synthetic"}
        assert session.event()["method"] == "thread/started"
        replies = [json.loads(line) for line in stdin.getvalue().splitlines()]
        assert replies[1] == {"id": 99, "result": {"decision": "decline"}}
        assert "CODEX_THREAD_ID" not in captured["env"]
        assert "OPENAI_API_KEY" not in captured["env"]
        assert captured["stderr"] == codex.subprocess.DEVNULL
        assert captured["argv"][:3] == ["codex", "app-server", "--stdio"]
        assert captured["cwd"] == tmp_path
    finally:
        session.close()


def test_rpc_error_does_not_leak_arbitrary_server_body():
    session = codex._StdioSession.__new__(codex._StdioSession)
    session.next_id, session.pending = 0, []
    session.send = lambda _: None
    session._receive = lambda: {"id": 1, "error": {"message": "SECRET credential echo"}}
    with pytest.raises(ModelError) as caught:
        session.request("initialize", {})
    assert "SECRET" not in str(caught.value)
