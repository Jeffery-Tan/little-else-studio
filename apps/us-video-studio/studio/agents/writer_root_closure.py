"""One evidenced framing correction, never a general JSON repair or tool replay."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

HTTP_COMPLETE = object()
SSE_COMPLETE = object()
METHOD = 'writer_root_close_v1'


def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('non-finite JSON number')


def _writer_schema(tools):
    from ..tools.registry import _writing_submission_schema
    if not tools or any(tool.get('strict') is not True for tool in tools):
        return None
    submissions = [tool for tool in tools if tool.get('name') == 'submit_stage']
    if len(submissions) != 1:
        return None
    schema = submissions[0].get('input_schema')
    for format in ('video', 'photo'):
        if _encoded(schema) == _encoded(_writing_submission_schema(format)):
            return schema
    return None


def _candidate(arguments, error, schema, rejection_diagnostic=None):
    def reject(rule, **issue):
        if rejection_diagnostic is not None:
            rejection_diagnostic.update(writer_framing_rule=rule, **issue)
        return None
    # A valid JSON response, a non-EOF error or a nested omission is never eligible.
    if error.pos != len(arguments):
        return reject('not_eof')
    if len(arguments.encode('utf-8')) > 1_000_000:
        return reject('arguments_too_large')
    stack, in_string, escaped = [], False, False
    for index, char in enumerate(arguments):
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in '{[':
            stack.append((char, index))
        elif char in '}]':
            if not stack or stack[-1][0] != ('{' if char == '}' else '['):
                return reject('delimiter_mismatch')
            stack.pop()
    root = len(arguments) - len(arguments.lstrip())
    if in_string or escaped:
        return reject('incomplete_string')
    if len(stack) > 1:
        return reject('nested_unclosed', writer_unclosed_depth=len(stack))
    if stack != [('{', root)]:
        return reject('root_not_object')
    candidate = arguments + '}'
    try:
        decoded = json.loads(candidate, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        from .deepseek_client import _strict_schema_issue
        from ..structured import normalize, validate_canonical_input
        issue = _strict_schema_issue(decoded, schema)
        if issue:
            return reject('schema_invalid', **issue)
        try:
            validate_canonical_input('writing', decoded['structured'])
        except (ValueError, TypeError):
            return reject('canonical_invalid')
        if normalize('writing', decoded['structured']).dropped:
            return reject('canonical_lossy')
        # Reject unpaired surrogates rather than retaining a non-UTF-8 execution copy.
        _encoded(decoded).encode('utf-8')
    except UnicodeError:
        return reject('invalid_utf8')
    except (ValueError, TypeError):
        return reject('invalid_candidate_json')
    return candidate, decoded


@dataclass
class WriterRootClosure:
    tool_call_id: str
    original_arguments: str
    execution_arguments: str
    schema_sha256: str
    completion_source: str
    evidence_saved: bool = False

    def diagnostic(self):
        return {'format': ('chat_completion_stream' if self.completion_source == 'sse_done'
                           else 'chat_completion'),
                'provider': 'deepseek', 'field': 'tool_arguments', 'code': 'root_object_closed',
                'processing_method': METHOD, 'completion_source': self.completion_source,
                'finish_reason': 'tool_calls', 'tool_call_index': 0,
                'json_position': len(self.original_arguments),
                'arguments_bytes': len(self.original_arguments.encode('utf-8')),
                'execution_arguments_bytes': len(self.execution_arguments.encode('utf-8')),
                'arguments_sha256': _digest(self.original_arguments),
                'execution_arguments_sha256': _digest(self.execution_arguments),
                'schema_sha256': self.schema_sha256}


def root_closure(arguments, error, *, tools, tool_call_id, completion, rejection_diagnostic=None):
    """Called only after identity checks, with provenance from a real completed transport."""
    source = ('http_body_complete' if completion is HTTP_COMPLETE else
              'sse_done' if completion is SSE_COMPLETE else None)
    if source is None:
        if rejection_diagnostic is not None:
            rejection_diagnostic['writer_framing_rule'] = 'not_complete_transport'
        return None
    try:
        schema = _writer_schema(tools)
        if schema is None:
            if rejection_diagnostic is not None:
                rejection_diagnostic['writer_framing_rule'] = 'not_writer_schema'
            return None
        candidate = _candidate(arguments, error, schema, rejection_diagnostic)
    except UnicodeError:
        if rejection_diagnostic is not None:
            rejection_diagnostic['writer_framing_rule'] = 'invalid_utf8'
        return None
    except (ValueError, TypeError):
        if rejection_diagnostic is not None:
            rejection_diagnostic['writer_framing_rule'] = 'invalid_candidate_json'
        return None
    if candidate is None:
        return None
    text, decoded = candidate
    return WriterRootClosure(tool_call_id, arguments, text, _digest(_encoded(schema)), source), decoded


def verify_root_closure(turn, tools):
    """Recheck the source/copy pair at the shared execution boundary, without defaults."""
    closure = turn.writer_root_closure
    if closure is None:
        return
    valid = False
    try:
        schema = _writer_schema(tools)
        calls = turn.tool_calls
        if (schema is not None and turn.stop_reason == 'tool_use' and len(calls) == 1
                and calls[0].name == 'submit_stage' and calls[0].id == closure.tool_call_id
                and closure.completion_source in {'http_body_complete', 'sse_done'}
                and closure.schema_sha256 == _digest(_encoded(schema))):
            try:
                json.loads(closure.original_arguments)
            except json.JSONDecodeError as error:
                candidate = _candidate(closure.original_arguments, error, schema)
                block = next((block for block in turn.raw_blocks if block.get('type') == 'tool_use'
                              and block.get('id') == calls[0].id), {})
                valid = bool(candidate is not None and candidate[0] == closure.execution_arguments
                             and _encoded(candidate[1]) == _encoded(calls[0].input)
                             and block.get('raw_arguments') == closure.original_arguments
                             and _encoded(block.get('execution_input')) == _encoded(candidate[1])
                             and block.get('arguments_origin') == METHOD
                             and 'input' not in block)
    except (ValueError, TypeError, AttributeError, UnicodeError):
        pass
    if not valid:
        from .client import ModelError
        raise ModelError(200, 'invalid evidenced writer root closure; no tools dispatched',
            usage=turn.usage, usage_final=True, retryable=False,
            public_message='脚本格式处理校验未通过，本轮未执行返回工具；已收到的模型用量保留。')
