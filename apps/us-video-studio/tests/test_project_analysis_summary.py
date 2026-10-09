"""Pure image summaries use real temporary receipts, pixels and saved observations."""
from copy import deepcopy
import hashlib

import pytest

from fakes import final_turn, tool_turn
from studio import conversation_input
from studio.agent_images import bind_image_analysis
from studio.agents.client import Turn, Usage
from studio.materials import resolve
from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.project_brief import content_format, latest_execution, project_brief
from studio.tools.registry import STRICT_ABSENT
from test_agent_images import call, result, answering_observer
from test_bailian_omni import native
from test_image_analysis import scene, wait_job
from test_image_analysis_task_contract import proposal, six, TEXT
from test_image_understanding import configure, answer
from test_natural_reference import scripted
from test_server import RUN


REPORT = '两张图中的可见结构已列出。材质与功能仍未知，图中文字仅为宣传。'


def begin(store, request='analysis', *, mode='analyze', format='photo', declared=True, text=TEXT, conditions=None):
    receipt = conversation_input.receive(store, RUN, text, request)
    conversation_input.accept(store, RUN, receipt)
    job_id, turn_id = request + '-job', request + '-turn'
    store.append_event(RUN, 'job_created', job_id=job_id, request_id=request,
                       user_input_id=receipt['message_id'], job_kind='conversation')
    conversation_input.start_execution(store, RUN, receipt)
    if declared:
        proposed = proposal(text, mode=mode, format=format,
            deliverable='analysis' if mode == 'analyze' else 'direction' if mode == 'produce' else 'follow_up',
            allowed_stages=['intake', 'direction'] if mode == 'produce' else [])
        proposed['constraints'] = conditions or []
        conversation_input.record_task_interpretation(store, RUN, receipt,
                                                     proposed)
    store.append_event(RUN, 'agent_turn_scope_started', turn_id=turn_id,
                       user_input_id=receipt['message_id'])
    store.append_event(RUN, 'job_status', job_id=job_id, status='running')
    return receipt, job_id, turn_id


def finish(store, source, job_id, *, job_status='succeeded', deliverable_status='complete',
           report=REPORT, reply_job=None, reply_source=None, goal='analysis', reply_check=...):
    if report is not None:
        index = len(store.read_chat(RUN))
        store.append_chat(RUN, 'assistant', report)
        store.append_event(RUN, 'agent_reply_recorded', chat_index=index,
                           source_message_id=reply_source or source['message_id'],
                           job_id=reply_job or job_id)
    store.append_event(RUN, 'job_deliverable', job_id=job_id,
                       deliverable={'goal': goal, 'status': deliverable_status,
                                    **({'reply_check': reply_check} if reply_check is not ... else {})})
    store.append_event(RUN, 'job_status', job_id=job_id, status=job_status)


def observed(native, *, count=1, text=TEXT, conditions=None):
    store = native[1]
    items = six(native) if count == 6 else [{'name': 'fixture.png', 'purpose': 'product'}]
    source, job_id, turn_id = begin(store, text=text, conditions=conditions)
    interpretation = conversation_input.interpretation_for(store, RUN, source['message_id'])
    bound = bind_image_analysis(store, RUN, source, interpretation, items, turn_id=turn_id)
    native[2].turn = result(count)
    call(native, bound)
    return source, job_id, turn_id


def summary(store):
    return project_brief(Pipeline(store).state(RUN).to_dict(), latest_execution(store, RUN),
                         store=store, run_id=RUN)


def test_exact_complete_analysis_changes_summary_without_changing_format_title_or_permissions(native):
    store = native[1]
    store.append_event(RUN, 'creative_context_selected', selection={'intent': 'new', 'format': 'video'})
    source, job_id, turn_id = observed(native, count=6)
    finish(store, source, job_id)
    before = (store.read_run(RUN).to_dict(), store.read_events(RUN), store.read_chat(RUN))
    out = summary(store)
    assert out['title'] == '图片分析已完成' and out['phase'] == 'analysis_complete'
    assert out['stage'] is None and out['completed'] == out['total'] == 6
    assert out['action']['kind'] == 'analysis'
    assert out['analysis']['job_id'] == job_id and out['analysis']['turn_id'] == turn_id
    assert out['analysis']['source_message_id'] == source['message_id']
    assert out['analysis']['report'] == REPORT and not out['analysis']['missing']
    assert out['analysis']['analysis_only'] is True
    assert content_format(store.read_events(RUN)) == 'video'
    assert not store.list_artifacts(RUN) and not store.list_approvals(RUN)
    assert before == (store.read_run(RUN).to_dict(), store.read_events(RUN), store.read_chat(RUN))
    assert latest_execution(store, RUN)['deliverable']['status'] == 'complete'


@pytest.mark.parametrize('job_status,delivery,report,reply_job,reply_source,goal', [
    ('blocked', 'complete', REPORT, None, None, 'analysis'),
    ('failed', 'complete', REPORT, None, None, 'analysis'),
    ('cancelled', 'complete', REPORT, None, None, 'analysis'),
    ('interrupted', 'complete', REPORT, None, None, 'analysis'),
    ('succeeded', 'incomplete', REPORT, None, None, 'analysis'),
    ('succeeded', 'complete', None, None, None, 'analysis'),
    ('succeeded', 'complete', REPORT, 'foreign-job', None, 'analysis'),
    ('succeeded', 'complete', REPORT, None, 'foreign-source', 'analysis'),
    ('succeeded', 'complete', REPORT, None, None, 'writing'),
])
def test_saved_observations_or_model_claim_do_not_complete_failed_missing_or_foreign_output(
        native, job_status, delivery, report, reply_job, reply_source, goal):
    source, job_id, _ = observed(native)
    finish(native[1], source, job_id, job_status=job_status, deliverable_status=delivery,
           report=report, reply_job=reply_job, reply_source=reply_source, goal=goal)
    out = summary(native[1])
    assert out['analysis']['status'] != 'complete' and out['title'] != '图片分析已完成'
    assert out['analysis']['observed'] == 1


def test_partial_fourth_failure_uses_original_coverage_even_with_complete_claim(native, monkeypatch):
    store = native[1]
    items = six(native)
    source, job_id, turn_id = begin(store)
    bound = bind_image_analysis(store, RUN, source,
        conversation_input.interpretation_for(store, RUN, source['message_id']), items, turn_id=turn_id)
    monkeypatch.setattr('studio.agents.bailian_omni.MAX_INLINE_BYTES', len(native[4]) * 2 - 1)
    answering_observer(native, monkeypatch, fail_batch=4)
    with pytest.raises(PipelineError, match='此前已保存 3 张'):
        call(native, bound)
    finish(store, source, job_id, report='全部已完成')
    out = summary(store)
    assert out['title'] == '图片分析未完成'
    assert out['analysis']['observed'] == 3 and out['analysis']['total'] == 6
    assert len(out['analysis']['missing']) == 3
    assert len(native[2].calls) == 4


def test_current_source_bytes_and_unique_turn_are_rechecked(native):
    source, job_id, turn_id = observed(native)
    finish(native[1], source, job_id)
    assert summary(native[1])['analysis']['status'] == 'complete'
    resolve(native[1], RUN, 'fixture.png').write_bytes(b'changed pixels')
    assert summary(native[1])['analysis']['status'] == 'incomplete'
    native[1].append_event(RUN, 'agent_turn_scope_started', turn_id='other-turn',
                           user_input_id=source['message_id'])
    assert 'analysis' not in summary(native[1])


def test_status_receipts_retain_exact_report_but_new_unbound_or_creative_request_does_not(native):
    source, job_id, _ = observed(native)
    finish(native[1], source, job_id)
    for request in ('progress-one', 'progress-two'):
        new_source, new_job, _ = begin(native[1], request, mode='status', format='none')
        finish(native[1], new_source, new_job, goal='follow_up', report='原分析仍可阅读')
        out = summary(native[1])
        assert out['analysis']['job_id'] == job_id and out['analysis']['report'] == REPORT
        assert out['title'] == '图片分析已完成'
    begin(native[1], 'unbound', declared=False)
    assert 'analysis' not in summary(native[1])
    begin(native[1], 'creative', mode='produce', format='video', text='请创作午餐包的视频方向，不生成媒体。')
    assert 'analysis' not in summary(native[1])


def test_old_observations_cannot_complete_new_analysis_until_explicit_cache_invocation(native):
    source, job_id, _ = observed(native)
    finish(native[1], source, job_id)
    source2, job2, turn2 = begin(native[1], 'new-analysis')
    assert summary(native[1])['analysis']['status'] != 'complete'
    bound = bind_image_analysis(native[1], RUN, source2,
        conversation_input.interpretation_for(native[1], RUN, source2['message_id']),
        [{'name': 'fixture.png', 'purpose': 'product'}], turn_id=turn2)
    assert call(native, bound)['reused'] == 1
    finish(native[1], source2, job2)
    assert summary(native[1])['analysis']['status'] == 'complete'
    assert len(native[2].calls) == 1


def test_existing_creative_artifact_preserves_original_view_and_project_damage_still_blocks(native):
    source, job_id, _ = observed(native)
    finish(native[1], source, job_id)
    pipeline = Pipeline(native[1])
    attempt = pipeline.start_attempt(RUN, 'intake')
    pipeline.submit(RUN, 'intake', attempt.token, content='Facts', summary='Facts')
    out = summary(native[1])
    assert out['analysis']['analysis_only'] is False
    assert out['title'] != '图片分析已完成' and out['stage'] == 'direction'
    (native[1].run_dir(RUN) / 'events.jsonl').write_bytes(b'{broken journal')
    with pytest.raises(PipelineError, match='not valid JSON'):
        summary(native[1])


@pytest.mark.parametrize('scene', [12], indirect=True)
def test_actual_job_and_http_home_timeline_share_completed_analysis_projection(native, monkeypatch):
    configure(native)
    image_report = answer()['choices'][0]['message']['content']
    observer = Turn(text=image_report, model='mimo-v2.6-flash', stop_reason='end_turn', usage=Usage(100, 40))
    strict = {**proposal(), **dict.fromkeys(('reuse_artifact', 'video_use', 'image_use'), STRICT_ABSENT)}
    scripted(native, monkeypatch, [tool_turn('look', 'observe_images', {
        'images': [{'name': 'fixture.png', 'purpose': 'product'}], 'task_interpretation': strict}),
        observer, final_turn(REPORT)])
    display = native[0].get(f'/api/runs/{RUN}/timeline').json()['display_context_id']
    response = native[0].post(f'/api/runs/{RUN}/jobs', json={
        'text': TEXT, 'request_id': 'analysis-ui', 'display_context_id': display, 'reference_consent': True})
    assert response.status_code == 200, response.text
    job = wait_job(native[0], response.json())
    assert job['status'] == 'succeeded', job
    listed = next(run for run in native[0].get('/api/runs').json() if run['run_id'] == RUN)
    timeline = native[0].get(f'/api/runs/{RUN}/timeline').json()
    for out in (listed['project_brief'], timeline['brief']):
        assert out['title'] == '图片分析已完成'
        assert out['analysis']['job_id'] == job['job_id'] and out['analysis']['report'] == REPORT
        assert out['analysis']['total'] == out['analysis']['observed'] == 1
    assert listed['content_format'] is timeline['run']['content_format'] is None
    assert not native[1].list_artifacts(RUN) and not native[1].list_approvals(RUN)
    assert len(native[2].calls) == 3 and not native[2].calls[1]['tools']


def test_future_implicit_titles_stay_neutral_and_existing_manual_titles_are_never_rewritten(native):
    response = native[0].post('/api/conversations', json={
        'request_id': 'new-title', 'text': '图片事实边界验收。只分析图片，不要视频。'})
    assert response.status_code == 200, response.text
    created = response.json()
    assert created['title'] == '图片事实边界验收'
    replay = native[0].post('/api/conversations', json={
        'request_id': 'new-title', 'text': '图片事实边界验收。只分析图片，不要视频。'})
    assert replay.json()['title'] == created['title']
    before = native[1].read_run(RUN).to_dict()
    native[0].get('/api/runs')
    assert native[1].read_run(RUN).to_dict() == before
    assert not native[2].calls


REPLY_TEXT = TEXT + ' 只用Markdown表格；表格有2行数据；全文不超过400字符。'


def reply_conditions():
    # Actual grounded constraints go through the original interpretation handler.
    rows = [('reply.format', 'markdown_table', '只用Markdown表格'),
            ('reply.table_rows', '2', '表格有2行数据'),
            ('reply.max_chars', '400', '全文不超过400字符')]
    return [{'key': key, 'value': value, 'quote': quote, 'operation': 'add',
             'polarity': 'require', 'intent': 'instruction', 'ambiguity': '',
             'scope': {'lifetime': 'turn', 'stages': [], 'paths': [], 'quote': ''},
             'supersedes': [], 'conflict_with': []} for key, value, quote in rows]


def typed_reply_check(store, source, *, missing=None, char_count=400, rows=2):
    # Saved checker output is synthetic test data; this projection must consume
    # its typed measurements, not run another format/character interpreter.
    interpretation = conversation_input.interpretation_for(store, RUN, source['message_id'])
    by_key = {row['key']: row['id'] for row in interpretation['constraints']}
    failures = [{'category': category, 'constraint_ids': [by_key[key]], 'expected': expected,
                 'actual': actual} for category, key, expected, actual in (missing or [])]
    return {'status': 'incomplete' if failures else 'satisfied',
            'source_message_id': source['message_id'],
            'source_sha256': hashlib.sha256(source['text'].encode('utf-8')).hexdigest(),
            'interpretation_id': interpretation['id'], 'constraint_ids': list(by_key.values()),
            'char_count': char_count, 'table_data_rows': rows, 'missing': failures}


@pytest.mark.parametrize('length', [400, 401])
def test_exact_saved_reply_check_controls_complete_without_rewriting_the_original(native, length):
    source, job_id, _ = observed(native, text=REPLY_TEXT, conditions=reply_conditions())
    missing = [('max_chars', 'reply.max_chars', 400, 401)] if length == 401 else []
    check = typed_reply_check(native[1], source, missing=missing, char_count=length)
    base = '| 图 | 观察 |\n| --- | --- |\n| 一 | 替换 |\n| 二 | 未知 |'
    report = base.replace('替换', '可' * (length - len(base) + 2))
    assert len(report) == length
    # Even an old outer status incorrectly saying complete cannot override
    # the exact saved reply check's incomplete result.
    finish(native[1], source, job_id, report=report, reply_check=check)
    before = native[1].read_events(RUN), native[1].read_chat(RUN)
    out = summary(native[1])
    assert out['analysis']['report'] == report and out['analysis']['reply_check'] == check
    assert out['analysis']['observed'] == out['analysis']['total'] == 1
    if length == 400:
        assert out['title'] == '图片分析已完成' and out['phase'] == 'analysis_complete'
    else:
        assert out['title'] == '图片观察已完成，回复格式未完成'
        assert out['phase'] == 'analysis_incomplete'
        assert '不超过 400 字符，实际 401 字符' in out['detail']
    assert before == (native[1].read_events(RUN), native[1].read_chat(RUN))


@pytest.mark.parametrize('missing,expected', [
    ([('table_rows', 'reply.table_rows', 2, 3)], '要求表格有 2 行数据，实际 3 行'),
    ([('table_rows', 'reply.table_rows', 2, None)], '目前数据行数无法核对'),
    ([('table_format', 'reply.format', 'markdown_table', 'text')], '要求 Markdown 表格，当前格式未满足'),
    ([('unresolved_condition', 'reply.format', 'clarified', 'needs_clarification')], '回复条件仍需澄清'),
])
def test_reply_failures_are_named_after_complete_image_coverage(native, missing, expected):
    source, job_id, _ = observed(native, text=REPLY_TEXT, conditions=reply_conditions())
    check = typed_reply_check(native[1], source, missing=missing)
    finish(native[1], source, job_id, job_status='blocked', deliverable_status='incomplete', reply_check=check)
    out = summary(native[1])
    assert out['title'] == '图片观察已完成，回复格式未完成'
    assert '已核对 1 张观察' in out['detail'] and expected in out['detail']
    assert out['analysis']['report'] == REPORT


@pytest.mark.parametrize('job_status', ['failed', 'cancelled', 'interrupted', 'running'])
def test_reply_format_notice_never_relabels_provider_failure_or_stop_as_completed_images(native, job_status):
    source, job_id, _ = observed(native, text=REPLY_TEXT, conditions=reply_conditions())
    check = typed_reply_check(native[1], source, missing=[('max_chars', 'reply.max_chars', 400, 401)], char_count=401)
    finish(native[1], source, job_id, job_status=job_status, reply_check=check)
    out = summary(native[1])
    assert out['title'] != '图片观察已完成，回复格式未完成'
    assert out['phase'] != 'analysis_complete'


@pytest.mark.parametrize('mutation', [
    {'source_message_id': 'foreign'}, {'source_sha256': '0' * 64}, {'interpretation_id': 'foreign'},
    {'constraint_ids': ['foreign']}, {'constraint_ids': []}, {'char_count': True}, {'table_data_rows': '2'},
    {'status': {}}, {'missing': [{'category': 'PRIVATE_UNOWNED_TEXT', 'constraint_ids': [],
                                'expected': 'PRIVATE_EXPECTED', 'actual': 'PRIVATE_ACTUAL'}]},
])
def test_foreign_or_tampered_reply_checks_do_not_explain_this_source_or_complete_it(native, mutation):
    source, job_id, _ = observed(native, text=REPLY_TEXT, conditions=reply_conditions())
    check = typed_reply_check(native[1], source)
    check.update(deepcopy(mutation))
    finish(native[1], source, job_id, reply_check=check)
    out = summary(native[1])
    assert out['phase'] != 'analysis_complete'
    assert '回复检查尚未与本轮来源核对' in out['detail']
    assert 'reply_check' not in out['analysis']
    assert not any(word in out['detail'] for word in ('PRIVATE_', 'foreign'))


def test_satisfied_check_cannot_omit_one_real_reply_constraint(native):
    source, job_id, _ = observed(native, text=REPLY_TEXT, conditions=reply_conditions())
    check = typed_reply_check(native[1], source)
    check['constraint_ids'] = check['constraint_ids'][:-1]
    finish(native[1], source, job_id, reply_check=check)
    out = summary(native[1])
    assert out['phase'] != 'analysis_complete'
    assert '回复检查尚未与本轮来源核对' in out['detail']
    assert 'reply_check' not in out['analysis'] and out['analysis']['report'] == REPORT


def test_saved_reply_check_cannot_hide_missing_images_or_restore_completion_without_a_reply(native):
    source, job_id, _ = observed(native, text=REPLY_TEXT, conditions=reply_conditions())
    check = typed_reply_check(native[1], source, missing=[('max_chars', 'reply.max_chars', 400, 401)], char_count=401)
    resolve(native[1], RUN, 'fixture.png').write_bytes(b'changed pixels')
    finish(native[1], source, job_id, reply_check=check, report=None)
    out = summary(native[1])
    assert out['analysis']['observed'] == 0 and len(out['analysis']['missing']) == 1
    assert out['title'] == '图片分析未完成' and out['analysis']['report'] == ''
