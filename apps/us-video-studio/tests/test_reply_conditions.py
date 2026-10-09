"""Grounded reply-only limits; original text, usage and permissions preserved."""
import copy
import hashlib
import json

import pytest

from studio.conversation_input import (constraints_for_inputs, constraints_for_scope,
    interpretation_for, record_task_interpretation, reply_constraints_for_input)
from studio.models import PipelineError, STAGES
from studio.reply_conditions import check_reply, table_shape
from studio.store import Store
from studio.task_contract import with_reply_check
from test_context_projection import RUN, env, executing, clause, proposal
from test_image_analysis import scene


TEXT = '仅分析图片，以两行表回复，全文不超过400字；不创作或生成。'


def reply_row(quote, key='reply.max_chars', value='400', **extra):
    row = clause(quote, key=key, value=value, lifetime='turn', **extra)
    row['scope']['stages'] = []
    return row


def declared(receipt, *rows):
    return proposal(receipt, *rows, mode='analyze', deliverable='analysis', format='photo',
                    allowed_stages=[])


def bound(env, *rows, text=TEXT):
    store, _ = env
    receipt = executing(env, text)
    saved = record_task_interpretation(store, RUN, receipt, declared(receipt, *rows))
    return receipt, saved


def table(chars):
    base = '|图片|可见内容|\n|---|---|\n|图一||\n|图二|未知|'
    return base.replace('图一||', '图一|' + '中' * (chars - len(base)) + '|')


def test_reply_scope_persists_without_creative_authority_and_is_not_project_policy(env):
    store, pipe = env
    receipt, saved = bound(env, reply_row('全文不超过400字'),
        reply_row('以两行表回复', 'reply.table_rows', '2'),
        reply_row('以两行表回复', 'reply.format', 'markdown_table'))
    assert saved['task']['allowed_stages'] == []
    assert saved['project_constraints'] == [] and len(saved['reply_constraints']) == 3
    assert constraints_for_inputs(store, RUN, [receipt['message_id']]) == []
    assert len(reply_constraints_for_input(Store(store.root), RUN, receipt['message_id'])) == 3
    assert not store.list_approvals(RUN)
    assert all(store.latest_artifact(RUN, stage) is None for stage in STAGES)
    assert interpretation_for(store, RUN, receipt['message_id'])['constraints'] == saved['constraints']


@pytest.mark.parametrize('chars,expected', [(400, 'satisfied'), (401, 'incomplete')])
def test_final_full_unicode_text_400_and_401_without_truncation(env, chars, expected):
    receipt, saved = bound(env, reply_row('全文不超过400字'),
        reply_row('以两行表回复', 'reply.table_rows', '2'),
        reply_row('以两行表回复', 'reply.format', 'markdown_table'))
    text = table(chars)
    assert len(text) == chars
    check = check_reply(text, receipt, saved, saved['reply_constraints'])
    assert check['status'] == expected and check['char_count'] == chars
    assert check['table_data_rows'] == 2
    assert text == table(chars)
    delivery = with_reply_check({'goal': 'analysis', 'status': 'complete',
                                'missing': [], 'note': '已保存 2/2 张真实观察。'}, check)
    assert delivery['reply_check'] == check
    assert delivery['status'] == ('complete' if chars == 400 else 'incomplete')
    if chars == 401:
        assert check['missing'] == [{'category': 'max_chars',
            'constraint_ids': [saved['constraints'][0]['id']], 'expected': 400, 'actual': 401}]
        assert '401' in delivery['note'] and '2/2' in delivery['note']


@pytest.mark.parametrize('text,shape,rows', [
    ('|H|J|\n|---|:---:|\n|A|B|\n|C|D|', 'markdown_table', 2),
    ('H | J\n--- | ---\nA | B', 'invalid_or_multiple', None),
    ('|H|J|\n|---|---|', 'markdown_table', 0),
    (r'|H|J|' + '\n|---|---|\n|A\\|inside|B|', 'invalid_or_multiple', None),
    ('text before\n|H|J|\n|---|---|\n|A|B|', 'invalid_or_multiple', None),
    ('|H|J|\n|---|---|\n|A|B|\n|X|Y|\n|---|---|', 'invalid_or_multiple', None),
    ('|H|J|\n|---|---|\n|A|B|C|', 'invalid_or_multiple', None),
    ('Ordinary prose 😀', 'text', None),
    ('|Header|\n|---|\n|First|\n|Second|', 'markdown_table', 2),
    ('|Header|\n|:---:|\n|First|', 'markdown_table', 1),
    ('Header\n|:---:|\nFirst', 'invalid_or_multiple', None),
    ('Header\n---\nFirst', 'text', None),
    ('\n\n|H|\n|---|\n|A|\n\n', 'markdown_table', 1),
    ('|H|J|\n\n|---|---|\n|A|B|', 'invalid_or_multiple', None),
    ('|H|J|\n|---|---|\n|A|B|\n\n|C|D|', 'invalid_or_multiple', None),
    ('|H|\n|---|\n\n|A|', 'invalid_or_multiple', None),
    ('普通文字含 A | B\n下一行仍是普通文字', 'text', None),
    ('|H|J|\n|--|---|\n|A|B|', 'invalid_or_multiple', None),
])
def test_single_markdown_table_shape_excludes_header_and_separator(text, shape, rows):
    assert table_shape(text) == (shape, rows)


def test_format_without_row_limit_still_validates_table(env):
    receipt, saved = bound(env, reply_row('以两行表回复', 'reply.format', 'markdown_table'))
    assert check_reply(table(100), receipt, saved, saved['reply_constraints'])['status'] == 'satisfied'
    check = check_reply('Ordinary prose', receipt, saved, saved['reply_constraints'])
    assert check['missing'][0] == {'category': 'table_format',
        'constraint_ids': [saved['constraints'][0]['id']], 'expected': 'markdown_table', 'actual': 'text'}


def test_plain_text_with_literal_pipe_is_accepted_but_real_table_is_not(env):
    receipt, saved = bound(env, reply_row('以两行表回复', 'reply.format', 'text'))
    check = check_reply('普通文字含 A | B\n下一行仍是普通文字', receipt, saved, saved['reply_constraints'])
    assert check['status'] == 'satisfied'
    rejected = check_reply(table(100), receipt, saved, saved['reply_constraints'])
    assert rejected['missing'][0]['actual'] == 'markdown_table'


@pytest.mark.parametrize('quote,key,value', [
    ('今天2026年10月9日查看400张图片', 'reply.max_chars', '400'),
    ('材料有2个分隔', 'reply.table_rows', '2'),
    ('最多22行数据', 'reply.table_rows', '2'),
    ('最多1400字符', 'reply.max_chars', '400'),
    ('a 400 pixel image', 'reply.max_chars', '400'),
    ('400 characters are visible in the image', 'reply.max_chars', '400'),
    ('全文不超过400字', 'reply.max_chars', '0400'),
])
def test_typed_limit_cannot_take_arbitrary_numbers_or_noncanonical_values(env, quote, key, value):
    store, _ = env
    receipt = executing(env, quote)
    before = store.read_events(RUN)
    with pytest.raises(PipelineError):
        record_task_interpretation(store, RUN, receipt, declared(receipt, reply_row(quote, key, value)))
    assert store.read_events(RUN) == before and interpretation_for(store, RUN, receipt['message_id']) is None


@pytest.mark.parametrize('quote,key,value', [
    ('全文不超过400字', 'reply.max_chars', '400'),
    ('400字符以内', 'reply.max_chars', '400'),
    ('最多四百字符', 'reply.max_chars', '400'),
    ('at most 400 characters', 'reply.max_chars', '400'),
    ('no more than 400 chars', 'reply.max_chars', '400'),
    ('two data rows', 'reply.table_rows', '2'),
    ('2-row table', 'reply.table_rows', '2'),
    ('用一个两行短表', 'reply.table_rows', '2'),
])
def test_numeric_quote_support_accepts_matching_count_with_unit(env, quote, key, value):
    receipt, saved = bound(env, reply_row(quote, key, value), text=quote)
    assert len(saved['reply_constraints']) == 1


@pytest.mark.parametrize('over', [
    {'lifetime': 'project'}, {'paths': [['segments', 0]]},
])
def test_reply_scope_rejects_persistent_or_artifact_paths_before_write(env, over):
    store, _ = env
    receipt = executing(env, TEXT)
    row = reply_row(TEXT)
    row['scope'].update(over)
    before = store.read_events(RUN)
    with pytest.raises(PipelineError, match='回复条件必须'):
        record_task_interpretation(store, RUN, receipt, declared(receipt, row))
    assert store.read_events(RUN) == before


def test_typed_reply_limit_cannot_be_disguised_as_intake_condition(env):
    store, _ = env
    receipt = executing(env, TEXT)
    row = reply_row(TEXT)
    row['scope']['stages'] = ['intake']
    with pytest.raises(PipelineError, match='回复格式条件只能'):
        record_task_interpretation(store, RUN, receipt, declared(receipt, row))


def test_current_reply_domain_does_not_inherit_history_and_compaction_is_separate(env):
    from studio.context_projection import build_conversation_context
    store, pipe = env
    receipt, saved = bound(env, reply_row('全文不超过400字'))
    for n in range(25):
        store.append_chat(RUN, 'user' if n % 2 == 0 else 'assistant', f'Historical turn {n}')
    context = build_conversation_context(store, pipe, RUN, current_receipt=receipt)
    assert context['projection']['current_reply_constraints'] == saved['reply_constraints']
    assert context['projection']['effective_constraints'] == []
    next_input = executing(env, '现在只查询状态')
    assert reply_constraints_for_input(store, RUN, next_input['message_id']) == []
    later = build_conversation_context(store, pipe, RUN, current_receipt=next_input)
    assert 'current_reply_constraints' not in later['projection']


@pytest.mark.parametrize('operation', ['replace', 'release', 'conflict'])
def test_reply_and_artifact_references_cannot_cross_domains(env, operation):
    store, _ = env
    old = executing(env, '整个项目的回答要简洁')
    saved = record_task_interpretation(store, RUN, old,
        proposal(old, clause(old['text'], key='answer.style', value='简洁')))
    new = executing(env, '这轮回答详细一些')
    row = reply_row(new['text'], 'answer.style', '详细',
        **({'conflict_with': [saved['constraints'][0]['id']]} if operation == 'conflict'
           else {'operation': operation, 'supersedes': [saved['constraints'][0]['id']]}))
    before = store.read_events(RUN)
    with pytest.raises(PipelineError):
        record_task_interpretation(store, RUN, new, declared(new, row), prior_user_input_ids=[old['message_id']])
    assert store.read_events(RUN) == before
    assert constraints_for_inputs(store, RUN, [old['message_id']]) == saved['project_constraints']


def test_reply_replacement_is_same_domain_and_only_new_source_has_authority(env):
    store, _ = env
    old, saved = bound(env, reply_row('全文不超过400字'))
    new = executing(env, '这一轮改为最多300字符')
    row = reply_row(new['text'], value='300', operation='replace', supersedes=[saved['constraints'][0]['id']])
    result = record_task_interpretation(store, RUN, new, declared(new, row), prior_user_input_ids=[old['message_id']])
    assert result['project_constraints'] == [] and len(result['reply_constraints']) == 1
    assert result['reply_constraints'][0]['source_message_id'] == new['message_id']
    assert reply_constraints_for_input(store, RUN, old['message_id']) == saved['reply_constraints']
    assert len(constraints_for_inputs(store, RUN, [old['message_id'], new['message_id']], domain='reply')) == 1


def test_actual_current_unresolved_reply_condition_is_not_claimed_checked(env):
    receipt, saved = bound(env, reply_row(TEXT, 'answer.format', '未确定', ambiguity='用户没有给出所需格式'))
    check = check_reply('观察已保存', receipt, saved, saved['reply_constraints'])
    assert check['missing'][0]['category'] == 'unresolved_condition'
    assert with_reply_check({'status': 'complete', 'missing': [], 'note': ''}, check)['status'] == 'incomplete'


@pytest.mark.parametrize('mutation', ['source', 'hash', 'quote', 'scope', 'origin'])
def test_final_check_rejects_non_original_condition_or_source(env, mutation):
    receipt, saved = bound(env, reply_row('全文不超过400字'))
    rows = copy.deepcopy(saved['reply_constraints'])
    key, value = {'source': ('source_message_id', 'other'), 'hash': ('source_sha256', 'f' * 64),
                  'quote': ('quote', 'invented'), 'scope': ('scope', {'stages': ['writing']}),
                  'origin': ('origin', 'unknown')}[mutation]
    rows[0][key] = value
    with pytest.raises(PipelineError, match='真实输入'):
        check_reply('safe', receipt, saved, rows)


def test_general_semantic_reply_guidance_has_no_fabricated_mechanical_pass(env):
    receipt, saved = bound(env, reply_row(TEXT, 'answer.format', '简洁中文'))
    assert check_reply('Response', receipt, saved, saved['reply_constraints']) is None


def test_checked_ids_use_same_requirements_without_claiming_preferences_or_semantics(env):
    from studio.reply_conditions import expected_check_ids
    receipt, saved = bound(env, reply_row('全文不超过400字'),
        reply_row('以两行表回复', 'reply.table_rows', '2', polarity='prefer'),
        reply_row(TEXT, 'answer.scope', '简洁'),
        reply_row(TEXT, 'answer.question', '格式待确定', intent='question'),
        reply_row(TEXT, 'answer.pending', '需要澄清', ambiguity='两种格式无法确认'))
    expected = [saved['constraints'][index]['id'] for index in (0, 4)]
    assert expected_check_ids(saved) == expected
    assert check_reply('实际原文', receipt, saved, saved['reply_constraints'])['constraint_ids'] == expected
    with pytest.raises(PipelineError, match='遗漏'):
        check_reply('实际原文', receipt, saved, saved['reply_constraints'][:1])


def test_reply_limits_are_only_in_root_finishing_not_writer_input(tmp_path, monkeypatch):
    from test_finishing_phase import harness, reply
    text = 'Write the complete script. Reply with two data rows, at most 400 characters.'
    rows = [reply_row('at most 400 characters'), reply_row('two data rows', 'reply.table_rows', '2'),
            reply_row('Reply with two data rows', 'reply.format', 'markdown_table')]
    output = table(200)
    orch, store, pipe, ledger, run, seen, work, contexts, _ = harness(tmp_path, monkeypatch,
        source_text=text, reply_rows=rows, finish=reply(text=output))
    result = orch.send(text)
    assert result.text == output and result.deliverable['status'] == 'complete'
    check = result.deliverable['reply_check']
    assert check['status'] == 'satisfied' and check['char_count'] == 200
    assert len(contexts) == 1 and len(seen) == len(ledger.entries(run)) == 4
    final = seen[-1]
    assert not final.get('tools')
    data = json.loads(final['messages'][-1]['content'])
    assert data['reply_constraints'] == orch._reply_constraints
    assert data['source_message_id'] == check['source_message_id']
    assert '格式和长度优先于默认段落安排' in final['messages'][0]['content']
    writers = [payload for payload in seen if any(tool['function']['name'] == 'submit_stage'
                                                  for tool in payload.get('tools', []))]
    assert len(writers) == 1
    writer_text = json.dumps(writers[0], ensure_ascii=False)
    assert not any(identity in writer_text for identity in check['constraint_ids'])
    assert 'reply_constraints' not in orch._role_turn_scope()
    assert not pipe.is_complete(run, 'writing') and not store.list_approvals(run)[-1].stage == 'writing'


def test_late_reply_condition_change_cannot_publish_stale_finishing(tmp_path, monkeypatch):
    from studio.agents.client import NonRetryableStageError
    from test_finishing_phase import harness, reply
    text = 'Write the complete script. Reply with at most 400 characters.'
    def changed(orch):
        orch._reply_constraints = []
    orch, store, _, ledger, run, seen, _, _, _ = harness(tmp_path, monkeypatch,
        source_text=text, reply_rows=[reply_row('at most 400 characters')],
        finish=reply(text='Saved real script.'), before_finish=changed)
    with pytest.raises(NonRetryableStageError, match='来源已变化'):
        orch.send(text)
    assert len(seen) == len(ledger.entries(run)) == 4
    assert store.latest_artifact(run, 'writing').version == 1
    assert not any(row['role'] == 'assistant' for row in store.read_chat(run))


def test_finishing_keeps_provider_outer_whitespace_and_counts_it(tmp_path, monkeypatch):
    from test_finishing_phase import harness, reply
    text = 'Write the complete script. Reply with two data rows, at most 400 characters.'
    output = ' \n' + table(400) + '\n'
    rows = [reply_row('at most 400 characters'), reply_row('two data rows', 'reply.table_rows', '2'),
            reply_row('Reply with two data rows', 'reply.format', 'markdown_table')]
    orch, store, _, ledger, run, seen, _, _, _ = harness(tmp_path, monkeypatch,
        source_text=text, reply_rows=rows, finish=reply(text=output))
    result = orch.send(text)
    assert result.text == output and result.deliverable['status'] == 'incomplete'
    assert result.deliverable['reply_check']['char_count'] == 403
    assert result.deliverable['reply_check']['table_data_rows'] == 2
    assert store.read_chat(run)[-1]['text'] == output
    assert len(seen) == len(ledger.entries(run)) == 4


@pytest.mark.parametrize('preserve', [False, True])
def test_work_loop_dynamic_reply_binding_and_legacy_strip_against_mock_http(monkeypatch, preserve):
    from studio.agents.client import run_agent_loop
    from studio.agents.openai_compat import OpenAICompatClient
    from test_finishing_phase import mock_http, reply
    output = ' \nFinal actual text.\n '
    responses = [reply(('bind_reply', {})), reply(text=output)]
    seen = mock_http(monkeypatch, lambda _: responses.pop(0))
    active, checked = [], []
    def bind(_name, _args):
        active.append(True)
        return 'Actual reply conditions accepted'
    def current_binding():
        checked.append(bool(active))
        return bool(active)
    result = run_agent_loop(OpenAICompatClient('https://example.invalid/v1', 'synthetic-only'),
        model='synthetic', system='Synthetic role', messages=[{'role': 'user', 'content': 'Task'}],
        tools=[{'name': 'bind_reply', 'input_schema': {'type': 'object'}}], execute_tool=bind,
        **({'preserve_final_text': current_binding} if preserve else {}))
    assert result.text == (output if preserve else output.strip())
    assert len(seen) == 2 and not responses
    assert checked == ([True] if preserve else [])


@pytest.mark.parametrize('preserve', [False, True])
def test_direct_finishing_preserves_only_explicit_source_bound_output(monkeypatch, preserve):
    from studio.agents.client import FinishingContext, LoopResult, complete_finishing_turn
    from studio.agents.openai_compat import OpenAICompatClient
    from test_finishing_phase import mock_http, reply
    output = '\nActual conclusion. \n'
    seen = mock_http(monkeypatch, lambda _: reply(text=output))
    result = complete_finishing_turn(OpenAICompatClient('https://example.invalid/v1', 'synthetic-only'),
        model='synthetic', context=FinishingContext(system='Read only', messages=[],
        check_current=lambda: None, stage='writing'),
        work_result=LoopResult(text='', messages=[], rounds=12, stopped_because='work_complete'),
        **({'preserve_final_text': lambda: True} if preserve else {}))
    assert result.text == (output if preserve else output.strip())
    assert len(seen) == 1 and not seen[0].get('tools') and result.rounds == 13


def test_old_strict_schema_wire_structure_and_sentinel_decoder_remain_compatible(monkeypatch):
    from studio.tools import registry
    current = registry._strict_task_interpretation_schema()
    old_tool = copy.deepcopy(registry.TASK_INTERPRETATION_TOOL)
    old_tool['input_schema']['properties']['constraints']['items']['properties']['scope']['properties']['stages']['minItems'] = 1
    monkeypatch.setattr(registry, 'TASK_INTERPRETATION_TOOL', old_tool)
    assert registry._strict_task_interpretation_schema() == current
    # The projection's exact-schema detector still recognizes old frozen wire.
    assert current['properties']['constraints']['items']['properties']['scope']['properties']['stages'].get('minItems') is None
    from studio.agents.deepseek_client import _matches_strict_schema
    arguments = declared({'text': TEXT}, reply_row('全文不超过400字'))
    original = copy.deepcopy(arguments)
    arguments.update(reuse_artifact='not_applicable', video_use='not_applicable', image_use='not_applicable')
    frozen_wire = copy.deepcopy(arguments)
    assert _matches_strict_schema(arguments, current)
    assert registry.is_strict_writer_wire_schema('propose_task_interpretation', current)
    assert registry.strict_writer_execution_arguments('propose_task_interpretation', arguments, current) == original
    assert arguments == frozen_wire


@pytest.mark.parametrize('chars,prefix,suffix,expected', [
    (400, '', '', 'complete'), (401, '', '', 'incomplete'),
    (400, ' ', '', 'incomplete'), (400, '', '\n', 'incomplete'),
    (398, '\n', '\n', 'complete')])
@pytest.mark.parametrize('scene', [12], indirect=True)
def test_actual_mock_http_image_job_checks_original_final_text_without_any_repair_call(scene, monkeypatch, chars, prefix, suffix, expected):
    from studio.agent_images import observe, saved_observations
    from studio.agents.openai_compat import OpenAICompatClient
    from studio.ledger import Ledger
    from test_deepseek_client import transport
    from test_finishing_phase import reply
    from test_image_understanding import add_second, answer, configure, frozen, observer_reply
    from test_image_analysis import wait_job
    from test_server import RUN as HTTP_RUN

    store, settings = scene[1], scene[5]
    add_second(scene)
    connections, _ = configure(scene)
    route = frozen(scene, connections)
    items = [{'name': name, 'purpose': 'product'} for name in ('fixture.png', 'second.png')]
    request_text = '只查看本项目两张图片，以两行表回复，全文不超过400字；不出创作或生成。'
    payload = {'task': {'quote': request_text, 'mode': 'analyze', 'deliverable': 'analysis',
                       'format': 'photo', 'allowed_stages': [], 'pause_after': False},
               'constraints': [reply_row('全文不超过400字'),
                   reply_row('以两行表回复', 'reply.table_rows', '2'),
                   reply_row('以两行表回复', 'reply.format', 'markdown_table')]}
    original = prefix + table(chars).replace('中', '😀', 1) + suffix
    full_chars = len(original)
    wire_replies = [observer_reply(answer(first=n), route[1].progress_stream) for n in (1, 2)]
    wire_replies += [reply(('observe_images', {'images': items, 'task_interpretation': payload})), reply(text=original)]
    seen = transport(monkeypatch, wire_replies)
    first = observe(store, settings, HTTP_RUN, items, endpoint=None, emit=lambda _: None,
        check_cancel=lambda: None, freeze_route=lambda: route)
    assert len(first['observations']) == len(seen) == 2
    ledger = Ledger(store, settings)
    visual_before = [row for row in ledger.entries(HTTP_RUN) if row.stage == 'image_analysis']
    events_before = len(store.read_events(HTTP_RUN))
    producer = OpenAICompatClient('https://example.invalid/v1', 'synthetic-only')
    # Reuse the fixture's sole writer; its root client now crosses real parsing
    # and transport against Mock HTTP. No second app may claim the same lease.
    monkeypatch.setattr(scene[2], 'complete', producer.complete)
    response = scene[0].post(f'/api/runs/{HTTP_RUN}/jobs', json={
        'text': request_text, 'request_id': f'reply-only-{chars}'})
    assert response.status_code == 200, response.text
    job = wait_job(scene[0], response.json())
    assert job['status'] == ('succeeded' if expected == 'complete' else 'blocked'), job
    delivery = job['deliverable']
    assert delivery['goal'] == 'analysis' and delivery['status'] == expected
    check = delivery['reply_check']
    assert check['char_count'] == full_chars and check['table_data_rows'] == 2
    assert check['status'] == ('satisfied' if expected == 'complete' else 'incomplete')
    assert store.read_chat(HTTP_RUN)[-1]['text'] == original
    assert len(seen) == 4 and not wire_replies  # Two initial pixel reports, two root calls only.
    assert [row for row in ledger.entries(HTTP_RUN) if row.stage == 'image_analysis'] == visual_before
    root_usage = [row for row in ledger.entries(HTTP_RUN) if row.role == 'orchestrator']
    assert len(root_usage) == 2
    assert all(row.input_tokens == 31 and row.output_tokens == 17 and row.usage_status == 'known' for row in root_usage)
    events = store.read_events(HTTP_RUN)[events_before:]
    bound_event = next(row for row in events if row['kind'] == 'user_task_interpretation_recorded')
    assert bound_event['interpretation']['task']['allowed_stages'] == []
    assert not any(row['kind'] in {'artifact_submitted', 'production_draft_prepared', 'approval_recorded',
                                   'execution_authorized', 'creative_context_selected'} for row in events)
    assert all(store.latest_artifact(HTTP_RUN, stage) is None for stage in STAGES)
    assert not store.list_approvals(HTTP_RUN) and len(saved_observations(store, HTTP_RUN)) == 2
    next_root_payload = json.loads(seen[3].content)
    result = next(row['content'] for row in next_root_payload['messages'] if row['role'] == 'tool')
    assert 'reply_constraints' in result and 'reply.max_chars' in result
    assert all('reply.max_chars' not in request.content.decode() for request in seen[:2])
    assert check['source_sha256'] == hashlib.sha256(request_text.encode()).hexdigest()
