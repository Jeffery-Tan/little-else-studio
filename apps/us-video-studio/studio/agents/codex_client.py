"""Independent Codex app-server transport for Studio's existing tool loop.

Each complete() creates a fresh ephemeral thread in an empty temporary cwd.
Codex's own tools are disabled where supported; read-only sandboxing and denied
approval callbacks are defense in depth. Only structured *requests* are returned
as ToolCall objects: Studio dispatch remains the only business-tool executor.

Protocol reviewed against codex-cli 0.155.0-alpha.9.2, 2026-09-19. This is not an
OS read-isolation guarantee or proof of model behavior. No auth files are read,
copied or supplied by this module. The CLI uses its existing ChatGPT login.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable
import urllib.request
from urllib.parse import urlsplit

from .client import JobCancelled, ModelError, SafePreflightError, ToolCall, Turn, Usage


AUDITED_VERSION = "0.155.0-alpha.9.2"
DISABLED_FEATURES = (
    "hooks", "plugins", "apps", "shell_tool", "unified_exec", "code_mode",
    "code_mode_host", "browser_use", "browser_use_external", "computer_use",
    "image_generation", "view_image", "multi_agent", "multi_agent_v2", "goals",
    "memories", "shell_snapshot", "skill_search", "skill_mcp_dependency_install",
    "sleep_tool", "tool_suggest", "remote_plugin", "workspace_dependencies",
    "in_app_local_automation", "unbounded_connection_retries",
)
SAFE_CONFIG: dict[str, Any] = {
    **{f"features.{name}": False for name in DISABLED_FEATURES},
    "features.skip_host_skill_discovery": True,
    "web_search": "disabled", "sandbox_mode": "read-only",
    "approval_policy": "never", "approvals_reviewer": "user",
    "model_provider": "openai", "forced_login_method": "chatgpt",
    "project_doc_max_bytes": 0, "developer_instructions": "",
    "agents.enabled": False,
}
MAX_LINE = 8 * 1024 * 1024


class CodexUnavailable(ModelError, SafePreflightError):
    """A local preflight failure: no model request was sent."""

    def __init__(self, message: str):
        super().__init__(0, message, provider_invoked=False)


def _desktop_codex_candidates() -> list[Path]:
    """Discover only this Windows user's installed desktop CLI, not workspace files."""
    local = os.environ.get("LOCALAPPDATA", "")
    if not local or not Path(local).is_absolute():
        return []
    root = (Path(local) / "OpenAI" / "Codex" / "bin").resolve()
    try:
        versioned = [p / "codex.exe" for p in root.iterdir()
                     if p.is_dir() and re.fullmatch(r"[0-9a-f]{8,64}", p.name)]
    except OSError:
        return []
    candidates = []
    for path in versioned:
        try:
            if path.is_file() and path.resolve().is_relative_to(root):
                candidates.append((path.stat().st_mtime_ns, str(path), path))
        except OSError:
            continue  # An installer may be replacing a version while we enumerate.
    result = [item[2] for item in sorted(candidates, reverse=True)]
    flat = root / "codex.exe"
    if flat.is_file() and flat.resolve().is_relative_to(root):
        result.append(flat)
    return result


def resolve_codex_command(command: str = "codex") -> str:
    executable = shutil.which(command)
    if executable:
        return executable
    # Do not silently replace an explicitly requested executable or custom command.
    if sys.platform == "win32" and command in {"codex", "codex.exe"}:
        candidates = _desktop_codex_candidates()
        if candidates:
            return str(candidates[0])
    raise CodexUnavailable("Codex CLI is not installed or not on PATH")


class _CancelledBeforeInvocation(JobCancelled, ModelError):
    def __init__(self):
        ModelError.__init__(self, 0, "Cancelled before Codex model invocation", provider_invoked=False)


def _toml(value: Any) -> str:
    # All generated overrides are booleans, integers or strings, whose JSON
    # spellings are also TOML scalars. Never serialise credentials here.
    return json.dumps(value, ensure_ascii=False)


def _overrides(names: tuple[str, ...]) -> dict[str, Any]:
    config = dict(SAFE_CONFIG)
    for name in names:
        # CLI dotted overrides do not support quoted path components reliably.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise CodexUnavailable("MCP name cannot be safely disabled by this CLI adapter")
        config[f"mcp_servers.{name}.enabled"] = False
    return config


def _metadata(config: dict[str, Any]) -> dict[str, Any]:
    """Return only safe policy fields; never retain/log a config/read response."""
    servers = config.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise CodexUnavailable("Codex MCP metadata shape is unsupported")
    return {
        "mcp_enabled": {name: value.get("enabled", True) for name, value in servers.items()},
        "features": {name: config.get("features", {}).get(name) for name in DISABLED_FEATURES},
        "skip_host_skill_discovery": config.get("features", {}).get("skip_host_skill_discovery"),
        "web_search": config.get("web_search"),
        "sandbox_mode": config.get("sandbox_mode"),
        "approval_policy": config.get("approval_policy"),
        "model_provider": config.get("model_provider"),
        "forced_login_method": config.get("forced_login_method"),
        "project_doc_max_bytes": config.get("project_doc_max_bytes"),
        "developer_instructions_empty": config.get("developer_instructions") == "",
        "agents_disabled": config.get("agents", {}).get("enabled") is False,
    }


def _verify_policy(metadata: dict[str, Any]) -> None:
    valid = (
        all(value is False for value in metadata["mcp_enabled"].values())
        and all(value is False for value in metadata["features"].values())
        and metadata["skip_host_skill_discovery"] is True
        and metadata["web_search"] == "disabled"
        and metadata["sandbox_mode"] == "read-only"
        and metadata["approval_policy"] == "never"
        and metadata["model_provider"] == "openai"
        and metadata["forced_login_method"] == "chatgpt"
        and metadata["project_doc_max_bytes"] == 0
        and metadata["developer_instructions_empty"]
        and metadata["agents_disabled"]
    )
    if not valid:
        raise CodexUnavailable("Codex effective policy differs from Studio's restricted policy; no model request sent")


def _codex_child_env(parent: dict[str, str], system_https_proxy: str | None) -> dict[str, str]:
    env = {k: v for k, v in parent.items()
           if not ((k.upper().startswith("CODEX_") and k.upper() != "CODEX_HOME")
                   or k.upper() in {"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"})}
    # On Windows the HTTPS client may use the user system proxy while Codex's
    # WebSocket transport goes direct. Give only this child the already enabled
    # local HTTP proxy; never replace an explicit proxy or use a remote one.
    if {k.upper() for k in env} & {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "WS_PROXY", "WSS_PROXY"}:
        return env
    if not system_https_proxy:
        return env
    raw = system_https_proxy if "://" in system_https_proxy else f"http://{system_https_proxy}"
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme.lower() != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or not parsed.port or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            return env
    except ValueError:
        return env
    env["HTTPS_PROXY"] = f"http://{parsed.netloc}"
    if not any(k.upper() == "NO_PROXY" for k in env):
        env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    return env


class _StdioSession:
    """Bounded JSON-RPC pipe; errors never expose server bodies or stderr."""

    def __init__(self, command: str, cwd: Path, config: dict[str, Any], timeout: float):
        argv = [command, "app-server", "--stdio"]
        for key, value in config.items():
            argv += ["-c", f"{key}={_toml(value)}"]
        # Do not forward development-thread identities or API-key credentials.
        # CLI authentication stays in its own supported login mechanism.
        system_proxy = None
        if sys.platform == "win32":
            try:
                system_proxy = urllib.request.getproxies_registry().get("https")
            except (OSError, ValueError):
                pass
        env = _codex_child_env(os.environ, system_proxy)
        self.process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.messages: queue.Queue = queue.Queue()
        self.pending: list[dict[str, Any]] = []
        self.next_id = 0
        self.denied_request = False
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()

    def _pump(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    self.messages.put(None)
                    return
                try:
                    event = json.loads(line)
                except ValueError:
                    self.messages.put(None)
                    return
                self.messages.put(event if isinstance(event, dict) else None)
        finally:
            self.messages.put(None)

    def send(self, message: dict[str, Any]):
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise ModelError(0, "Codex pipe closed; usage unconfirmed") from exc

    def _receive(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ModelError(0, "Codex request timed out; usage unconfirmed")
        try:
            message = self.messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise ModelError(0, "Codex request timed out; usage unconfirmed") from exc
        if message is None:
            raise ModelError(0, "Codex process or protocol ended unexpectedly; usage unconfirmed")
        return message

    def _deny(self, message):
        self.denied_request = True
        if message["method"] in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            reply = {"id": message["id"], "result": {"decision": "decline"}}
        else:
            reply = {"id": message["id"], "error": {"code": -32601, "message": "Studio does not expose native tools or approvals"}}
        self.send(reply)

    def request(self, method: str, params: dict[str, Any]):
        self.next_id += 1
        request_id = self.next_id
        self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = self._receive()
            if "id" in message and "method" in message:
                self._deny(message)
            elif message.get("id") == request_id:
                if "error" in message:
                    raise ModelError(0, "Codex rejected the protocol request; response details withheld")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise ModelError(0, "Codex returned an invalid protocol result")
                return result
            else:
                self.pending.append(message)

    def event(self):
        while True:
            message = self.pending.pop(0) if self.pending else self._receive()
            if "id" in message and "method" in message:
                self._deny(message)
                continue
            return message

    def event_poll(self, interval=0.25):
        """Read one event without hiding a user cancellation behind a long wait."""
        while True:
            if self.pending:
                message = self.pending.pop(0)
            else:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise ModelError(0, "Codex request timed out; usage unconfirmed")
                try:
                    message = self.messages.get(timeout=min(interval, remaining))
                except queue.Empty:
                    raise TimeoutError() from None
                if message is None:
                    raise ModelError(0, "Codex process or protocol ended unexpectedly; usage unconfirmed")
            if "id" in message and "method" in message:
                self._deny(message)
                continue
            return message

    def initialize(self):
        result = self.request("initialize", {
            "clientInfo": {"name": "us_video_studio", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        })
        self.send({"method": "initialized", "params": {}})
        return result

    def policy_metadata(self):
        response = self.request("config/read", {"includeLayers": False})
        return _metadata(response.get("config", {}))

    def close(self):
        # On a timeout/pipe failure, the caller reports unknown usage and never
        # retries. Normal user cancellation is checked before the next call;
        # it does not force-kill an otherwise healthy in-flight model request.
        try:
            self.process.stdin.close()
        except OSError:
            pass
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process.stdout.close()
        self.reader.join(timeout=1)


def _schema(tools: list[dict[str, Any]]) -> dict[str, Any]:
    names = [tool.get("name") for tool in tools]
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise CodexUnavailable("Studio tool definitions need unique names")
    call = {"type": "object", "properties": {
        "id": {"type": "string"},
        "name": {"type": "string", **({"enum": names} if names else {})},
        "arguments_json": {"type": "string"},
    }, "required": ["id", "name", "arguments_json"], "additionalProperties": False}
    return {"type": "object", "properties": {
        "text": {"type": "string"},
        "tool_calls": {"type": "array", "items": call, "maxItems": 16 if names else 0},
    }, "required": ["text", "tool_calls"], "additionalProperties": False}


def _decode_turn(text: str, tools, model: str, usage: Usage) -> Turn:
    try:
        value = json.loads(text)
        if not isinstance(value, dict) or set(value) != {"text", "tool_calls"}:
            raise ValueError()
        if not isinstance(value["text"], str) or not isinstance(value["tool_calls"], list) or len(value["tool_calls"]) > 16:
            raise ValueError()
        names = {tool["name"] for tool in tools}
        calls, ids, blocks = [], set(), []
        if value["text"]:
            blocks.append({"type": "text", "text": value["text"]})
        for call in value["tool_calls"]:
            if not isinstance(call, dict) or set(call) != {"id", "name", "arguments_json"}:
                raise ValueError()
            if not isinstance(call["id"], str) or not call["id"] or call["id"] in ids or call["name"] not in names:
                raise ValueError()
            arguments = json.loads(call["arguments_json"])
            if not isinstance(arguments, dict):
                raise ValueError()
            ids.add(call["id"])
            calls.append(ToolCall(call["id"], call["name"], arguments))
            blocks.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": arguments})
        return Turn(text=value["text"], tool_calls=calls, stop_reason="tool_use" if calls else "end_turn",
                    usage=usage, model=model, raw_blocks=blocks)
    except (ValueError, KeyError, TypeError) as exc:
        raise ModelError(200, "Codex structured output is invalid; no tools dispatched", usage=usage) from exc


def _global_instruction_sources(initialized: dict[str, Any]) -> set[Path]:
    home = initialized.get("codexHome")
    return {(Path(home) / "AGENTS.md").resolve()} if isinstance(home, str) and Path(home).is_absolute() else set()


def _verify_thread(result: dict[str, Any], cwd: Path, model: str,
                   allowed_instruction_sources: set[Path] | None = None) -> str:
    thread = result.get("thread", {})
    sources = {Path(p).resolve() for p in result.get("instructionSources", [])}
    if not (result.get("sandbox", {}).get("type") == "readOnly"
            and result.get("sandbox", {}).get("networkAccess", False) is False
            and result.get("approvalPolicy") == "never"
            and result.get("modelProvider") == "openai"
            and isinstance(result.get("model"), str) and bool(result["model"])
            and (not model or result["model"] == model)
            and Path(result.get("cwd", "")).resolve() == cwd.resolve()
            and result.get("runtimeWorkspaceRoots") in ([], [str(cwd)])
            and sources <= (allowed_instruction_sources or set())
            and thread.get("ephemeral") is True and thread.get("environments") == []
            and not thread.get("forkedFromId") and not thread.get("parentThreadId")
            and not thread.get("turns") and isinstance(thread.get("id"), str)):
        raise CodexUnavailable("Codex did not establish the requested fresh, read-only Studio thread")
    return thread["id"]


def _verify_schema(command: str, directory: Path):
    """Check the local binary's contract, rather than blocking every upgrade."""
    output = directory / "schema"
    result = subprocess.run([command, "app-server", "generate-json-schema", "--experimental", "--out", str(output)],
                            capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise CodexUnavailable("Codex cannot report its local protocol schema")
    required = {
        "ThreadStartParams": {"model", "ephemeral", "environments", "baseInstructions", "developerInstructions",
                              "sandbox", "approvalPolicy", "dynamicTools", "runtimeWorkspaceRoots", "selectedCapabilityRoots"},
        "ThreadStartResponse": {"model", "modelProvider", "sandbox", "approvalPolicy", "cwd", "thread",
                                "instructionSources", "runtimeWorkspaceRoots"},
        "TurnStartParams": {"threadId", "input", "outputSchema", "sandboxPolicy", "environments"},
    }
    try:
        for name, keys in required.items():
            schema = json.loads((output / "v2" / f"{name}.json").read_text(encoding="utf-8"))
            if not keys <= schema.get("properties", {}).keys():
                raise CodexUnavailable("Codex protocol lacks required isolation or structured-output fields")
    except (OSError, ValueError, TypeError) as exc:
        raise CodexUnavailable("Codex local protocol schema is unsupported") from exc


class CodexClient:
    def __init__(self, *, run_id: str, role: str, data_root: str | Path,
                 command: str = "codex", timeout: float = 180.0,
                 check_cancel: Callable[[], None] | None = None):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", run_id) or not re.fullmatch(r"[a-z][a-z_]*", role):
            raise CodexUnavailable("Invalid Studio run or role identity")
        self.run_id, self.role = run_id, role
        self.data_root = Path(data_root)  # Never used as the Codex working directory.
        self.command, self.timeout = command, timeout
        self.check_cancel = check_cancel
        self._mcp_names: tuple[str, ...] | None = None
        self._version = None

    def _check(self):
        if self.check_cancel:
            try:
                self.check_cancel()
            except JobCancelled as exc:
                raise _CancelledBeforeInvocation() from exc

    def preflight(self):
        """Only local CLI/config checks, before the ledger starts a model intent."""
        self._check()
        executable = resolve_codex_command(self.command)
        try:
            version = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                     encoding="utf-8", timeout=10,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            match = re.fullmatch(r"codex-cli ([0-9][A-Za-z0-9.+-]*)", version.stdout.strip())
            if version.returncode or not match:
                raise CodexUnavailable("Codex CLI version could not be determined")
            self._version = match[1]
            self.command = executable
            with tempfile.TemporaryDirectory(prefix="studio-codex-preflight-") as directory:
                _verify_schema(executable, Path(directory))
                session = _StdioSession(executable, Path(directory), _overrides(()), min(self.timeout, 30))
                try:
                    session.initialize()
                    names = tuple(session.policy_metadata()["mcp_enabled"])
                finally:
                    session.close()
                session = _StdioSession(executable, Path(directory), _overrides(names), min(self.timeout, 30))
                try:
                    session.initialize()
                    _verify_policy(session.policy_metadata())
                finally:
                    session.close()
            self._mcp_names = names
            return {"backend": "codex_cli", "version": self._version,
                    "restricted_policy_verified": True, "model_invoked": False}
        except (OSError, subprocess.SubprocessError, ModelError) as exc:
            if isinstance(exc, CodexUnavailable):
                raise
            raise CodexUnavailable("Codex local preflight failed; no model request sent") from exc

    def list_models(self) -> dict[str, Any]:
        """Read the local Codex picker catalog without starting a model turn."""
        self.preflight()
        models: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="studio-codex-models-") as directory:
                session = _StdioSession(self.command, Path(directory),
                                        _overrides(self._mcp_names or ()), min(self.timeout, 30))
                try:
                    session.initialize()
                    _verify_policy(session.policy_metadata())
                    for _ in range(5):
                        params: dict[str, Any] = {"limit": 100, "includeHidden": False}
                        if cursor:
                            params["cursor"] = cursor
                        page = session.request("model/list", params)
                        rows = page.get("data")
                        if not isinstance(rows, list):
                            raise CodexUnavailable("Codex model catalog has an unsupported shape")
                        for row in rows:
                            if not isinstance(row, dict):
                                raise CodexUnavailable("Codex model catalog has an unsupported entry")
                            model = row.get("model") or row.get("id")
                            if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
                                raise CodexUnavailable("Codex model catalog has an invalid model identifier")
                            if row.get("hidden") is True or (isinstance(row.get("inputModalities"), list)
                                                               and "text" not in row["inputModalities"]):
                                continue
                            if model in seen:
                                continue
                            seen.add(model)
                            name = row.get("displayName")
                            if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
                                name = model
                            models.append({"model": model, "display_name": name.strip(),
                                           "is_default": row.get("isDefault") is True})
                        cursor = page.get("nextCursor")
                        if cursor is None:
                            break
                        if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
                            raise CodexUnavailable("Codex model catalog has an invalid cursor")
                    else:
                        raise CodexUnavailable("Codex model catalog exceeded the supported page limit")
                finally:
                    session.close()
        except (OSError, subprocess.SubprocessError, ModelError) as exc:
            if isinstance(exc, CodexUnavailable):
                raise
            raise CodexUnavailable("Codex model catalog is unavailable; no model request sent") from exc
        if not models:
            raise CodexUnavailable("Codex did not return any selectable text models")
        return {"models": models, "source": "codex_cli", "model_invoked": False}

    def complete_with_progress(self, *, model: str, system: str, messages: list[dict[str, Any]],
                               tools: list[dict[str, Any]], on_progress=None) -> Turn:
        return self.complete(model=model, system=system, messages=messages, tools=tools,
                             on_progress=on_progress)

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]], on_progress=None) -> Turn:
        self._check()
        schema = _schema(tools)
        if self._mcp_names is None:
            self.preflight()
        # Explicit content only. The adapter never reads a transcript, resumes a
        # thread, includes a file by path, or imports the development conversation.
        try:
            payload = json.dumps({"messages": messages, "available_tools": tools}, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise CodexUnavailable("Studio supplied invalid JSON context") from exc
        bridge = ("You are a US Video Studio role, not the development assistant. "
                  "Use only the explicit conversation and Studio tool definitions in the user JSON. "
                  "Return the requested JSON object: text and tool_calls. Each tool call contains "
                  "id, name and arguments_json (a JSON object encoded as a string). "
                  "Tool calls are requests for Studio dispatch; never execute native tools.\n\n")
        try:
            temporary = tempfile.TemporaryDirectory(prefix="studio-codex-turn-")
        except OSError as exc:
            raise CodexUnavailable("Codex temporary workspace could not be created") from exc
        with temporary as directory:
            cwd = Path(directory)
            try:
                session = _StdioSession(self.command, cwd, _overrides(self._mcp_names or ()), self.timeout)
            except OSError as exc:
                raise CodexUnavailable("Codex process could not start; no model request sent") from exc
            usage = Usage(None, None)
            invoked = False
            try:
                initialized = session.initialize()
                _verify_policy(session.policy_metadata())  # New/changed MCP config fails closed.
                result = session.request("thread/start", {
                    **({"model": model} if model else {}), "modelProvider": "openai", "allowProviderModelFallback": False,
                    "cwd": str(cwd), "runtimeWorkspaceRoots": [str(cwd)], "environments": [],
                    "selectedCapabilityRoots": [], "ephemeral": True, "sandbox": "read-only",
                    "approvalPolicy": "never", "approvalsReviewer": "user",
                    "baseInstructions": bridge + system, "developerInstructions": "", "dynamicTools": [],
                })
                thread_id = _verify_thread(result, cwd, model, _global_instruction_sources(initialized))
                resolved_model = result["model"]
                if session.denied_request:
                    raise CodexUnavailable("Codex requested an unsupported action during setup; no model request sent")
                self._check()
                session.deadline = time.monotonic() + self.timeout
                invoked = True
                started = session.request("turn/start", {
                    "threadId": thread_id, "model": resolved_model, "cwd": str(cwd), "environments": [],
                    "runtimeWorkspaceRoots": [str(cwd)], "approvalPolicy": "never",
                    "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                    "input": [{"type": "text", "text": payload}], "outputSchema": schema,
                })
                turn_id = started.get("turn", {}).get("id")
                if not isinstance(turn_id, str) or not turn_id:
                    raise ModelError(200, "Codex did not return a turn identity")
                answer, seen_final, forbidden = "", False, False
                unphased_answer = None
                actual_model = resolved_model
                commentary_items = {}
                commentary_preview_size = {}
                interrupt_requested = False
                interrupt_deadline = None
                while True:
                    if self.check_cancel and not interrupt_requested:
                        try:
                            self.check_cancel()
                        except JobCancelled:
                            interrupt_requested = True
                            interrupt_deadline = time.monotonic() + 5
                            # The CLI's own turn stays isolated. The Studio
                            # goal revision is handled above this adapter.
                            try:
                                session.next_id += 1
                                session.send({"id": session.next_id, "method": "turn/interrupt",
                                              "params": {"threadId": thread_id, "turnId": turn_id}})
                            except ModelError:
                                raise JobCancelled('停止已请求；提供方中断回执未确认。', usage=usage) from None
                    if interrupt_requested and time.monotonic() >= interrupt_deadline:
                        raise JobCancelled('停止已请求；提供方中断回执未确认。', usage=usage)
                    try:
                        event = session.event_poll() if self.check_cancel and hasattr(session, 'event_poll') else session.event()
                    except TimeoutError:
                        continue
                    method, params = event.get("method"), event.get("params", {})
                    if params.get("threadId") != thread_id or params.get("turnId", params.get("turn", {}).get("id")) != turn_id:
                        continue
                    if method == "thread/tokenUsage/updated":
                        last = params.get("tokenUsage", {}).get("last", {})
                        for field, target in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens")):
                            value = last.get(field)
                            if type(value) is int and value >= 0:
                                setattr(usage, target, value)
                    elif method == "model/rerouted":
                        actual_model = params.get("toModel") or actual_model
                    elif method == "item/agentMessage/delta":
                        item_id, fragment = params.get('itemId'), params.get('delta')
                        if item_id in commentary_items and isinstance(fragment, str):
                            commentary_items[item_id] += fragment
                            visible = commentary_items[item_id]
                            shown = commentary_preview_size.get(item_id, 0)
                            if (on_progress is not None and len(visible) - shown >= 12
                                    and (visible.endswith(('。', '！', '？', '.', '!', '?', '\n'))
                                         or len(visible) - shown >= 120)
                                    and shown < 1600):
                                on_progress({'type': 'agent_narration_preview', 'text': visible[-500:]})
                                commentary_preview_size[item_id] = len(visible)
                    elif method in {"item/started", "item/completed"}:
                        item = params.get("item", {})
                        if item.get("type") not in {"userMessage", "agentMessage", "reasoning"}:
                            forbidden = True
                        if (method == 'item/started' and item.get('type') == 'agentMessage'
                                and item.get('phase') == 'commentary' and isinstance(item.get('id'), str)):
                            commentary_items[item['id']] = ''
                        if (method == "item/completed" and item.get("type") == "agentMessage"
                                and item.get("phase") == "commentary" and isinstance(item.get("text"), str)
                                and item["text"].strip() and on_progress is not None):
                            commentary_items.pop(item.get('id'), None)
                            on_progress({"type": "agent_narration", "text": item["text"].strip()[:1000]})
                        if method == "item/completed" and item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                            if seen_final:
                                raise ModelError(200, "Codex returned multiple final answers", usage=usage)
                            answer, seen_final = item.get("text", ""), True
                        elif method == "item/completed" and item.get("type") == "agentMessage" and item.get("phase") is None:
                            # The protocol explicitly permits legacy providers
                            # without phases. Only their last message at a
                            # completed turn is a candidate structured answer.
                            unphased_answer = item.get("text")
                    elif method == "turn/completed":
                        if interrupt_requested:
                            raise JobCancelled('旧轮已中断；用量按已收到的记录保留。', usage=usage)
                        if params.get("turn", {}).get("status") != "completed":
                            raise ModelError(200, "Codex turn did not complete; no tools dispatched", usage=usage)
                        break
                if forbidden or session.denied_request:
                    raise ModelError(200, "Codex attempted a native tool; no Studio tools dispatched", usage=usage)
                if not seen_final and isinstance(unphased_answer, str):
                    answer, seen_final = unphased_answer, True
                if not seen_final:
                    raise ModelError(200, "Codex final structured answer missing", usage=usage)
                return _decode_turn(answer, tools, actual_model, usage)
            except ModelError as exc:
                if not invoked:
                    if isinstance(exc, (CodexUnavailable, _CancelledBeforeInvocation)):
                        raise
                    raise CodexUnavailable("Codex setup failed before model invocation") from exc
                raise ModelError(exc.status, exc.message, usage=usage, provider_invoked=True) from exc
            except (ValueError, TypeError, KeyError) as exc:
                raise ModelError(0, "Codex returned an unsupported protocol shape", usage=usage,
                                 provider_invoked=invoked) from exc
            finally:
                session.close()
