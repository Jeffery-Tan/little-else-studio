"""One-attempt Chat Completions transport translated to Studio tool messages."""
from __future__ import annotations

import json
import httpx

from .client import DEFAULT_MAX_TOKENS, ModelError, ToolCall, Turn, Usage


def _messages(system, messages):
    converted = [{'role': 'system', 'content': system}] if system else []
    for message in messages:
        content = message.get('content', '')
        if isinstance(content, str):
            converted.append({'role': message['role'], 'content': content})
            continue
        if message['role'] == 'assistant':
            text = ''.join(b.get('text', '') for b in content if b.get('type') == 'text')
            calls = [{'id': b['id'], 'type': 'function', 'function': {'name': b['name'],
                      'arguments': json.dumps(b['input'], ensure_ascii=False)}}
                     for b in content if b.get('type') == 'tool_use']
            item = {'role': 'assistant', 'content': text or None}
            if calls:
                item['tool_calls'] = calls
            converted.append(item)
        else:
            if any(block.get('type') == 'image' for block in content):
                if message['role'] != 'user':
                    raise ValueError('Image input requires a user message')
                parts = []
                for block in content:
                    if block.get('type') == 'text':
                        parts.append({'type': 'text', 'text': block.get('text', '')})
                    elif block.get('type') == 'image':
                        source = block.get('source', {})
                        if source.get('type') != 'base64' or source.get('media_type') not in {'image/png', 'image/jpeg', 'image/webp'}:
                            raise ValueError('Only local base64 still images are supported')
                        parts.append({'type': 'image_url', 'image_url': {
                            'url': f"data:{source['media_type']};base64,{source['data']}"}})
                    else:
                        raise ValueError('Unsupported mixed image input')
                converted.append({'role': 'user', 'content': parts})
                continue
            text = []
            for block in content:
                if block.get('type') == 'tool_result':
                    converted.append({'role': 'tool', 'tool_call_id': block['tool_use_id'],
                                      'content': str(block.get('content', ''))})
                elif block.get('type') == 'text':
                    text.append(block.get('text', ''))
                else:
                    raise ValueError('This Chat Completions adapter does not support that content block')
            if text:
                converted.append({'role': message['role'], 'content': '\n'.join(text)})
    return converted


def _parse(data, model):
    usage = Usage(None, None)
    try:
        raw = data.get('usage') or {}
        for wire, attr in (('prompt_tokens', 'input_tokens'), ('completion_tokens', 'output_tokens')):
            if wire in raw:
                value = raw[wire]
                if type(value) is not int or value < 0:
                    raise ValueError
                setattr(usage, attr, value)
        choices = data['choices']
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError
        choice = choices[0]
        reason = {'stop': 'end_turn', 'tool_calls': 'tool_use', 'length': 'max_tokens'}.get(choice.get('finish_reason'))
        if reason is None:
            raise ValueError
        message = choice['message']
        text = message.get('content') or ''
        if not isinstance(text, str):
            raise ValueError
        turn = Turn(text=text, model=data.get('model') or model, usage=usage, stop_reason=reason)
        if text:
            turn.raw_blocks.append({'type': 'text', 'text': text})
        ids = set()
        for call in message.get('tool_calls') or []:
            function = call['function']
            args = json.loads(function['arguments'])
            if (call.get('type') != 'function' or not isinstance(args, dict)
                    or not isinstance(call.get('id'), str) or not call['id'] or call['id'] in ids
                    or not isinstance(function.get('name'), str) or not function['name']):
                raise ValueError
            ids.add(call['id'])
            turn.tool_calls.append(ToolCall(call['id'], function['name'], args))
            turn.raw_blocks.append({'type': 'tool_use', 'id': call['id'], 'name': function['name'], 'input': args})
        if (reason == 'tool_use') != bool(turn.tool_calls):
            raise ValueError
        return turn
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        raise ModelError(200, 'invalid Chat Completions response; received usage retained',
                         usage=usage, usage_final=True) from None


class OpenAICompatClient:
    def __init__(self, base_url, token, *, max_tokens=DEFAULT_MAX_TOKENS, timeout=180.0,
                 token_limit_parameter='max_completion_tokens'):
        from ..connections import validate_base_url
        self.base_url = validate_base_url(base_url)
        if not token:
            raise ValueError('No API key configured for this connection')
        self._token = token
        self.max_tokens = max_tokens
        self.timeout = timeout
        if token_limit_parameter not in ('max_completion_tokens', 'max_tokens'):
            raise ValueError('Unsupported token limit parameter')
        self.token_limit_parameter = token_limit_parameter

    def complete(self, *, model, system, messages, tools):
        payload = {'model': model, 'messages': _messages(system, messages),
                   self.token_limit_parameter: self.max_tokens, 'stream': False}
        if tools:
            payload['tools'] = [{'type': 'function', 'function': {'name': t['name'],
                                'description': t.get('description', ''),
                                'parameters': t['input_schema']}} for t in tools]
        url = self.base_url + ('/chat/completions' if self.base_url.endswith('/v1') else '/v1/chat/completions')
        try:
            # No redirects, environment proxy credentials, retries, or response-body errors.
            with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                response = client.post(url, headers={'Authorization': f'Bearer {self._token}'}, json=payload)
                if response.status_code != 200:
                    raise ModelError(response.status_code, 'request rejected; usage unconfirmed')
                try:
                    data = response.json()
                except ValueError:
                    raise ModelError(200, 'invalid JSON response; usage unconfirmed') from None
                return _parse(data, model)
        except httpx.TransportError as exc:
            raise ModelError(0, f'transport failed ({type(exc).__name__}); usage unconfirmed') from None
