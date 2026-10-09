"""Deterministic turn deliverable checks, separate from execution termination.

Two things live here and they are different questions:

* Did the model stop? That is ``run_agent_loop``'s business.
* Did the user's request get delivered? That is this module's business.

A turn that ends politely with nothing delivered is not a finished turn, and
the difference between the two must not depend on the model's own summary.

``follow_up`` is the only goal that promises nothing, and it is limited to
read-only or open-ended requests. Every other goal names a concrete object that
must exist afterwards.
"""
from __future__ import annotations

import hashlib
import re

from .models import STAGES

DELIVERABLES = ('direction', 'intake', 'writing', 'production_input', 'production_media',
                'revision', 'analysis', 'reference_prompt', 'follow_up')

# This domain distinction is shared by the interpretation schema and request
# instructions. It does not classify text or rewrite a saved interpretation.
READ_ONLY_TASK_GUIDANCE = (
    '仅核对已保存稿件/制作原描述、检索准确记录或答复状态，不要求改稿或新创作交付时，使用 status 或 discuss、'
    'follow_up、allowed_stages=[]；这是交付只读回答，不创建新的观察报告。'
    'analyze/analysis 指针对准确图片/视频/参考来源交付有观察覆盖的分析报告，可复用合格已保存观察，'
    '不是一般意义上的思考、文字比较或阅读已有报告。不得为了匹配工具而改写用户最终创作交付。'
)

#: Attempt states that mean a provider may still be working, or that the local
#: side has not yet established what happened. Neither is a finished result, and
#: neither is a licence to submit again.
IN_FLIGHT = frozenset({'queued', 'running', 'preparing', 'submitting', 'submitted',
                       'downloading', 'recovering', 'cancelling'})

#: Attempt states where the outcome is recorded but the result is not available.
UNSETTLED = frozenset({'unknown', 'submission_unknown', 'retrieve_failed',
                       'retry_authorized'})

#: Serialised as a JSON array so the value cannot be confused with a digest.
MEDIA_EXPECTATION = {
    'production_input': ('已准备的制作输入', '已保存'),
    'production_media': ('可阅读的生成结果', '已保存'),
}


def with_reply_check(delivery, check):
    """Final-only compliance: never request a paid repair or change coverage."""
    if check is None:
        return delivery
    result = {**delivery, 'reply_check': check}
    if check['status'] == 'incomplete':
        reasons = []
        for issue in check['missing']:
            category = issue['category']
            if category == 'max_chars':
                reasons.append(f"回复为 {issue['actual']} 个字符，超过要求的 {issue['expected']} 个字符上限。")
            elif category == 'table_rows':
                count = '无法核对数据行数' if issue['actual'] is None else f"实际有 {issue['actual']} 行数据"
                reasons.append(f"回复要求 {issue['expected']} 行数据，{count}；表头不计数据行。")
            elif category == 'table_format':
                reasons.append('回复尚未满足要求的单个 Markdown 表格格式。' if issue['expected'] == 'markdown_table'
                               else '回复尚未满足要求的纯文本格式。')
            else:
                reasons.append('本轮回复条件仍有未澄清的问题或冲突。')
        result['status'] = 'incomplete'
        result['missing'] = [*delivery.get('missing', []), *reasons]
        result['note'] = (delivery.get('note', '') + ' 回复格式尚未完成：' + ' '.join(reasons)).strip()
        result['repeat_key'] = _repeat_missing(result['missing'], 2000)
    return result


def _repeat_missing(value, width):
    """A stable key for "the same thing is still missing"."""
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True)[:width]


def _shot_label(ordinals):
    return '第 ' + '、'.join(str(n) for n in ordinals) + ' 镜'


_COUNT_NUMBER = r'[1-9][0-9]*|[一二两三四五六七八九十百]+'
_COUNT_LEFT = r'(?<![0-9.+\-–第一二两三四五六七八九十百零])'
_SHOT_COUNT = re.compile(rf'{_COUNT_LEFT}(?P<count>{_COUNT_NUMBER})\s*(?:个\s*)?(?:分镜头|镜头|分镜|镜(?=脚本|的脚本|[^\u4e00-\u9fff]|$))'
                         rf'|{_COUNT_LEFT}(?P<english>[1-9][0-9]*)\s+shots?\b', re.I)
# A looser mention can only invalidate a proof, never establish a count. In
# "不用4镜而用6镜", the first shorthand is still a competing quantity.
_COUNT_MENTION = re.compile(rf'{_COUNT_LEFT}(?:{_COUNT_NUMBER})\s*(?:个\s*)?(?:分镜头|镜头|分镜|镜(?!面|子))'
                            rf'|{_COUNT_LEFT}[1-9][0-9]*\s+shots?\b', re.I)
_COUNT_PREFIX_CONTEXT_UNCERTAIN = re.compile(
    r'如果|假如|比如|例如|可能|建议|推荐|他说|她说|同事说'
    r'|\b(?:maybe|if)\b', re.I)
_COUNT_GOVERNOR_BREAK = re.compile(r'并且|而且|但是|以及|且|但|\b(?:and|but)\b', re.I)
# Negation and quantity modifiers must govern the number, not an unrelated
# material/voiceover condition elsewhere in the same sentence. Only grammar
# immediately adjoining the already grounded count is a numeric proof here.
_COUNT_PREFIX_UNCERTAIN = re.compile(
    r'(?:不要|不用|不做|不能|不是|不需要|别)(?:再|只|仅)?(?:做|写|用|拍|制作|生成|安排|采用)?\s*$'
    r'|(?:不超过|最多|至少|少于|大于|多于|大概|约|从|改|替换(?:为|成)?|增加(?:到|为)?|减少(?:到|为)?)\s*$'
    r"|\b(?:not|no|don.t|do not)(?:\s+(?:make|write|use|create|shoot))?\s*$"
    r'|\b(?:at least|at most|up to|more than|less than|no more than|around|about|from|change to)\s*$', re.I)
_COUNT_SUFFIX_UNCERTAIN = re.compile(
    r'^\s*(?:的?脚本)?\s*(?:以内|以上|以下|左右|改|替换|增加|减少)'
    r'|^\s*(?:的?脚本)?\s*(?:不要|不用|不做|不能|不需要|别)(?:再)?(?:做|写|用|拍|制作|生成|安排|采用)?\s*$'
    r'|^\s*(?:at least|at most|or more|or less|instead)\b'
    r"|^\s*(?:not|no|don.t|do not)(?:\s+(?:make|write|use|create|shoot))?\s*$"
    r'|^\s*(?:的?脚本)?\s*(?:吗|么|行吗|可以吗)?[？?]'
    r'|^\s*(?:的?脚本)?\s*(?:可能|建议|推荐)(?:更好|不够|太多)?\s*$', re.I)
_COUNT_CLAUSE_BREAK = re.compile(r'[，,。\n；;！!]')
_COUNT_RANGE = re.compile(rf'(?:{_COUNT_NUMBER})\s*(?:个\s*)?(?:镜头|分镜|镜)?\s*(?:到|至|[-–~～]|to)\s*(?:{_COUNT_NUMBER})', re.I)
_COUNT_TARGET_PREFIX = re.compile(r'(?:改成|改为)\s*$')
_COUNT_NEGATED_PREFIX = re.compile(
    r'(?:不要|不用|不做|不能|不是|不需要|别)(?:再|只|仅)?(?:做|写|用|拍|制作|生成|安排|采用)?\s*$')


def _natural_count(value):
    """Only exact numerals; interpreting intent remains the existing input path."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.fullmatch(r'[1-9][0-9]*', value):
        return int(value) if len(value) <= 6 else None
    digits = dict(zip('一二两三四五六七八九', (1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    if value in digits:
        return digits[value]
    match = re.fullmatch(r'([一二两三四五六七八九])?十([一二三四五六七八九])?', value)
    if match:
        return digits.get(match[1], 1) * 10 + digits.get(match[2], 0)
    match = re.fullmatch(r'([一二两三四五六七八九])百', value)
    return digits[match[1]] * 100 if match else None


def _quoted_shot_count(text, quote):
    """A bounded count proof, never a keyword-based user intent decision.

    Only a previously grounded require/instruction condition reaches here.
    Competing counts, count negation and ranges need clarification rather
    than guessing. An explicit replacement target can coexist with a rejected
    old count in a separate clause. Read adjacent grammar
    as well so a model cannot quote only ``4镜`` from ``不要4镜``.
    """
    if not isinstance(text, str) or not isinstance(quote, str) or not quote or quote not in text:
        return None
    quoted = list(_SHOT_COUNT.finditer(quote))
    if len(quoted) > 1:
        # A full real quote may include "不要4镜，改成6镜". Select its single
        # explicit new target only when every other number is directly negated
        # in a separate clause; never discard competing affirmative quantities.
        quote_breaks = list(_COUNT_CLAUSE_BREAK.finditer(quote))
        targets = []
        for item in quoted:
            left = max((mark.end() for mark in quote_breaks if mark.end() <= item.start()), default=0)
            if _COUNT_TARGET_PREFIX.search(quote[left:item.start()]):
                targets.append(item)
            elif not _COUNT_NEGATED_PREFIX.search(quote[left:item.start()]):
                return None
        if len(targets) != 1 or _COUNT_RANGE.search(quote):
            return None
        quoted = targets
    if len(quoted) != 1:
        return None
    counts = []
    breaks = list(_COUNT_CLAUSE_BREAK.finditer(text))
    for occurrence in re.finditer(re.escape(quote), text):
        # The quote can also contain unrelated clauses. Locate the actual
        # numeral within it before proving its governing grammar.
        count_start = occurrence.start() + quoted[0].start()
        count_end = occurrence.start() + quoted[0].end()
        left = max((mark.end() for mark in breaks if mark.end() <= count_start), default=0)
        right = min((mark.start() for mark in breaks if mark.start() >= count_end), default=len(text))
        clause = text[left:right]
        matches = list(_SHOT_COUNT.finditer(clause))
        if (_COUNT_RANGE.search(clause) or len(matches) != 1
                or len(list(_COUNT_MENTION.finditer(clause))) != 1):
            return None
        match = matches[0]
        prefix = clause[:match.start()]
        governors = list(_COUNT_GOVERNOR_BREAK.finditer(prefix))
        governor = prefix[governors[-1].end():] if governors else prefix
        if (_COUNT_PREFIX_CONTEXT_UNCERTAIN.search(governor)
                or _COUNT_PREFIX_UNCERTAIN.search(prefix)
                or _COUNT_SUFFIX_UNCERTAIN.search(clause[match.end():])):
            return None
        counts.append(_natural_count(match['count'] or match['english']))
    quoted_count = _natural_count(quoted[0]['count'] or quoted[0]['english'])
    return quoted_count if quoted_count and all(number == quoted_count for number in counts) else None


def _writing_count_requirement(store, run_id, events, source_message_ids):
    from .conversation_input import constraints_for_inputs

    constraints = constraints_for_inputs(store, run_id, source_message_ids, stage='writing')
    receipts = {event['message_id']: event for event in events if event['kind'] == 'user_message_received'}
    executed = {event['message_id'] for event in events if event['kind'] == 'user_message_execution_started'}
    numbers = set()
    for row in constraints:
        if (row.get('key') != 'script.shot_count' or row.get('intent') != 'instruction'
                or row.get('polarity') != 'require'
                or row.get('scope', {}).get('paths') not in ([], [['segments']])):
            continue
        source_id = row.get('source_message_id')
        receipt = receipts.get(source_id) or {}
        text = receipt.get('text', '')
        count = _natural_count(row.get('value'))
        if (row.get('status') != 'active' or source_id not in executed
                or row.get('origin') != 'grounded_model_interpretation'
                or row.get('source_sha256') != hashlib.sha256(text.encode('utf-8')).hexdigest()
                or count is None or _quoted_shot_count(text, row.get('quote')) != count):
            return None, ['本轮镜头数量条件无法与真实原话准确核对，需先澄清']
        numbers.add(count)
    if len(numbers) > 1:
        return None, ['本轮存在不同的明确镜头数量条件，需先澄清']
    return next(iter(numbers), None), []


def writing_completeness(store, run_id, structured, *, source_message_ids=(), format=None):
    """Business delivery checks for a full draft, separate from wire parsing.

    This does not derive production targets or change historical artifacts.
    Photo text planning and exact field patches retain their existing contract.
    The trusted caller supplies consumed user identities, never tool arguments.
    """
    from .project_brief import content_format

    events = store.read_events(run_id)
    format = format or content_format(events) or 'video'
    if format not in {'video', 'photo'}:
        raise ValueError('Unknown writing delivery format')
    if format == 'photo':
        return {'missing': [], 'count': None, 'expected_count': None, 'format': format}
    rows = structured.get('segments', []) if isinstance(structured, dict) else []
    rows = rows if isinstance(rows, list) else []
    missing = []
    if not rows:
        missing.append('完整视频脚本的非空镜头清单（仅正文或 caption 不算镜头）')
    elif any(not isinstance(row, dict) or not isinstance(row.get('picture'), str)
             or not row['picture'].strip() for row in rows):
        missing.append('每个视频镜头的实际画面描述')
    expected, unresolved = _writing_count_requirement(store, run_id, events, source_message_ids)
    missing.extend(unresolved)
    if expected is not None and len(rows) != expected:
        missing.append(f'本轮明确要求 {expected} 个镜头，当前稿件为 {len(rows)} 个')
    return {'missing': missing, 'count': len(rows), 'expected_count': expected, 'format': format}


def _shot_coverage(store, pipeline, run_id, kind, require_media, *, state=None):
    """Per-shot readiness for the shots this project actually asks for.

    Returns ``(missing, note, target_count)``. The target list comes from the
    adopted script, so a six-segment script asks for six inputs and a
    three-segment one asks for three — nothing here counts to six.

    Read through the same projection the page uses. If this file derived its own
    answer, a shot could read as blocked on screen and ready in chat.
    """
    from .production import _read_state_without_service

    if state is None:
        state = _read_state_without_service(store, pipeline, run_id)
    targets = state['targets'][kind]
    if not targets['complete_list']:
        # A legacy prose script has no readable shot list. Say so instead of
        # inventing a number.
        return (['可核对的目标镜头清单'], targets['notice'], None)

    shots = state['shots'][kind]['shots']
    total = len(shots)
    if not require_media:
        unready = [row['ordinal'] for row in shots if row['stage'] == 'unprepared']
        if unready:
            return ([f'{_shot_label(unready)}还没有绑定当前方案的制作输入'],
                    f'{total} 个目标镜头中，已准备 {total - len(unready)} 个。', total)
        return ([], f'{total} 个目标镜头的制作输入已齐备。', total)

    failed = [row['ordinal'] for row in shots if row['stage'] == 'failed']
    if failed:
        return ([f'{_shot_label(failed)}生成失败，需要处理原因后再决定'],
                '每面镜头的失败原因与下一步已记录在对应镜头。', total)
    unsettled = [row['ordinal'] for row in shots if row['stage'] == 'unverified']
    if unsettled:
        return ([f'{_shot_label(unsettled)}结果待核实；先查询原任务，不要重复提交'],
                '结果不确定时只核实原任务，不会自动重发。', total)
    missing = [row['ordinal'] for row in shots if row['stage'] != 'completed']
    if missing:
        return ([f'{_shot_label(missing)}还没有可阅读的生成结果'],
                f'{total} 个目标镜头中，已完成 {total - len(missing)} 个。', total)
    return ([], f'{total} 个目标镜头的生成结果已齐备。', total)


def evaluate(store, pipeline, run_id, goal, *, revision_scope=None, analysis_completed=False,
             production_kind=None, media_kind=None, turn_id=None, reuse_artifact=None,
             analysis_source_message_id=None):
    if goal is None:
        return {'goal': None, 'status': 'unspecified', 'missing': [], 'note': '',
                'target_shots': None, 'repeat_key': '["unspecified"]'}
    if goal == 'follow_up':
        return {'goal': goal, 'status': 'informational', 'missing': [], 'note': '',
                'target_shots': None, 'repeat_key': '["informational"]'}
    if goal not in DELIVERABLES:
        raise ValueError('Unknown deliverable goal')

    missing: list[str] = []
    note = ''
    target_count = None
    delivery_status = None
    if goal in ('intake', 'direction', 'writing'):
        state = pipeline.stage_state(run_id, goal)
        events = store.read_events(run_id) if goal == 'writing' or turn_id else []
        scope = next((e for e in reversed(events)
                      if e.get('kind') == 'agent_turn_scope_started'
                      and e.get('turn_id') == turn_id), None) if turn_id else None
        if not state.latest or state.stale or not state.integrity_ok or state.open_attempt:
            missing.append(f'{goal} 当前准确版本')
        elif turn_id:
            baseline = (scope or {}).get('artifact_baseline', {}).get(goal)
            if reuse_artifact:
                if (reuse_artifact.get('stage') != goal
                        or reuse_artifact.get('version') != state.latest.version
                        or reuse_artifact.get('sha256') != state.latest.sha256):
                    missing.append(f'{goal} 用户指定沿用的准确版本')
            elif not scope or 'artifact_baseline' not in scope:
                missing.append(f'{goal} 本轮可核对的交付基线')
            elif baseline and state.latest.version <= baseline['version']:
                missing.append(f'{goal} 本轮准确新版本（旧稿仅供参考）')
            elif (revision_scope and revision_scope.get('stage') == goal
                  and revision_scope.get('mode') == 'local'):
                missing.append(f'{goal} 完整新交付（当前仅完成局部补丁）')
        if goal == 'writing' and not missing:
            submitted = next((event for event in reversed(events)
                              if event['kind'] == 'attempt_submitted' and event.get('stage') == 'writing'
                              and event.get('sha256') == state.latest.sha256), {})
            contract = submitted.get('writing_delivery') or {}
            identities = list(dict.fromkeys([*contract.get('source_message_ids', []),
                                           *([(scope or {}).get('user_input_id')] if (scope or {}).get('user_input_id') else [])]))
            check = writing_completeness(store, run_id, state.latest.structured,
                source_message_ids=identities, format=contract.get('format'))
            missing.extend(check['missing'])
            if check['missing']:
                note = '稿件版本已保留，但未满足完整视频交付；不能以保存成功代替内容齐备。'
    elif goal in MEDIA_EXPECTATION:
        writing = pipeline.stage_state(run_id, 'writing')
        if (not writing.latest or writing.stale or not writing.integrity_ok
                or not pipeline.is_complete(run_id, 'writing')):
            missing.append('已确认的当前完整方案')
            note = '脚本本身还没有被采用或是过期版本，制作输入没有可靠依据。'
            if goal == 'production_media':
                # The script being unadopted is not a media-delivery failure; the
                # first thing to fix is upstream, and saying otherwise would push
                # the controller at a generation it cannot justify.
                missing = ['当前脚本待用户采用']
        else:
            from .production import _read_state_without_service

            kind = media_kind or production_kind or 'image'
            production_state = _read_state_without_service(store, pipeline, run_id)
            missing, note, target_count = _shot_coverage(
                store, pipeline, run_id, kind, require_media=(goal == 'production_media'),
                state=production_state)
            if goal == 'production_media' and missing:
                # A read-only candidate is neither a displayed confirmation nor
                # a delivered result. Keep the missing-media evidence and expose
                # the real stopping condition so the controller never retries an
                # active/unknown job just to make the contract say complete.
                progress = production_state['shots'][kind]
                delivery_status = progress['state']
                if delivery_status == 'input_ready':
                    note = f'{target_count} 个镜头的输入已齐备，尚未提交生成；需要用户核对并授权。'
                elif delivery_status == 'awaiting_authorisation':
                    note = '准确生成确认已准备，等待用户授权；媒体尚未交付。'
                elif delivery_status == 'in_progress':
                    note = '原任务仍在执行，等待或查询原任务，不要重新提交。'
    elif goal == 'revision':
        if not revision_scope:
            missing.append('绑定本轮原话与准确基线的修订计划')
        else:
            stage = revision_scope['stage']
            if stage not in STAGES:
                missing.append('有效修订阶段')
            else:
                applied = [e for e in store.read_events(run_id)
                           if e.get('kind') == 'agent_revision_applied'
                           and e.get('plan_id') == revision_scope['plan_id']]
                state = pipeline.stage_state(run_id, stage)
                if (not applied or not state.latest or state.stale or not state.integrity_ok
                        or state.latest.sha256 != applied[-1]['sha256']):
                    missing.append('按本轮计划提交的准确新版本')
    elif goal == 'analysis':
        source_id = analysis_source_message_id
        if not source_id and turn_id:
            source_id = next((event.get('user_input_id') for event in reversed(store.read_events(run_id))
                              if event['kind'] == 'agent_turn_scope_started' and event.get('turn_id') == turn_id), None)
        coverage = None
        interpretation = None
        if source_id:
            from .agent_images import analysis_coverage
            from .conversation_input import interpretation_for
            interpretation = interpretation_for(store, run_id, source_id)
            coverage = analysis_coverage(store, run_id, source_id, turn_id)
        if coverage is not None:
            missing = ['尚未核对的图片观察：' + name for name in coverage['missing']]
            note = f"本轮固定 {coverage['total']} 张图片，已核对 {coverage['observed']} 张观察。"
        else:
            # A photo source declaration is not a reference-video report and
            # does not mutate the project's creative output format.
            receipt = None
            if source_id:
                from .conversation_input import message
                receipt = message(store, run_id, source_id)
            from .materials import kind_of
            source_kinds = {kind_of(item['name']) for item in (receipt or {}).get('attachments') or []}
            video_requested = source_id and any(event['kind'] == 'reference_observation_requested'
                and event.get('source_user_input_id') == source_id and event.get('action') == 'analyze'
                for event in store.read_events(run_id))
            video_source = (interpretation and interpretation.get('video_use')
                            or (receipt or {}).get('reference_target') or source_kinds == {'video'}
                            or video_requested)
            image_source = not video_source and (source_kinds == {'image'}
                or interpretation and interpretation['task']['format'] == 'photo')
            if image_source:
                missing.append('本轮图片来源绑定与完整观察')
            elif not analysis_completed:
                missing.append('已保存的参考分析报告')
    elif goal == 'reference_prompt':
        saved = any(e.get('kind') == 'reference_prompt_saved' and e.get('turn_id') == turn_id
                    for e in store.read_events(run_id)) if turn_id else False
        if not saved:
            missing.append('本轮已保存的商品适配提示词')
    return {'goal': goal, 'status': delivery_status or ('incomplete' if missing else 'complete'),
            'missing': missing, 'note': note, 'target_shots': target_count,
            'repeat_key': _repeat_missing(missing, 600)}


def media_progress(store, run_id, kind):
    """How the target shots stand, for a turn that asked for media.

    Separated from ``evaluate`` because "still generating" must not be reported
    as "you have not delivered": the first means wait, the second means work.
    Telling the controller a turn is incomplete while a provider task is in
    flight is how the same generation gets submitted twice.
    """
    from .production import _read_state_without_service
    from .pipeline import Pipeline

    state = _read_state_without_service(store, Pipeline(store), run_id)
    targets = state['targets'][kind]
    if not targets['complete_list']:
        return {'status': 'unknown_targets', 'pending': False, 'notes': targets['notice']}
    result = state['shots'][kind]
    return {'status': result['state'], 'pending': bool(result['pending']),
            'failed': bool(result['failed']), 'delivered': bool(result['delivered']),
            'total': result.get('total', 0),
            'in_flight': [row['ordinal'] for row in result['shots']
                          if row['stage'] in {'awaiting_authorisation', 'submitted',
                                              'generating', 'retrieving'}]}
