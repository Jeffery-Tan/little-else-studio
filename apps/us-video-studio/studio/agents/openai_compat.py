"""One-attempt Chat Completions transport translated to Studio tool messages."""
from __future__ import annotations

import hashlib
import json
import base64
import math
import asyncio
import httpx

from .client import DEFAULT_MAX_TOKENS, ModelError, ToolCall, Turn, Usage
from .requests import safe_response_diagnostic
from .bounded_http import bounded_http, check_stopped, validate_wait


def _messages(system, messages, *, allow_video=False):
    converted = [{'role': 'system', 'content': system}] if system else []
    for message in messages:
        content = message.get('content', '')
        if isinstance(content, str):
            converted.append({'role': message['role'], 'content': content})
            continue
        if message['role'] == 'assistant':
            text = ''.join(b.get('text', '') for b in content if b.get('type') == 'text')
            calls = [{'id': b['id'], 'type': 'function', 'function': {'name': b['name'],
                      'arguments': (b['raw_arguments'] if 'raw_arguments' in b
                                    else json.dumps(b['input'], ensure_ascii=False))}}
                     for b in content if b.get('type') == 'tool_use']
            item = {'role': 'assistant', 'content': text or None}
            if calls:
                item['tool_calls'] = calls
            converted.append(item)
        else:
            if any(block.get('type') in ({'image', 'video'} if allow_video else {'image'}) for block in content):
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
                    elif allow_video and block.get('type') == 'video':
                        source = block.get('source', {})
                        encoded = source.get('data')
                        mime = source.get('media_type', 'video/mp4')
                        fps, resolution = block.get('fps', 2), block.get('media_resolution', 'default')
                        if (source.get('type') != 'base64' or mime not in {'video/mp4', 'video/quicktime'}
                                or not isinstance(encoded, str) or not encoded or len(encoded) > 50_000_000 - 40
                                or type(fps) not in (float, int) or not math.isfinite(fps) or not .1 <= fps <= 10
                                or resolution not in {'default', 'max'}):
                            raise ValueError('Invalid bounded local video input')
                        try:
                            base64.b64decode(encoded, validate=True)
                        except ValueError:
                            raise ValueError('Invalid local video encoding') from None
                        parts.append({'type': 'video_url', 'video_url': {
                            'url': f'data:{mime};base64,{encoded}'}, 'fps': fps, 'media_resolution': resolution})
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


_INTERRUPTED = {
    'repetition_truncation': '模型服务检测到重复输出并截断了结果，未执行返回的工具。',
    'content_filter': '模型服务因内容过滤停止了本次输出，未执行返回的工具。',
    'insufficient_system_resource': '模型服务因推理资源不足中断了本次输出，未执行返回的工具。',
    'aborted': '模型服务中断了本次输出，未执行返回的工具。',
}
_FINISH_REASONS = {'stop', 'tool_calls', 'length', *_INTERRUPTED}
_JSON_ERROR_CATEGORIES = {
    'Expecting value': 'expected_value',
    'Expecting property name enclosed in double quotes': 'expected_property_name',
    "Expecting ',' delimiter": 'expected_comma',
    "Expecting ':' delimiter": 'expected_colon',
    'Extra data': 'extra_data',
    'Unterminated string starting at': 'unterminated_string',
    'Invalid \\escape': 'invalid_escape',
    'Invalid control character at': 'invalid_control_character',
    'Invalid \\uXXXX escape': 'invalid_unicode_escape',
    'Unexpected UTF-8 BOM (decode using utf-8-sig)': 'unexpected_bom',
}


def _response_error(data, usage, field, code, *, choice=None, index=None, arguments=None, json_error=None,
                    schema_issue=None, tool_name=None, tools=()):
    """Only fixed labels, counts, positions and hashes leave the adapter."""
    finish = choice.get('finish_reason') if isinstance(choice, dict) else None
    known_finish = finish if isinstance(finish, str) and finish in _FINISH_REASONS else 'unrecognized'
    diagnostic = {'format': 'chat_completion', 'field': field, 'code': code,
                  'finish_reason': known_finish}
    if isinstance(data, dict) and isinstance(data.get('choices'), list):
        diagnostic['choice_count'] = len(data['choices'])
    if index is not None:
        diagnostic['tool_call_index'] = index
    if isinstance(arguments, str):
        raw = arguments.encode('utf-8', errors='surrogatepass')
        diagnostic.update(arguments_chars=len(arguments), arguments_bytes=len(raw),
                          arguments_sha256=hashlib.sha256(raw).hexdigest())
    if isinstance(json_error, json.JSONDecodeError):
        category = (_JSON_ERROR_CATEGORIES.get(json_error.msg, 'other_json_error')
                    if isinstance(json_error.msg, str) else 'other_json_error')
        diagnostic.update(json_line=json_error.lineno, json_column=json_error.colno, json_position=json_error.pos,
                          json_error_category=category)
    diagnostic['tool_name'] = tool_name
    if schema_issue is not None:
        diagnostic.update(schema_issue)
    diagnostic = safe_response_diagnostic(diagnostic, tools=tools)
    if known_finish == 'length':
        message = 'output truncated at max_tokens; received usage retained'
        public = '模型输出达到单次上限，返回结果不完整，本轮未执行这些工具。'
    elif code == 'provider_interrupted':
        message = f'provider interrupted output ({known_finish}); received usage retained'
        public = _INTERRUPTED[known_finish]
    else:
        message = f'invalid Chat Completions response ({field}: {code}); received usage retained'
        public = ('模型返回的工具参数不是有效 JSON，本轮未执行这些工具。'
                  if code == 'invalid_arguments_json' else
                  '模型返回的工具参数不符合本次工具格式，本轮未执行这些工具。'
                  if code == 'invalid_arguments_schema' else
                  '模型返回的工具参数必须是完整对象，本轮未执行这些工具。'
                  if field == 'tool_arguments' else
                  '模型返回内容未通过格式校验，本轮未执行返回的工具。')
        if code == 'invalid_arguments_schema' and diagnostic.get('schema_rule'):
            path = ''.join(f'[{part}]' if type(part) is int else ('.' if i else '') + part
                           for i, part in enumerate(diagnostic.get('schema_path', []))) or '工具参数'
            rule = {'missing_field': '缺少必需字段', 'extra_field': '含未定义字段',
                    'wrong_type': '字段类型不符', 'enum_mismatch': '不在允许值范围',
                    'minimum_mismatch': '低于允许的最小值', 'maximum_mismatch': '超过允许的最大值',
                    'any_of_mismatch': '不符合任一允许格式', 'unavailable_tool': '工具不在本次允许集合'}
            public += f'具体问题：{path}（{rule[diagnostic["schema_rule"]]}）。'
    return ModelError(200, message, usage=usage, usage_final=True,
                      response_diagnostic=diagnostic,
                      public_message=public + '已保存的内容与调用用量记录保留，不会自动重试。')


def _parse(data, model, *, recover_arguments=None, tools=()):
    usage = Usage(None, None)
    if not isinstance(data, dict):
        raise _response_error(data, usage, 'response', 'expected_object')
    raw = data.get('usage')
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise _response_error(data, usage, 'usage', 'expected_object')
    invalid_usage = False
    for wire, attr in (('prompt_tokens', 'input_tokens'), ('completion_tokens', 'output_tokens')):
        if wire in raw:
            value = raw[wire]
            if type(value) is int and value >= 0:
                setattr(usage, attr, value)
            else:
                invalid_usage = True
    if invalid_usage:
        raise _response_error(data, usage, 'usage', 'invalid_token_count')
    choices = data.get('choices')
    if not isinstance(choices, list) or len(choices) != 1:
        raise _response_error(data, usage, 'choices', 'expected_single_choice')
    choice = choices[0]
    if not isinstance(choice, dict):
        raise _response_error(data, usage, 'choice', 'expected_object')
    finish = choice.get('finish_reason')
    if isinstance(finish, str) and finish in _INTERRUPTED:
        raise _response_error(data, usage, 'finish_reason', 'provider_interrupted', choice=choice)
    reason = {'stop': 'end_turn', 'tool_calls': 'tool_use', 'length': 'max_tokens'}.get(finish) if isinstance(finish, str) else None
    if reason is None:
        raise _response_error(data, usage, 'finish_reason', 'unsupported_finish_reason', choice=choice)
    message = choice.get('message')
    if not isinstance(message, dict):
        raise _response_error(data, usage, 'message', 'expected_object', choice=choice)
    text = message.get('content')
    text = '' if text is None else text
    if not isinstance(text, str):
        raise _response_error(data, usage, 'content', 'expected_text', choice=choice)
    calls = message.get('tool_calls')
    calls = [] if calls is None else calls
    if not isinstance(calls, list):
        raise _response_error(data, usage, 'tool_calls', 'expected_array', choice=choice)
    turn = Turn(text=text, model=data.get('model') or model, usage=usage, stop_reason=reason)
    if text:
        turn.raw_blocks.append({'type': 'text', 'text': text})
    ids = set()
    for index, call in enumerate(calls):
        function = call.get('function') if isinstance(call, dict) else None
        if not isinstance(function, dict):
            raise _response_error(data, usage, 'tool_call', 'expected_function', choice=choice, index=index)
        if (call.get('type') != 'function' or not isinstance(call.get('id'), str) or not call['id'] or call['id'] in ids
                or not isinstance(function.get('name'), str) or not function['name']):
            raise _response_error(data, usage, 'tool_call', 'invalid_identity', choice=choice, index=index)
        arguments = function.get('arguments')
        if not isinstance(arguments, str):
            raise _response_error(data, usage, 'tool_arguments', 'expected_json_string', choice=choice, index=index,
                                  tool_name=function['name'], tools=tools)
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError as exc:
            args = (recover_arguments(arguments, exc, index, function['name'], call['id'])
                    if recover_arguments is not None else None)
            if args is None:
                raise _response_error(data, usage, 'tool_arguments', 'invalid_arguments_json', choice=choice,
                                      index=index, arguments=arguments, json_error=exc,
                                      tool_name=function['name'], tools=tools) from None
        if not isinstance(args, dict):
            raise _response_error(data, usage, 'tool_arguments', 'expected_object', choice=choice,
                                  index=index, arguments=arguments, tool_name=function['name'], tools=tools)
        ids.add(call['id'])
        turn.tool_calls.append(ToolCall(call['id'], function['name'], args))
        turn.raw_blocks.append({'type': 'tool_use', 'id': call['id'], 'name': function['name'], 'input': args})
    if (reason == 'tool_use') != bool(turn.tool_calls):
        raise _response_error(data, usage, 'tool_calls', 'finish_reason_mismatch', choice=choice)
    return turn


class OpenAICompatClient:
    def __init__(self, base_url, token, *, max_tokens=DEFAULT_MAX_TOKENS, timeout=180.0,
                 token_limit_parameter='max_completion_tokens', check_cancel=None, cancel_event=None,
                 request_timeout=600.0, progress_interval=10.0):
        from ..connections import validate_base_url
        self.base_url = validate_base_url(base_url)
        if not token:
            raise ValueError('No API key configured for this connection')
        self._token = token
        self.max_tokens = max_tokens
        self.timeout = timeout
        validate_wait(request_timeout, progress_interval)
        self.check_cancel, self.cancel_event = check_cancel, cancel_event
        self.request_timeout, self.progress_interval = request_timeout, progress_interval
        if token_limit_parameter not in ('max_completion_tokens', 'max_tokens'):
            raise ValueError('Unsupported token limit parameter')
        self.token_limit_parameter = token_limit_parameter

    def complete(self, *, model, system, messages, tools):
        return self._complete(model=model, system=system, messages=messages, tools=tools)

    def complete_with_progress(self, *, model, system, messages, tools, on_progress=None):
        return self._complete(model=model, system=system, messages=messages, tools=tools, on_progress=on_progress)

    def _complete(self, *, model, system, messages, tools, on_progress=None):
        check_stopped(self.check_cancel, self.cancel_event)
        payload = {'model': model, 'messages': _messages(system, messages),
                   self.token_limit_parameter: self.max_tokens, 'stream': False}
        if tools:
            payload['tools'] = [{'type': 'function', 'function': {'name': t['name'],
                                'description': t.get('description', ''),
                                'parameters': t['input_schema']}} for t in tools]
        url = self.base_url + ('/chat/completions' if self.base_url.endswith('/v1') else '/v1/chat/completions')
        async def exchange(model_data, model_usage):
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                async with client.stream('POST', url, headers={'Authorization': f'Bearer {self._token}'},
                                         json=payload) as response:
                    if response.status_code != 200:
                        raise ModelError(response.status_code, 'request rejected; usage unconfirmed', retryable=False)
                    await response.aread()
                    try:
                        data = response.json()
                    except ValueError:
                        raise ModelError(200, 'invalid JSON response; usage unconfirmed', retryable=False) from None
                    turn = _parse(data, model, tools=tools)
                    model_usage(turn.usage)
                    if turn.text or turn.tool_calls:
                        model_data()
                    return turn

        return asyncio.run(bounded_http(exchange, payload=payload, endpoint=url, provider='openai_compat',
            check_cancel=self.check_cancel, cancel_event=self.cancel_event,
            request_timeout=self.request_timeout, progress_interval=self.progress_interval, on_progress=on_progress))
