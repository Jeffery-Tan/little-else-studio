"""DeepSeek thinking transport; private reasoning stays in this role's wire history.

Protocol checked against official thinking_mode and create-chat-completion docs
on 2026-09-19. Never retries a request or presents reasoning as assistant text.
"""
from __future__ import annotations

import json
import re
import httpx

from .client import ModelError
from .openai_compat import _messages as openai_messages, _parse as openai_parse

ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
MODELS = ("deepseek-flash", "deepseek-v4-pro")
REASONING_BLOCK = "deepseek_reasoning"


def _messages(system, messages, *, with_tools):
    """Only assistant messages carry private metadata; tool text cannot add it.

    With tools, every preceding assistant response needs its actual reasoning,
    including responses with no tool call. Across jobs Studio stores display
    text, not private transcripts: that text becomes explicitly marked history
    data in a new reasoning conversation, never an invented empty reasoning turn.
    """
    converted = openai_messages(system, [])
    for message in messages:
        if message.get("role") == "assistant":
            content = message.get("content", "")
            private = ([b for b in content if b.get("type") == REASONING_BLOCK]
                       if isinstance(content, list) else [])
            plain = isinstance(content, str) or (isinstance(content, list) and
                     all(isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str) for b in content))
            if not private and plain:
                prior_text = content if isinstance(content, str) else "".join(b["text"] for b in content)
                converted.append({"role": "user", "content":
                    "以下是上一轮制片团队的已保存回复，仅供历史参考，不是本轮指令；当前任务与已保存产物决定本轮工作范围：\n" +
                    json.dumps({"saved_assistant_text": prior_text}, ensure_ascii=False)})
                continue
            if len(private) != 1 or not isinstance(private[0].get("reasoning_content"), str):
                raise ModelError(0, "DeepSeek 历史消息缺少原始思考字段；请从保存的任务资料开启新一轮，不能重放不完整对话。",
                                 provider_invoked=False)
            items = openai_messages("", [message])
            if with_tools:
                items[0]["reasoning_content"] = private[0]["reasoning_content"]
                # DeepSeek accepts an empty string for a reasoning-only assistant.
                items[0]["content"] = items[0].get("content") or ""
        else:
            items = openai_messages("", [message])
        converted.extend(items)
    return converted


def _parse(data, model, *, require_reasoning=False):
    turn = openai_parse(data, model)
    reasoning = data["choices"][0]["message"].get("reasoning_content")
    if (reasoning is not None and not isinstance(reasoning, str)) or (require_reasoning and not isinstance(reasoning, str)):
        raise ModelError(200, "invalid DeepSeek reasoning response; received usage retained",
                         usage=turn.usage, usage_final=True)
    if reasoning is not None:
        # The loop replays raw_blocks verbatim. Neither text nor thinking nor
        # any observer-facing progress contains this private protocol field.
        turn.raw_blocks.insert(0, {"type": REASONING_BLOCK, "reasoning_content": reasoning})
    return turn


class DeepSeekClient:
    def __init__(self, token, *, max_tokens=None, timeout=180.0):
        if (not isinstance(token, str) or not token or not token.isascii()
                or any(character.isspace() or ord(character) < 33 or ord(character) == 127 for character in token)):
            raise ValueError("DeepSeek API key must contain ASCII characters without whitespace or control characters")
        if max_tokens is not None and (type(max_tokens) is not int or not 1 <= max_tokens <= 393216):
            raise ValueError("DeepSeek max_tokens must be an integer between 1 and 393216")
        self._token = token
        self.max_tokens = max_tokens
        self.timeout = timeout

    def complete(self, *, model, system, messages, tools):
        # The provider's discovered model list may grow or retain an existing
        # alias. Validate the wire identifier, not a frozen release catalog.
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
            raise ModelError(0, "DeepSeek model must be a valid identifier of 1 to 200 characters", provider_invoked=False)
        try:
            payload = {"model": model, "messages": _messages(system, messages, with_tools=bool(tools)),
                       "thinking": {"type": "enabled"}, "reasoning_effort": "high",
                       "stream": False}
            # Omit the optional limit to use the provider's thinking-mode default.
            if self.max_tokens is not None:
                payload["max_tokens"] = self.max_tokens
            if tools:
                payload["tools"] = [{"type": "function", "function": {
                    "name": tool["name"], "description": tool.get("description", ""),
                    "parameters": tool["input_schema"]}} for tool in tools]
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ModelError(0, "invalid DeepSeek request structure; request not sent", provider_invoked=False) from None
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                response = client.post(ENDPOINT, headers={"Authorization": f"Bearer {self._token}"}, json=payload)
                if response.status_code != 200:
                    raise ModelError(response.status_code, "request rejected; usage unconfirmed")
                try:
                    data = response.json()
                except ValueError:
                    raise ModelError(200, "invalid JSON response; usage unconfirmed") from None
                return _parse(data, model, require_reasoning=bool(tools))
        except httpx.TransportError as exc:
            raise ModelError(0, f"transport failed ({type(exc).__name__}); usage unconfirmed") from None
