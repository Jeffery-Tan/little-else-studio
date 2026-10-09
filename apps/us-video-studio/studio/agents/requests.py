"""Immutable model input diagnostics in the existing run, not a second task store."""
from __future__ import annotations

import hashlib
import json
import os
import re

from ..structured import SHAPES
from ..conversation_input import TASK_INTERPRETATION_TOOL
from ..agent_sessions import persistable_messages

HTTP_ERROR_TERMS = ('strict', 'schema', 'anyOf', 'oneOf', 'allOf', 'enum', 'empty', 'unsupported',
    'required', 'additionalProperties', 'minimum', 'maximum', 'minItems', 'maxItems', 'uniqueItems',
    'thinking', 'tool_choice', 'context_length_exceeded', 'object', 'properties', 'parameters',
    'function', 'type', 'number', 'integer', 'boolean', 'array', 'string', 'null', 'const',
    'at least', 'non-empty', 'missing', 'duplicate', 'not supported', 'not allowed', 'invalid',
    'too many', 'too long', 'too large', 'max_tokens', 'name', 'description', 'tools', 'model',
    'deserialize', 'parse', 'JSON', 'context')
HTTP_ERROR_TOOLS = {'read_capabilities', 'read_method_document', 'read_artifact', 'list_materials',
                    'read_role_history', 'submit_stage', 'propose_task_interpretation'}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def save_request(store, run_id, call_id, *, role, stage, request, route):
    # Only model-visible arguments and explicitly selected public route fields
    # cross this seam. Never serialize clients, settings or connection objects.
    if not re.fullmatch(r"[a-f0-9-]{36}", call_id):
        raise ValueError("invalid model call identity")
    messages, omitted = persistable_messages(request['messages'])
    captured = {**request, 'messages': messages}
    record = {"schema_version": 2 if omitted else 1, "run_id": run_id, "call_id": call_id,
              "role": role, "stage": stage, "request": captured, "route": route}
    if omitted:
        # This is an explicit diagnostic copy, not the complete input or a
        # resumable provider transcript. Keep only the original input digest;
        # private reasoning and exceptional original arguments never enter it.
        record['capture'] = {'mode': 'diagnostic_copy', 'private_protocol_omitted': True,
                             'full_protocol_replay_available': False,
                             'original_input_sha256': hashlib.sha256(encode(request).encode('utf-8')).hexdigest()}
    raw = encode(record).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    with store.run_lock(run_id):
        root = store.run_dir(run_id).resolve()
        path = root / "requests" / (call_id + ".json")
        if not path.resolve().is_relative_to(root):
            raise ValueError("request directory is outside the run")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        store.append_event(run_id, "agent_request_snapshot", call_id=call_id,
                           role=role, stage=stage, sha256=digest,
                           evidence_ref=f"requests/{call_id}.json")
    return digest


def read_request(store, run_id, call_id):
    """Local diagnostic reader; never a model/tool replay or an HTTP endpoint.

    Schema 2 diagnostic copies omit required private protocol fields and cannot
    be submitted as a complete historical request. Schema 1 historical files
    keep their original bytes and fingerprint; reading never rewrites them.
    """
    if not isinstance(call_id, str) or not re.fullmatch(r"[a-f0-9-]{36}", call_id):
        raise ValueError("invalid model call identity")
    event = next((e for e in store.read_events(run_id)
                  if e.get("kind") == "agent_request_snapshot" and e.get("call_id") == call_id), None)
    if event is None:
        raise ValueError("this call has no recorded request snapshot")
    root = store.run_dir(run_id).resolve()
    path = root / "requests" / (call_id + ".json")
    if not path.resolve().is_relative_to(root):
        raise ValueError("request directory is outside the run")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != event["sha256"]:
        raise ValueError("request snapshot fingerprint mismatch")
    record = json.loads(raw)
    if record.get("run_id") != run_id or record.get("call_id") != call_id:
        raise ValueError("request snapshot identity mismatch")
    return record


def declared_tool_names(tools):
    """Bound names to this call's logical declarations, never model output."""
    return frozenset(tool['name'] for tool in tools if isinstance(tool, dict)
                     and isinstance(tool.get('name'), str)
                     and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', tool['name']))


def safe_response_diagnostic(diagnostic, *, tools=()):
    """One whitelist for persisted diagnostics and public error details."""
    labels = {
        'format': {'chat_completion', 'chat_completion_stream', 'http_error', 'model_transport'},
        'provider': {'deepseek', 'mimo', 'openai_compat', 'anthropic_compat'},
        'field': {'response', 'usage', 'choices', 'choice', 'finish_reason', 'message',
                  'content', 'tool_calls', 'tool_call', 'tool_arguments', 'reasoning_content', 'delta'},
        'code': {'expected_object', 'invalid_token_count', 'expected_single_choice',
                 'provider_interrupted', 'unsupported_finish_reason', 'expected_text',
                 'expected_array', 'expected_function', 'invalid_identity', 'expected_json_string',
                 'invalid_arguments_json', 'invalid_arguments_schema', 'finish_reason_mismatch', 'missing_or_invalid_reasoning',
                 'empty_defaults_applied', 'root_object_closed', 'provider_error', 'invalid_chunk_json', 'incomplete_stream',
                 'duplicate_finish', 'data_after_finish', 'arguments_too_large', 'http_request_rejected',
                 'transport_observed', 'transport_error', 'protocol_error', 'request_deadline_exceeded'},
        'transport_outcome': {'completed', 'failed', 'cancelled'},
        'processing_method': {'writer_root_close_v1'},
        'writer_framing_rule': {'not_complete_transport', 'not_writer_schema', 'not_eof',
                                'arguments_too_large', 'delimiter_mismatch', 'incomplete_string',
                                'nested_unclosed', 'root_not_object', 'invalid_candidate_json',
                                'schema_invalid', 'canonical_invalid', 'canonical_lossy', 'invalid_utf8'},
        'completion_source': {'http_body_complete', 'sse_done'},
        'transport_failure': {'connect_timeout', 'read_timeout', 'write_timeout', 'pool_timeout',
                              'remote_protocol_error', 'local_protocol_error', 'connect_error',
                              'read_error', 'write_error', 'close_error', 'proxy_error',
                              'unsupported_protocol', 'decoding_error', 'transport_error',
                              'total_deadline', 'cancelled', 'runtime_error', 'stream_eof'},
        'http_failure': {'request_rejected', 'authentication_rejected', 'balance_rejected',
                         'permission_rejected', 'rate_limited', 'provider_unavailable'},
        'request_mode': {'strict', 'standard'},
        'error_body_state': {'complete', 'truncated', 'interrupted', 'timed_out'},
        'error_body_encoding': {'identity', 'encoded'},
        'thinking_type': {'enabled', 'disabled'},
        'output_limit_mode': {'explicit', 'provider_default'},
        'reasoning_effort': {'none', 'low', 'high'},
        'provider_error_type': {'invalid_request_error', 'authentication_error', 'permission_error',
                               'rate_limit_error', 'api_error', 'server_error', 'insufficient_quota'},
        'provider_error_code': {'invalid_request_error', 'invalid_json', 'invalid_api_key',
                               'model_not_found', 'context_length_exceeded', 'insufficient_quota',
                               'rate_limit_exceeded', 'server_error'},
        'provider_message_type': {'string', 'object', 'array', 'number', 'boolean', 'null', 'missing'},
        'finish_reason': {'stop', 'tool_calls', 'length', 'content_filter',
                          'insufficient_system_resource', 'aborted', 'repetition_truncation', 'unrecognized'},
        'json_error_category': {'expected_value', 'expected_property_name', 'expected_comma',
                                'expected_colon', 'extra_data', 'unterminated_string',
                                'invalid_escape', 'invalid_control_character', 'invalid_unicode_escape',
                                'unexpected_bom', 'other_json_error'},
        'schema_rule': {'missing_field', 'extra_field', 'wrong_type', 'enum_mismatch',
                        'minimum_mismatch', 'maximum_mismatch', 'any_of_mismatch', 'unavailable_tool'},
        'expected_type': {'object', 'array', 'string', 'integer', 'boolean', 'anyOf'},
        'actual_type': {'object', 'array', 'string', 'integer', 'number', 'boolean', 'null'},
    }
    labels['error_body_failure'] = labels['transport_failure'] | {'body_read_deadline'}
    safe = {}
    for key, values in labels.items():
        value = diagnostic.get(key)
        if isinstance(value, str) and value in values:
            safe[key] = value
    for key in ('choice_count', 'tool_call_index', 'arguments_chars', 'arguments_bytes', 'execution_arguments_bytes', 'json_line', 'json_column', 'json_position', 'writer_unclosed_depth',
                'frame_index', 'frame_bytes', 'request_bytes', 'error_body_bytes', 'output_token_limit',
                'received_bytes', 'received_frames', 'received_frame_bytes', 'public_content_bytes',
                'reasoning_bytes', 'tool_argument_bytes', 'elapsed_ms', 'last_data_age_ms',
                'last_model_data_age_ms', 'request_timeout_ms', 'http_connect_timeout_ms',
                'http_read_timeout_ms', 'http_write_timeout_ms', 'http_pool_timeout_ms'):
        value = diagnostic.get(key)
        if type(value) is int and 0 <= value < 2**31:
            safe[key] = value
    for key in ('arguments_sha256', 'execution_arguments_sha256', 'schema_sha256',
                'frame_sha256', 'request_sha256', 'error_body_sha256'):
        digest = diagnostic.get(key)
        if isinstance(digest, str) and re.fullmatch('[a-f0-9]{64}', digest):
            safe[key] = digest
    value = diagnostic.get('provider_status')
    if type(value) is int and 100 <= value <= 599:
        safe['provider_status'] = value
    if type(diagnostic.get('stream')) is bool:
        safe['stream'] = diagnostic['stream']
    if type(diagnostic.get('transport_statistics_capped')) is bool:
        safe['transport_statistics_capped'] = diagnostic['transport_statistics_capped']
    tool_name = diagnostic.get('tool_name')
    if isinstance(tool_name, str) and tool_name in declared_tool_names(tools):
        safe['tool_name'] = tool_name
    terms = diagnostic.get('provider_message_terms')
    allowed_terms = set(HTTP_ERROR_TERMS)
    if isinstance(terms, list) and len(terms) <= len(allowed_terms) and all(
            isinstance(term, str) and term in allowed_terms for term in terms):
        safe['provider_message_terms'] = sorted(set(terms))
    tool_names = diagnostic.get('provider_tools')
    if isinstance(tool_names, list) and len(tool_names) <= len(HTTP_ERROR_TOOLS) and all(
            isinstance(name, str) and name in HTTP_ERROR_TOOLS for name in tool_names):
        safe['provider_tools'] = sorted(set(tool_names))
    fields = {'content', 'summary', 'evidence', 'limitations', 'structured', 'stage', 'version', 'name',
              'shot_id', 'page_id', 'entry_seq', 'before_seq', 'offset', 'limit',
              'task_interpretation', 'question', 'context', 'options', 'recommended', 'label', 'description'}
    def declared_fields(schema):
        properties = schema.get('properties', {})
        fields.update(properties)
        for child in properties.values():
            declared_fields(child)
        if 'items' in schema:
            declared_fields(schema['items'])
    # Only application-owned schema keys join the path whitelist; never read
    # keys from a model response or a caller-supplied diagnostic.
    declared_fields(TASK_INTERPRETATION_TOOL['input_schema'])
    for shape in SHAPES.values():
        for field in shape.fields:
            fields.add(field.name)
            fields.update(field.columns)
    path = diagnostic.get('schema_path')
    def safe_path(path):
        return isinstance(path, list) and len(path) <= 12 and all(
            (isinstance(part, str) and part in fields) or (type(part) is int and 0 <= part < 2**31)
            for part in path)
    if safe_path(path):
        safe['schema_path'] = path.copy()
    request_fields = fields | {'tools', 'function', 'parameters', 'properties', 'items', 'required',
        'additionalProperties', 'type', 'enum', 'anyOf', 'minimum', 'maximum', 'messages', 'model',
        'thinking', 'reasoning_effort', 'max_tokens', 'max_completion_tokens', 'tool_choice',
        'response_format', 'stream'}
    request_path = diagnostic.get('request_path')
    if (isinstance(request_path, list) and 0 < len(request_path) <= 24 and all(
            (isinstance(part, str) and part in request_fields) or
            (type(part) is int and 0 <= part < 2**31) for part in request_path)):
        safe['request_path'] = request_path.copy()
    defaults = diagnostic.get('defaulted_paths')
    if (isinstance(defaults, list) and 0 < len(defaults) <= 512
            and all(safe_path(path) for path in defaults)):
        safe['defaulted_paths'] = [path.copy() for path in defaults]
    return safe


def record_response_rejection(store, run_id, call_id, *, role, stage, diagnostic, tools=()):
    """Persist bounded metadata only; never response text or tool arguments."""
    store.append_event(run_id, 'agent_response_rejected', call_id=call_id,
                       role=role, stage=stage, diagnostic=safe_response_diagnostic(diagnostic, tools=tools))


def request_transport_diagnostic(payload, provider):
    """Freeze public options from the actual wire payload, never credentials/content."""
    return safe_response_diagnostic({
        'provider': provider.lower(),
        'request_mode': 'strict' if any(t.get('function', {}).get('strict') is True
                                      for t in payload.get('tools', [])) else 'standard',
        'stream': payload.get('stream'),
        'thinking_type': payload.get('thinking', {}).get('type'),
        'reasoning_effort': payload.get('reasoning_effort'),
        'output_token_limit': payload.get('max_tokens', payload.get('max_completion_tokens')),
        'output_limit_mode': ('explicit' if 'max_tokens' in payload or 'max_completion_tokens' in payload
                              else 'provider_default'),
    })


def record_model_transport(store, run_id, call_id, *, role, stage, diagnostic, tools=()):
    """Append one safe transport summary to the existing call, not another task."""
    safe = safe_response_diagnostic(diagnostic, tools=tools)
    if safe.get('format') != 'model_transport' or safe.get('code') != 'transport_observed':
        raise ValueError('invalid model transport metadata')
    store.append_event(run_id, 'agent_model_transport', call_id=call_id,
                       role=role, stage=stage, diagnostic=safe)


def record_response_normalization(store, run_id, call_id, *, role, stage, diagnostic):
    safe = safe_response_diagnostic(diagnostic)
    if safe.get('code') != 'empty_defaults_applied' or not safe.get('defaulted_paths'):
        raise ValueError('invalid response normalization metadata')
    store.append_event(run_id, 'agent_response_normalized', call_id=call_id,
                       role=role, stage=stage, diagnostic=safe)


def save_writer_root_closure(store, run_id, call_id, *, role, stage, closure):
    """Private immutable evidence, never exposed to Agent tools or used to execute/replay."""
    if (role != 'writer' or stage != 'writing'
            or not isinstance(call_id, str) or not re.fullmatch(r'[a-f0-9-]{36}', call_id)):
        raise ValueError('invalid writer root closure identity')
    diagnostic = safe_response_diagnostic(closure.diagnostic())
    original = closure.original_arguments.encode('utf-8')
    execution = closure.execution_arguments.encode('utf-8')
    if (diagnostic.get('code') != 'root_object_closed'
            or diagnostic.get('processing_method') != 'writer_root_close_v1'
            or execution != original + b'}'
            or diagnostic.get('arguments_sha256') != hashlib.sha256(original).hexdigest()
            or diagnostic.get('execution_arguments_sha256') != hashlib.sha256(execution).hexdigest()):
        raise ValueError('invalid writer root closure evidence')
    with store.run_lock(run_id):
        store.require_healthy(run_id)
        root = store.run_dir(run_id).resolve()
        folder = root / 'response-evidence' / call_id
        if not folder.resolve().is_relative_to(root):
            raise ValueError('response evidence is outside the run')
        folder.mkdir(parents=True, exist_ok=True)
        # Store exact UTF-8 argument values, without newline/BOM or JSON re-encoding.
        # An interrupted pair stays diagnostic-only; it cannot grant execution.
        for name, raw in (('writer-original.txt', original), ('writer-execution.json', execution)):
            path = folder / name
            if not path.resolve().is_relative_to(root):
                raise ValueError('response evidence is outside the run')
            with path.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        store.append_event(run_id, 'agent_response_normalized', call_id=call_id,
                           role=role, stage=stage, diagnostic=diagnostic)
    closure.evidence_saved = True
