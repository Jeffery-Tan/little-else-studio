"""DeepSeek transport; thinking stays private when a role enables it.

Protocol checked against official thinking_mode and create-chat-completion docs
on 2026-09-19. Never retries a request or presents reasoning as assistant text.
"""
from __future__ import annotations

import json
import re
import threading
import httpx

from .client import JobCancelled, ModelError
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


def _stream_turn(lines, model, *, require_reasoning, on_progress=None, check_cancel=None):
    """Assemble public text and private replay fields from one complete SSE turn.

    Only `delta.content` can produce observer progress. Partial tool JSON and
    reasoning remain local until the complete response validates in `_parse`.
    """
    content, reasoning, calls = [], [], {}
    finish, usage, actual_model, done = None, None, model, False
    preview_count, last_preview_size = 0, 0
    try:
        for line in lines:
            if check_cancel:
                check_cancel()
            if not line.startswith('data:'):
                continue
            value = line[5:].strip()
            if value == '[DONE]':
                done = True
                break
            chunk = json.loads(value)
            choices = chunk['choices']
            if not isinstance(choices, list) or len(choices) != 1 or choices[0].get('index') != 0:
                raise ValueError('invalid stream choice')
            choice = choices[0]
            delta = choice.get('delta') or {}
            if not isinstance(delta, dict):
                raise ValueError('invalid stream delta')
            public = delta.get('content')
            private = delta.get('reasoning_content')
            if public is not None:
                if not isinstance(public, str):
                    raise ValueError('invalid public delta')
                content.append(public)
                rendered = ''.join(content)
                if (on_progress and preview_count < 16 and len(rendered) - last_preview_size >= 12
                        and (rendered.endswith(('。', '！', '？', '.', '!', '?', '\n'))
                             or len(rendered) - last_preview_size >= 120)):
                    on_progress({'type': 'agent_narration_preview', 'text': rendered[-500:]})
                    preview_count += 1
                    last_preview_size = len(rendered)
            if private is not None:
                if not isinstance(private, str):
                    raise ValueError('invalid private delta')
                reasoning.append(private)
            for piece in delta.get('tool_calls') or []:
                index = piece.get('index')
                if type(index) is not int or not 0 <= index < 16:
                    raise ValueError('invalid tool index')
                assembled = calls.setdefault(index, {'id': '', 'type': 'function',
                                                       'function': {'name': '', 'arguments': ''}})
                if piece.get('type') not in (None, 'function'):
                    raise ValueError('invalid tool type')
                for field in ('id',):
                    fragment = piece.get(field)
                    if fragment is not None:
                        if not isinstance(fragment, str) or (assembled[field] and assembled[field] != fragment):
                            raise ValueError('invalid tool id')
                        assembled[field] = fragment
                function = piece.get('function') or {}
                for field in ('name', 'arguments'):
                    fragment = function.get(field)
                    if fragment is not None:
                        if not isinstance(fragment, str):
                            raise ValueError('invalid tool fragment')
                        if field == 'name':
                            if assembled['function'][field] and assembled['function'][field] != fragment:
                                raise ValueError('invalid tool name')
                            assembled['function'][field] = fragment
                        else:
                            assembled['function'][field] += fragment
                            if len(assembled['function'][field]) > 1_000_000:
                                raise ValueError('tool arguments too large')
            if choice.get('finish_reason') is not None:
                if finish is not None:
                    raise ValueError('duplicate stream finish')
                finish = choice['finish_reason']
            if chunk.get('usage') is not None:
                usage = chunk['usage']
            if isinstance(chunk.get('model'), str) and chunk['model']:
                actual_model = chunk['model']
    except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ModelError(200, 'invalid DeepSeek stream; usage unconfirmed') from None
    if not done or finish is None:
        raise ModelError(0, 'DeepSeek stream ended before a complete response; usage unconfirmed')
    if calls and sorted(calls) != list(range(len(calls))):
        raise ModelError(200, 'invalid DeepSeek tool stream; usage unconfirmed')
    message = {'content': ''.join(content),
               'tool_calls': [calls[i] for i in sorted(calls)]}
    if reasoning:
        message['reasoning_content'] = ''.join(reasoning)
    return _parse({'model': actual_model, 'choices': [{'finish_reason': finish,
        'message': message}], 'usage': usage or {}},
        model, require_reasoning=require_reasoning)


class DeepSeekClient:
    def __init__(self, token, *, max_tokens=None, timeout=180.0, check_cancel=None,
                 cancel_event=None, reasoning_effort='high'):
        if (not isinstance(token, str) or not token or not token.isascii()
                or any(character.isspace() or ord(character) < 33 or ord(character) == 127 for character in token)):
            raise ValueError("DeepSeek API key must contain ASCII characters without whitespace or control characters")
        if max_tokens is not None and (type(max_tokens) is not int or not 1 <= max_tokens <= 393216):
            raise ValueError("DeepSeek max_tokens must be an integer between 1 and 393216")
        if reasoning_effort not in ('none', 'low', 'high'):
            raise ValueError('DeepSeek reasoning_effort must be none, low or high')
        self._token = token
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.check_cancel = check_cancel
        self.cancel_event = cancel_event

    def complete_with_progress(self, *, model, system, messages, tools, on_progress=None):
        payload = self._payload(model, system, messages, tools)
        payload['stream'] = True
        if self.check_cancel:
            self.check_cancel()
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                with client.stream('POST', ENDPOINT,
                                   headers={'Authorization': f'Bearer {self._token}'}, json=payload) as response:
                    if response.status_code != 200:
                        raise ModelError(response.status_code, 'request rejected; usage unconfirmed')
                    done = threading.Event()
                    def stop_reader():
                        while not done.wait(.1):
                            if self.cancel_event.is_set():
                                try:
                                    response.close()
                                except (RuntimeError, httpx.TransportError):
                                    pass
                                return
                    monitor = (threading.Thread(target=stop_reader, daemon=True)
                               if self.cancel_event is not None else None)
                    if monitor:
                        monitor.start()
                    try:
                        turn = _stream_turn(response.iter_lines(), model,
                                            require_reasoning=bool(tools) and self.reasoning_effort != 'none',
                                            on_progress=on_progress, check_cancel=self.check_cancel)
                        if self.check_cancel:
                            try:
                                self.check_cancel()
                            except JobCancelled as exc:
                                raise JobCancelled(str(exc), usage=turn.usage, usage_final=True) from exc
                        return turn
                    finally:
                        done.set()
                        if monitor:
                            monitor.join(timeout=.2)
        except httpx.TransportError as exc:
            if self.check_cancel:
                self.check_cancel()
            raise ModelError(0, f'transport failed ({type(exc).__name__}); usage unconfirmed') from None
        except (ModelError, JobCancelled):
            raise
        except RuntimeError as exc:
            if self.check_cancel:
                self.check_cancel()
            raise ModelError(0, f'stream failed ({type(exc).__name__}); usage unconfirmed') from None

    def _payload(self, model, system, messages, tools):
        # The provider's discovered model list may grow or retain an existing
        # alias. Validate the wire identifier, not a frozen release catalog.
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
            raise ModelError(0, "DeepSeek model must be a valid identifier of 1 to 200 characters", provider_invoked=False)
        try:
            thinking = self.reasoning_effort != 'none'
            payload = {"model": model,
                       "messages": (_messages(system, messages, with_tools=bool(tools)) if thinking
                                    else openai_messages(system, messages)),
                       "thinking": {"type": "enabled" if thinking else "disabled"},
                       "stream": False}
            if thinking:
                payload['reasoning_effort'] = self.reasoning_effort
            # Omit the optional limit to use the provider's thinking-mode default.
            if self.max_tokens is not None:
                payload["max_tokens"] = self.max_tokens
            if tools:
                payload["tools"] = [{"type": "function", "function": {
                    "name": tool["name"], "description": tool.get("description", ""),
                    "parameters": tool["input_schema"]}} for tool in tools]
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ModelError(0, "invalid DeepSeek request structure; request not sent", provider_invoked=False) from None
        return payload

    def complete(self, *, model, system, messages, tools):
        payload = self._payload(model, system, messages, tools)
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                response = client.post(ENDPOINT, headers={"Authorization": f"Bearer {self._token}"}, json=payload)
                if response.status_code != 200:
                    raise ModelError(response.status_code, "request rejected; usage unconfirmed")
                try:
                    data = response.json()
                except ValueError:
                    raise ModelError(200, "invalid JSON response; usage unconfirmed") from None
                return _parse(data, model,
                              require_reasoning=bool(tools) and self.reasoning_effort != 'none')
        except httpx.TransportError as exc:
            raise ModelError(0, f"transport failed ({type(exc).__name__}); usage unconfirmed") from None
