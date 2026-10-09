"""DeepSeek transport; thinking stays private when a role enables it.

Thinking protocol checked against official docs on 2026-09-19; complete writer
tasks use the official strict tool-call Beta contract checked on 2026-10-06.
Never retries a request or presents reasoning as assistant text.
"""
from __future__ import annotations

import asyncio
import json
import hashlib
import math
import re
import time
import httpx

from .client import JobCancelled, ModelError, Usage
from .openai_compat import _messages as openai_messages, _parse as openai_parse, _response_error
from .requests import safe_response_diagnostic, request_transport_diagnostic
from .http_rejection import read_http_rejection, transport_failure_kind
from .writer_root_closure import HTTP_COMPLETE, SSE_COMPLETE, METHOD, root_closure
from ..tools.registry import complete_writing_empty_fields, strict_writer_execution_arguments

ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
STRICT_ENDPOINT = "https://api.deepseek.com/beta/chat/completions"
MODELS = ("deepseek-flash", "deepseek-v4-pro")
REASONING_BLOCK = "deepseek_reasoning"
REQUEST_TIMEOUT = 600.0
PROGRESS_INTERVAL = 10.0


def _check_strict_schema(schema, *, reject_nested_empty=False, _root=True):
    """Validate the documented strict subset used by complete writer tasks.

    Request-level projections encode optional fields explicitly. Unknown
    keywords/types still fail before HTTP instead of being silently downgraded.
    """
    keywords = {'object': {'properties', 'required', 'additionalProperties'},
                'array': {'items'}, 'string': {'enum'},
                'integer': {'minimum', 'maximum'}, 'boolean': set()}
    if not isinstance(schema, dict):
        raise ValueError('unsupported strict schema')
    if 'description' in schema and not isinstance(schema['description'], str):
        raise ValueError('invalid strict description')
    if 'anyOf' in schema:
        choices = schema['anyOf']
        if (set(schema) - {'anyOf', 'description'} or not isinstance(choices, list)
                or len(choices) < 2):
            raise ValueError('unsupported strict anyOf')
        for child in choices:
            _check_strict_schema(child, reject_nested_empty=reject_nested_empty, _root=False)
        return
    kind = schema.get('type')
    if kind not in keywords or set(schema) - {'type', 'description'} - keywords[kind]:
        raise ValueError('unsupported strict schema')
    if kind == 'object':
        properties = schema.get('properties')
        required = schema.get('required')
        if (not isinstance(properties, dict) or schema.get('additionalProperties') is not False
                or not isinstance(required, list) or any(not isinstance(v, str) for v in required)
                or any(not isinstance(key, str) for key in properties) or len(required) != len(properties)
                or set(required) != set(properties)):
            raise ValueError('strict objects must be closed and require all fields')
        if reject_nested_empty and not _root and not properties:
            raise ValueError('DeepSeek strict does not accept nested empty objects')
        for child in properties.values():
            _check_strict_schema(child, reject_nested_empty=reject_nested_empty, _root=False)
    elif kind == 'array':
        _check_strict_schema(schema.get('items'), reject_nested_empty=reject_nested_empty, _root=False)
    if 'enum' in schema and (kind != 'string' or not isinstance(schema['enum'], list)
                           or not schema['enum'] or any(not isinstance(v, str) for v in schema['enum'])):
        raise ValueError('unsupported strict enum')
    for bound in ('minimum', 'maximum'):
        if bound in schema and (kind != 'integer' or type(schema[bound]) is not int):
            raise ValueError('unsupported strict bound')
    if 'minimum' in schema and 'maximum' in schema and schema['minimum'] > schema['maximum']:
        raise ValueError('invalid strict bounds')


def _strict_schemas(tools, *, reject_nested_empty=False):
    if not any(tool.get('strict') is True for tool in tools):
        return {}
    schemas = {}
    for tool in tools:
        if tool.get('strict') is not True or tool['name'] in schemas:
            raise ValueError('all strict functions must be unique and enabled')
        _check_strict_schema(tool['input_schema'], reject_nested_empty=reject_nested_empty)
        schemas[tool['name']] = tool['input_schema']
    return schemas


def _strict_schema_issue(value, schema, path=()):
    """Locate a mismatch without returning model-authored keys or values."""
    actual = ('null' if value is None else 'boolean' if type(value) is bool else
              'integer' if type(value) is int else 'number' if isinstance(value, float) else
              'string' if isinstance(value, str) else 'array' if isinstance(value, list) else 'object')
    if 'anyOf' in schema:
        if any(_strict_schema_issue(value, child, path) is None for child in schema['anyOf']):
            return None
        return {'schema_path': list(path), 'expected_type': 'anyOf',
                'actual_type': actual, 'schema_rule': 'any_of_mismatch'}
    kind = schema['type']
    issue = {'schema_path': list(path), 'expected_type': kind, 'actual_type': actual}
    if kind != actual:
        return {**issue, 'schema_rule': 'wrong_type'}
    if kind == 'object':
        for key, child in schema['properties'].items():
            if key not in value:
                return {'schema_path': [*path, key], 'schema_rule': 'missing_field',
                        'expected_type': child.get('type', 'anyOf')}
        if set(value) - set(schema['properties']):
            return {**issue, 'schema_rule': 'extra_field'}
        for key, child in schema['properties'].items():
            found = _strict_schema_issue(value[key], child, (*path, key))
            if found:
                return found
    elif kind == 'array':
        for index, item in enumerate(value):
            found = _strict_schema_issue(item, schema['items'], (*path, index))
            if found:
                return found
    elif kind == 'integer':
        if 'minimum' in schema and value < schema['minimum']:
            return {**issue, 'schema_rule': 'minimum_mismatch'}
        if 'maximum' in schema and value > schema['maximum']:
            return {**issue, 'schema_rule': 'maximum_mismatch'}
    elif kind == 'string' and 'enum' in schema and value not in schema['enum']:
        return {**issue, 'schema_rule': 'enum_mismatch'}
    return None


def _matches_strict_schema(value, schema):
    return _strict_schema_issue(value, schema) is None


def _endpoint(payload):
    return STRICT_ENDPOINT if any(t['function'].get('strict') for t in payload.get('tools', [])) else ENDPOINT


def _messages(system, messages, *, with_tools, reasoning_block=REASONING_BLOCK, provider_name='DeepSeek', allow_video=False):
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
            private = ([b for b in content if b.get("type") == reasoning_block]
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
                raise ModelError(0, f"{provider_name} 历史消息缺少原始思考字段；请从保存的任务资料开启新一轮，不能重放不完整对话。",
                                 provider_invoked=False)
            items = openai_messages("", [message], allow_video=allow_video)
            if with_tools:
                items[0]["reasoning_content"] = private[0]["reasoning_content"]
                # DeepSeek accepts an empty string for a reasoning-only assistant.
                items[0]["content"] = items[0].get("content") or ""
        else:
            items = openai_messages("", [message], allow_video=allow_video)
        converted.extend(items)
    return converted


def _parse(data, model, *, require_reasoning=False, tools=(), reasoning_block=REASONING_BLOCK,
           complete_response=None):
    recovered = None
    framing_diagnostic = {}
    def recover(arguments, error, index, name, call_id):
        nonlocal recovered
        choice = data['choices'][0]
        if (index != 0 or name != 'submit_stage' or choice.get('finish_reason') != 'tool_calls'
                or len(choice['message'].get('tool_calls', [])) != 1):
            return None
        result = root_closure(arguments, error, tools=tools, tool_call_id=call_id,
                              completion=complete_response, rejection_diagnostic=framing_diagnostic)
        if result is None:
            return None
        recovered, decoded = result
        return decoded
    try:
        turn = openai_parse(data, model, recover_arguments=recover if complete_response is not None else None,
                            tools=tools)
    except ModelError as error:
        if framing_diagnostic:
            error.response_diagnostic = safe_response_diagnostic(
                {**(error.response_diagnostic or {}), **framing_diagnostic}, tools=tools)
        raise
    schemas = _strict_schemas(tools)
    for index, call in enumerate(turn.tool_calls):
        paths = []
        if recovered is None and call.name == 'submit_stage' and call.name in schemas:
            completed, paths = complete_writing_empty_fields(call.input, schemas[call.name])
            # raw_blocks contains the actual model response. Only execution
            # parameters may use the completed independent copy.
            call.input = completed
        issue = (_strict_schema_issue(call.input, schemas[call.name]) if call.name in schemas else
                 {'schema_rule': 'unavailable_tool'}) if schemas else None
        if issue:
            choice = data['choices'][0]
            arguments = choice['message']['tool_calls'][index]['function']['arguments']
            raise _response_error(data, turn.usage, 'tool_arguments', 'invalid_arguments_schema',
                                  choice=choice, index=index, arguments=arguments, schema_issue=issue,
                                  tool_name=call.name, tools=tools)
        if call.name in schemas:
            call.input = strict_writer_execution_arguments(call.name, call.input, schemas[call.name])
        if paths:
            arguments = data['choices'][0]['message']['tool_calls'][index]['function']['arguments']
            raw = arguments.encode('utf-8', errors='surrogatepass')
            turn.response_normalizations.append(safe_response_diagnostic({
                'format': 'chat_completion', 'field': 'tool_arguments', 'code': 'empty_defaults_applied',
                'tool_call_index': index, 'arguments_bytes': len(raw),
                'arguments_sha256': hashlib.sha256(raw).hexdigest(), 'defaulted_paths': paths}))
    reasoning = data["choices"][0]["message"].get("reasoning_content")
    if (reasoning is not None and not isinstance(reasoning, str)) or (require_reasoning and not isinstance(reasoning, str)):
        raise _response_error(data, turn.usage, 'reasoning_content', 'missing_or_invalid_reasoning',
                              choice=data['choices'][0])
    if reasoning is not None:
        # The loop replays raw_blocks verbatim. Neither text nor thinking nor
        # any observer-facing progress contains this private protocol field.
        turn.raw_blocks.insert(0, {"type": reasoning_block, "reasoning_content": reasoning})
    if recovered is not None:
        # The raw block carries the original wire string. Its explicitly named
        # execution copy is never serialized as if it were model-origin input.
        block = next(block for block in turn.raw_blocks if block.get('type') == 'tool_use')
        block['execution_input'] = block.pop('input')
        block.update(raw_arguments=recovered.original_arguments, arguments_origin=METHOD,
                     arguments_sha256=recovered.diagnostic()['arguments_sha256'],
                     execution_arguments_sha256=recovered.diagnostic()['execution_arguments_sha256'])
        turn.writer_root_closure = recovered
        turn.response_normalizations.append(recovered.diagnostic())
    return turn


def _provider_status(data):
    error = data.get('error', data) if isinstance(data, dict) else None
    if isinstance(error, dict):
        value = error.get('code')
        # Never carry arbitrary error names/messages into persisted metadata.
        if type(value) is int and 400 <= value <= 599:
            return value
    return None


async def _sse_events(lines):
    """Join data lines within an SSE event; comments are transport keepalives."""
    data, event = [], ''
    async for line in lines:
        if not line:
            if data or event:
                yield event, '\n'.join(data)
            data, event = [], ''
        elif line.startswith('data:'):
            value = line[5:]
            data.append(value[1:] if value.startswith(' ') else value)
        elif line.startswith('event:'):
            event = line[6:].strip()
    if data or event:
        yield event, '\n'.join(data)


def _partial_usage(raw):
    """Only retain valid counters; a stream fragment never proves final usage."""
    raw = raw if isinstance(raw, dict) else {}
    counts = [raw.get(key) for key in ('prompt_tokens', 'completion_tokens')]
    return Usage(*(value if type(value) is int and value >= 0 else None for value in counts))


def _stream_error(provider_name, field, code, *, raw='', frame_index=0, choice_count=None,
                  json_error=None, status=200, format='chat_completion_stream', provider_status=None,
                  usage=None):
    """Keep an exact structural location, never arbitrary provider error text."""
    encoded = raw.encode('utf-8', errors='surrogatepass')
    issue = {'format': format, 'provider': provider_name.lower(),
             'field': field, 'code': code, 'frame_index': frame_index,
             'frame_bytes': len(encoded), 'frame_sha256': hashlib.sha256(encoded).hexdigest()}
    if choice_count is not None:
        issue['choice_count'] = choice_count
    if provider_status is not None:
        issue['provider_status'] = provider_status
    if json_error is not None:
        issue.update(json_line=json_error.lineno, json_column=json_error.colno, json_position=json_error.pos)
    public = (f'{provider_name} 接口在返回过程中报错，本次结果未完成。' if code == 'provider_error' else
              f'{provider_name} 返回未完整结束，本次结果不可用。' if code == 'incomplete_stream' else
              f'{provider_name} 返回结果未通过格式检查（{field}: {code}）。')
    return ModelError(status, f'{provider_name} stream failed ({field}: {code}); usage unconfirmed',
                      usage=_partial_usage(usage),
                      response_diagnostic=safe_response_diagnostic(issue), retryable=False,
                      public_message=public + '已保存内容保留，没有自动重试。')


async def _stream_turn(lines, model, *, require_reasoning, on_progress=None,
                       check_cancel=None, on_model_data=None, on_usage=None, tools=(),
                       reasoning_block=REASONING_BLOCK, provider_name='DeepSeek',
                       on_frame=None, on_delta=None):
    """Assemble public text and private replay fields from one complete SSE turn.

    Only `delta.content` can produce public text. The model-data callback carries
    no content; private reasoning and partial tool JSON remain local.
    """
    content, reasoning, calls = [], [], {}
    finish, usage, actual_model, done = None, None, model, False
    preview_count, last_preview_size = 0, 0
    frame_index, raw = -1, ''
    async for event, raw in _sse_events(lines):
        frame_index += 1
        if on_frame:
            on_frame(raw)
        def reject(field, code, **metadata):
            return _stream_error(provider_name, field, code, raw=raw, frame_index=frame_index,
                                 usage=usage, **metadata)

        if check_cancel:
            check_cancel()
        if event == 'error':
            try:
                error_data = json.loads(raw)
            except ValueError:
                error_data = None
            if isinstance(error_data, dict) and error_data.get('usage') is not None:
                usage = error_data['usage']
                if on_usage:
                    on_usage(usage)
            raise reject('response', 'provider_error', provider_status=_provider_status(error_data))
        value = raw.strip()
        if not value:
            continue
        if value == '[DONE]':
            done = True
            break
        try:
            chunk = json.loads(value)
        except json.JSONDecodeError as exc:
            raise reject('response', 'invalid_chunk_json', json_error=exc) from None
        if not isinstance(chunk, dict):
            raise reject('response', 'expected_object')
        if chunk.get('usage') is not None:
            usage = chunk['usage']
            if on_usage:
                on_usage(usage)
        if chunk.get('error') is not None:
            raise reject('response', 'provider_error', provider_status=_provider_status(chunk))
        choices = chunk.get('choices')
        # A provider may send accounting separately after its final text/tool
        # delta. It is not an empty answer and does not itself complete a turn.
        if choices == [] and isinstance(chunk.get('usage'), dict):
            continue
        if not isinstance(choices, list) or len(choices) != 1:
            raise reject('choices', 'expected_single_choice',
                         choice_count=len(choices) if isinstance(choices, list) else None)
        choice = choices[0]
        if not isinstance(choice, dict):
            raise reject('choice', 'expected_object')
        if type(choice.get('index')) is not int or choice['index'] != 0:
            raise reject('choice', 'invalid_identity')
        delta = choice.get('delta')
        if delta is None:
            delta = {}
        if not isinstance(delta, dict):
            raise reject('delta', 'expected_object')
        if finish is not None and any(delta.get(key) for key in ('content', 'reasoning_content', 'tool_calls')):
            raise reject('delta', 'data_after_finish')
        if on_model_data and any(delta.get(key) for key in ('content', 'reasoning_content', 'tool_calls')):
            on_model_data()
        public = delta.get('content')
        private = delta.get('reasoning_content')
        if public is not None:
            if not isinstance(public, str):
                raise reject('content', 'expected_text')
            content.append(public)
            if on_delta:
                on_delta('public_content_bytes', public)
            rendered = ''.join(content)
            if (on_progress and preview_count < 16 and len(rendered) - last_preview_size >= 12
                    and (rendered.endswith(('。', '！', '？', '.', '!', '?', '\n'))
                         or len(rendered) - last_preview_size >= 120)):
                on_progress({'type': 'agent_narration_preview', 'text': rendered[-500:]})
                preview_count += 1
                last_preview_size = len(rendered)
        if private is not None:
            if not isinstance(private, str):
                raise reject('reasoning_content', 'expected_text')
            reasoning.append(private)
            if on_delta:
                on_delta('reasoning_bytes', private)
        pieces = delta.get('tool_calls')
        if pieces is None:
            pieces = []
        if not isinstance(pieces, list):
            raise reject('tool_calls', 'expected_array')
        for piece in pieces:
            if not isinstance(piece, dict):
                raise reject('tool_call', 'expected_object')
            index = piece.get('index')
            if type(index) is not int or not 0 <= index < 16:
                raise reject('tool_call', 'invalid_identity')
            assembled = calls.setdefault(index, {'id': '', 'type': 'function',
                                                   'function': {'name': '', 'arguments': ''}})
            if piece.get('type') not in (None, 'function'):
                raise reject('tool_call', 'expected_function')
            fragment = piece.get('id')
            if fragment is not None:
                if not isinstance(fragment, str) or (assembled['id'] and assembled['id'] != fragment):
                    raise reject('tool_call', 'invalid_identity')
                assembled['id'] = fragment
            function = piece.get('function')
            if function is None:
                function = {}
            if not isinstance(function, dict):
                raise reject('tool_call', 'expected_function')
            for field in ('name', 'arguments'):
                fragment = function.get(field)
                if fragment is not None:
                    if not isinstance(fragment, str):
                        raise reject('tool_arguments' if field == 'arguments' else 'tool_call', 'expected_text')
                    if field == 'name':
                        if assembled['function'][field] and assembled['function'][field] != fragment:
                            raise reject('tool_call', 'invalid_identity')
                        assembled['function'][field] = fragment
                    else:
                        assembled['function'][field] += fragment
                        if on_delta:
                            on_delta('tool_argument_bytes', fragment)
                        if len(assembled['function'][field]) > 1_000_000:
                            raise reject('tool_arguments', 'arguments_too_large')
        if choice.get('finish_reason') is not None:
            if finish is not None:
                raise reject('finish_reason', 'duplicate_finish')
            if not isinstance(choice['finish_reason'], str):
                raise reject('finish_reason', 'expected_text')
            finish = choice['finish_reason']
        if isinstance(chunk.get('model'), str) and chunk['model']:
            actual_model = chunk['model']
    if not done or finish is None:
        raise _stream_error(provider_name, 'response', 'incomplete_stream', status=0,
                            frame_index=max(frame_index, 0), raw=raw, usage=usage)
    if calls and sorted(calls) != list(range(len(calls))):
        raise _stream_error(provider_name, 'tool_calls', 'invalid_identity', frame_index=frame_index,
                            raw=raw, usage=usage)
    message = {'content': ''.join(content),
               'tool_calls': [calls[i] for i in sorted(calls)]}
    if reasoning:
        message['reasoning_content'] = ''.join(reasoning)
    try:
        return _parse({'model': actual_model, 'choices': [{'finish_reason': finish,
            'message': message}], 'usage': usage if usage is not None else {}},
            model, require_reasoning=require_reasoning, tools=tools, reasoning_block=reasoning_block,
            complete_response=SSE_COMPLETE if provider_name == 'DeepSeek' else None)
    except ModelError as exc:
        if isinstance(exc.response_diagnostic, dict):
            exc.response_diagnostic = safe_response_diagnostic({**exc.response_diagnostic,
                'format': 'chat_completion_stream', 'provider': provider_name.lower()}, tools=tools)
        raise


class _TransportObservation:
    """Call-local bounded statistics, never response text or private replay."""
    def __init__(self, payload, provider, endpoint, timeout, started):
        self.options = request_transport_diagnostic(payload, provider)
        self.started, self.last_data, self.last_model_data = started, None, None
        self.capped = False
        self.counts = dict.fromkeys(('received_bytes', 'received_frames', 'received_frame_bytes',
                                    'public_content_bytes', 'reasoning_bytes', 'tool_argument_bytes'), 0)
        self.options['request_timeout_ms'] = self.milliseconds(timeout)
        # Uses HTTPX's actual JSON serializer without constructing credentials.
        self.request(httpx.Request('POST', endpoint, json=payload))

    def bounded(self, value):
        if value >= 2**31:
            self.capped = True
        return min(max(0, value), 2**31 - 1)

    def count(self, field, size):
        self.counts[field] = self.bounded(self.counts[field] + size)

    def milliseconds(self, seconds):
        # Clamp before multiplying: a valid finite timeout may overflow in ms.
        return self.bounded(2**31 if seconds >= 2**31 / 1000 else int(seconds * 1000))

    def timeouts(self, timeout):
        for phase in ('connect', 'read', 'write', 'pool'):
            seconds = getattr(timeout, phase)
            if seconds is not None:
                self.options[f'http_{phase}_timeout_ms'] = self.milliseconds(seconds)

    def request(self, request):
        wire = request.content
        self.options.update(request_bytes=self.bounded(len(wire)),
                            request_sha256=hashlib.sha256(wire).hexdigest())

    def received(self, data):
        self.count('received_bytes', len(data))
        if data:
            self.last_data = time.monotonic()

    def frame(self, raw):
        self.count('received_frames', 1)
        self.count('received_frame_bytes', len(raw.encode('utf-8', errors='surrogatepass')))

    def delta(self, field, value):
        self.count(field, len(value.encode('utf-8', errors='surrogatepass')))
        if value:
            self.last_model_data = time.monotonic()

    def snapshot(self):
        now = time.monotonic()
        result = {**self.options, **self.counts,
                  'elapsed_ms': self.milliseconds(now - self.started)}
        for key, timestamp in (('last_data_age_ms', self.last_data),
                               ('last_model_data_age_ms', self.last_model_data)):
            if timestamp is not None:
                result[key] = self.milliseconds(now - timestamp)
        result['transport_statistics_capped'] = self.capped
        return safe_response_diagnostic(result)


class _ObservedByteStream(httpx.AsyncByteStream):
    def __init__(self, stream, observation):
        self.stream, self.observation = stream, observation

    async def __aiter__(self):
        async for chunk in self.stream:
            self.observation.received(chunk)
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class DeepSeekClient:
    allow_video = False
    reject_nested_empty_strict_objects = True
    provider_name = 'DeepSeek'
    reasoning_block = REASONING_BLOCK
    _endpoint = staticmethod(_endpoint)

    def __init__(self, token, *, max_tokens=None, timeout=180.0, check_cancel=None,
                 cancel_event=None, reasoning_effort='high', request_timeout=REQUEST_TIMEOUT,
                 progress_interval=PROGRESS_INTERVAL, progress_stream=True):
        if (not isinstance(token, str) or not token or not token.isascii()
                or any(character.isspace() or ord(character) < 33 or ord(character) == 127 for character in token)):
            raise ValueError("DeepSeek API key must contain ASCII characters without whitespace or control characters")
        if max_tokens is not None and (type(max_tokens) is not int or not 1 <= max_tokens <= 393216):
            raise ValueError("DeepSeek max_tokens must be an integer between 1 and 393216")
        if reasoning_effort not in ('none', 'low', 'high'):
            raise ValueError('DeepSeek reasoning_effort must be none, low or high')
        for name, value in (('request_timeout', request_timeout), ('progress_interval', progress_interval)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'DeepSeek {name} must be a positive finite number')
        if type(progress_stream) is not bool:
            raise ValueError('DeepSeek progress_stream must be boolean')
        self.progress_stream = progress_stream
        self._token = token
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.check_cancel = check_cancel
        self.cancel_event = cancel_event
        self.request_timeout = request_timeout
        self.progress_interval = progress_interval

    def complete_with_progress(self, *, model, system, messages, tools, on_progress=None):
        payload = self._payload(model, system, messages, tools)
        payload['stream'] = self.progress_stream
        return self._complete_request(payload, model, tools, on_progress=on_progress)

    def _check_cancel(self):
        if self.check_cancel:
            self.check_cancel()
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise JobCancelled('用户已请求停止；未启动下一次模型调用或工具。')

    def _complete_request(self, payload, model, tools, *, on_progress=None):
        self._check_cancel()
        try:
            return asyncio.run(self._bounded_request(payload, model, tools, on_progress))
        except httpx.TransportError as exc:
            self._check_cancel()
            raise ModelError(0, f'transport failed ({type(exc).__name__}); usage unconfirmed') from None
        except (ModelError, JobCancelled):
            raise
        except RuntimeError as exc:
            self._check_cancel()
            raise ModelError(0, f'stream failed ({type(exc).__name__}); usage unconfirmed') from None

    async def _bounded_request(self, payload, model, tools, on_progress):
        # Async cancellation interrupts headers and body reads as well as SSE.
        # No abandoned request thread or second provider attempt is created.
        started = time.monotonic()
        observation = _TransportObservation(payload, self.provider_name, self._endpoint(payload),
                                             self.request_timeout, started)
        failed_error, completed_turn = None, None
        receiving = False
        last_progress = started
        observed_usage = Usage(None, None)

        def report():
            nonlocal last_progress
            last_progress = time.monotonic()
            if on_progress:
                elapsed = min(self.request_timeout, max(0.0, last_progress - started))
                on_progress({'type': 'agent_activity', 'phase': 'waiting_model',
                             'elapsed_seconds': round(elapsed, 1),
                             'remaining_seconds': round(max(0.0, self.request_timeout - elapsed), 1),
                             'timeout_seconds': self.request_timeout,
                             'transport_state': 'receiving' if receiving else 'waiting'})

        def model_data():
            nonlocal receiving
            if not receiving:
                receiving = True
                report()

        def model_usage(raw):
            nonlocal observed_usage
            usage = _partial_usage(raw)
            observed_usage = Usage(usage.input_tokens if usage.input_tokens is not None else observed_usage.input_tokens,
                                   usage.output_tokens if usage.output_tokens is not None else observed_usage.output_tokens)

        report()
        task = asyncio.create_task(self._exchange(payload, model, tools, on_progress, model_data,
                                                 model_usage, observation))
        try:
            while True:
                if task.done():
                    try:
                        turn = task.result()
                    except ModelError as failure:
                        if failure.usage_status != 'known':
                            failure.usage = Usage(
                                failure.usage.input_tokens if failure.usage.input_tokens is not None else observed_usage.input_tokens,
                                failure.usage.output_tokens if failure.usage.output_tokens is not None else observed_usage.output_tokens)
                            failure.usage_status = ('partial' if any(value is not None for value in
                                (failure.usage.input_tokens, failure.usage.output_tokens)) else 'unknown')
                        try:
                            self._check_cancel()
                        except JobCancelled as exc:
                            raise JobCancelled(str(exc), usage=failure.usage,
                                               usage_final=failure.usage_status == 'known') from exc
                        raise
                    try:
                        self._check_cancel()
                    except JobCancelled as exc:
                        raise JobCancelled(str(exc), usage=turn.usage, usage_final=True) from exc
                    if time.monotonic() - started >= self.request_timeout:
                        raise ModelError(0, f'模型单次等待超过 {self.request_timeout:g} 秒，本轮已停止；'
                                         '未自动重试；received usage retained',
                            usage=turn.usage, usage_final=True, retryable=False,
                            response_diagnostic={'code': 'request_deadline_exceeded',
                                                 'transport_failure': 'total_deadline'},
                            public_message='模型本次等待超过总时限，已停止本轮且没有自动重试；已保存内容和已收到用量保留。')
                    completed_turn = turn
                    return turn
                self._check_cancel()
                remaining = self.request_timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise ModelError(0, f'模型单次等待超过 {self.request_timeout:g} 秒，本轮已停止；'
                                     '未自动重试，已保存内容保留。请检查连接后发送新的指令；usage unconfirmed',
                                     usage=observed_usage, retryable=False,
                                     response_diagnostic={'code': 'request_deadline_exceeded',
                                                          'transport_failure': 'total_deadline'})
                if time.monotonic() - last_progress >= self.progress_interval:
                    report()
                await asyncio.wait({task}, timeout=min(.1, remaining, self.progress_interval))
        except JobCancelled as exc:
            if exc.usage is None:
                exc.usage = observed_usage
            failed_error = exc
            raise
        except (httpx.TransportError, httpx.DecodingError) as exc:
            try:
                self._check_cancel()
            except JobCancelled as cancelled:
                failed_error = JobCancelled(str(cancelled), usage=observed_usage)
                raise failed_error from cancelled
            kind = transport_failure_kind(exc)
            issue = {'code': 'incomplete_stream' if payload['stream'] else 'transport_error',
                     'transport_failure': kind}
            action = ('连接超时' if kind == 'connect_timeout' else
                      '读取超时' if kind == 'read_timeout' else '连接中断')
            failed_error = ModelError(0, f'{self.provider_name} transport interrupted ({kind}); usage unconfirmed',
                usage=observed_usage, response_diagnostic=safe_response_diagnostic(issue), retryable=False,
                public_message=f'{self.provider_name} {action}，本次结果未完成。已保存内容保留，没有自动重试。')
            raise failed_error from None
        except ModelError as exc:
            failed_error = exc
            raise
        except RuntimeError:
            try:
                self._check_cancel()
            except JobCancelled as cancelled:
                failed_error = JobCancelled(str(cancelled), usage=observed_usage)
                raise failed_error from cancelled
            failed_error = ModelError(0, f'{self.provider_name} stream processing failed; usage unconfirmed',
                usage=observed_usage, response_diagnostic={'code': 'transport_error',
                                                         'transport_failure': 'runtime_error'}, retryable=False)
            raise failed_error from None
        finally:
            if not task.done():
                task.cancel()
            # Await closure before releasing the job/ledger; late replies cannot
            # become a turn, tool call, or saved artifact after timeout/stop.
            await asyncio.gather(task, return_exceptions=True)
            details = observation.snapshot()
            if failed_error is not None:
                cancelled = isinstance(failed_error, JobCancelled)
                prior = getattr(failed_error, 'response_diagnostic', None) or {}
                if cancelled:
                    details['transport_failure'] = 'cancelled'
                elif prior.get('code') == 'incomplete_stream' and not prior.get('transport_failure'):
                    details['transport_failure'] = 'stream_eof'
                failed_error.transport_diagnostic = safe_response_diagnostic({**prior, **details,
                    'format': 'model_transport', 'code': 'transport_observed',
                    'transport_outcome': 'cancelled' if cancelled else 'failed'}, tools=tools)
                if isinstance(failed_error, ModelError):
                    failed_error.response_diagnostic = safe_response_diagnostic({
                        'format': 'chat_completion_stream' if payload['stream'] else 'chat_completion',
                        'field': 'response', 'code': 'transport_error', **details, **prior}, tools=tools)
            elif completed_turn is not None:
                completed_turn.transport_diagnostic = safe_response_diagnostic({**details,
                    'format': 'model_transport', 'code': 'transport_observed',
                    'transport_outcome': 'completed'})

    async def _exchange(self, payload, model, tools, on_progress, model_data, model_usage, observation):
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
            observation.timeouts(client.timeout)
            async with client.stream('POST', self._endpoint(payload),
                                     headers={'Authorization': f'Bearer {self._token}',
                                              'Accept-Encoding': 'identity'}, json=payload) as response:
                observation.request(response.request)
                observation.options['provider_status'] = response.status_code
                if response.is_stream_consumed:
                    observation.received(response.content)
                else:
                    response.stream = _ObservedByteStream(response.stream, observation)
                if response.status_code != 200:
                    raise await read_http_rejection(response, payload,
                        provider=self.provider_name, check_cancel=self._check_cancel)
                media_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
                if payload['stream'] and media_type != 'application/json':
                    return await _stream_turn(response.aiter_lines(), model,
                        require_reasoning=bool(tools) and self.reasoning_effort != 'none',
                        on_progress=on_progress, check_cancel=self._check_cancel, on_model_data=model_data,
                        on_usage=model_usage, on_frame=observation.frame, on_delta=observation.delta,
                        tools=tools, reasoning_block=self.reasoning_block, provider_name=self.provider_name)
                await response.aread()
                try:
                    data = response.json()
                except ValueError as exc:
                    raise _stream_error(self.provider_name, 'response', 'invalid_chunk_json',
                        raw=response.text, format='chat_completion',
                        json_error=exc if isinstance(exc, json.JSONDecodeError) else None) from None
                if isinstance(data, dict) and data.get('error') is not None:
                    raise _stream_error(self.provider_name, 'response', 'provider_error',
                        raw=response.text, format='chat_completion', provider_status=_provider_status(data),
                        usage=data.get('usage'))
                try:
                    return _parse(data, model, require_reasoning=bool(tools) and self.reasoning_effort != 'none',
                                  tools=tools, reasoning_block=self.reasoning_block,
                                  complete_response=HTTP_COMPLETE if self.provider_name == 'DeepSeek' else None)
                except ModelError as exc:
                    if isinstance(exc.response_diagnostic, dict):
                        exc.response_diagnostic = safe_response_diagnostic({**exc.response_diagnostic,
                            'provider': self.provider_name.lower()}, tools=tools)
                    raise

    def _payload(self, model, system, messages, tools):
        # The provider's discovered model list may grow or retain an existing
        # alias. Validate the wire identifier, not a frozen release catalog.
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
            raise ModelError(0, f"{self.provider_name} model must be a valid identifier of 1 to 200 characters", provider_invoked=False)
        try:
            strict_requested = isinstance(tools, (list, tuple)) and any(
                isinstance(tool, dict) and tool.get('strict') is True for tool in tools)
            schemas = _strict_schemas(tools,
                reject_nested_empty=self.reject_nested_empty_strict_objects)
            thinking = self.reasoning_effort != 'none'
            payload = {"model": model,
                       "messages": (_messages(system, messages, with_tools=bool(tools),
                                               reasoning_block=self.reasoning_block, provider_name=self.provider_name,
                                               allow_video=self.allow_video) if thinking
                                    else openai_messages(system, messages, allow_video=self.allow_video)),
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
                    "parameters": tool["input_schema"], **({'strict': True} if schemas else {})}} for tool in tools]
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ModelError(0, f"invalid {self.provider_name} request structure; request not sent", provider_invoked=False,
                             retryable=not strict_requested,
                             public_message=('脚本模型请求格式检查未通过，请修正接口定义后再继续；本次未调用模型。'
                                             if strict_requested else None)) from None
        return payload

    def complete(self, *, model, system, messages, tools):
        payload = self._payload(model, system, messages, tools)
        return self._complete_request(payload, model, tools)
