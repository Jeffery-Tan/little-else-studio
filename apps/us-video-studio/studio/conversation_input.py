"""Trusted user ingress and the view that accompanied it, in the run journal.

A display receipt identifies a response offered to the browser. It does not
prove attention or human reading, and never grants execution permission.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from .models import PipelineError, STAGES
from .task_contract import READ_ONLY_TASK_GUIDANCE
from .reply_conditions import (REPLY_GUIDANCE, is_reply_scope, typed_condition_issue,
                               validate_typed_condition)


CONSTRAINT_SCOPE_GUIDANCE = (
    'scope.stages=[] 仅表示本条主制片回复域，不表示全部创作阶段；仅此域使用 lifetime=turn、paths=[]。'
    f'项目或产物内容条件须列明实际适用的非空 stages，从 {", ".join(STAGES)} 选择。'
    '项目条件可覆盖本轮 task.allowed_stages 之外的未来创作阶段；条件范围不授予执行权限。'
    '不要为了通过校验删除真实条件、改为回复条件或猜测阶段。'
)

EDIT_SCOPE_KEY = 'content.edit_scope'
EDIT_SCOPE_GUIDANCE = (
    '修改已有文字产物时 task.mode=revise，并在首次解释中用 content.edit_scope 冻结编辑范围；'
    '它只适用本条任务，operation=add、polarity=require、intent=instruction、lifetime=turn。'
    'value=local 时 scope.stages 只列一个本轮允许阶段，paths 列已有文字字段，quote 与 scope.quote '
    '准确引用原话中的修改及保留范围；value=rework 时同样绑定一个阶段，paths=[]。'
    '读取准确版本后再绑定；一般语气或画面条件的 paths 不代表编辑权限。'
    '已有用户精确选中的字段继续使用原目标，不要求重复批准。缺范围的提案未落盘，可沿同一原话纠正；'
    '真实范围不明确时用 clarify，不自行扩大成整稿重做。制作输入反馈继续原镜头通道。'
)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def display_context(store, pipeline, run_id, timeline):
    from .decision import candidates
    chat = store.read_chat(run_id)
    decisions = candidates(store, pipeline, run_id, pending=[timeline['pending']]
                           if timeline.get('pending') else [])
    last = chat[-1] if chat and chat[-1].get('role') == 'assistant' else None
    displayed = {'decisions': decisions, 'pending': timeline.get('pending'),
                 'questions': timeline.get('questions') or [],
                 'interaction': timeline.get('interaction'),
                 'assistant': last, 'chat_entries': len(chat)}
    # Reuse only the identical offered view; an intervening message changes it.
    fingerprint = _digest(displayed)
    with store.run_lock(run_id):
        prior = next((e for e in reversed(store.read_events(run_id))
                      if e['kind'] == 'conversation_displayed'), None)
        if prior and prior.get('fingerprint') == fingerprint:
            return prior['display_context_id']
        context_id = uuid.uuid4().hex
        store.append_event(run_id, 'conversation_displayed',
                           display_context_id=context_id, fingerprint=fingerprint,
                           displayed=displayed)
        return context_id


def receive(store, run_id, text, request_id, display_context_id=None, *,
            feedback_target=None, request_kind=None, question_id=None, attachments=None,
            reference_consent=False, reference_target=None, reference_recovery=None):
    """Called by trusted ingress, before work/model preparation, exactly once."""
    with store.run_lock(run_id):
        events = store.read_events(run_id)
        prior = next((e for e in events if e['kind'] == 'user_message_received'
                      and e['request_id'] == request_id), None)
        if prior:
            if (prior['text'] != text or prior.get('display_context_id') != display_context_id
                    or any(prior.get(key) != value for key, value in {
                        'feedback_target': feedback_target, 'request_kind': request_kind,
                        'question_id': question_id, 'attachments': attachments,
                        'reference_target': reference_target,
                        'reference_recovery': reference_recovery}.items())
                    or prior.get('reference_consent', False) != reference_consent):
                raise PipelineError('request_conflict', '这次提交标识已用于另一条消息或展示内容。')
            return prior
        validate_media_input(store, run_id, attachments, reference_target)
        view = None
        if display_context_id:
            view = next((e for e in events if e['kind'] == 'conversation_displayed'
                         and e['display_context_id'] == display_context_id), None)
            if view is None:
                raise PipelineError('display_context', '这条消息的展示内容无法核对，请刷新后发送。')
        message_id = uuid.uuid4().hex
        displayed = view['displayed'] if view else None
        # Freeze whether that question was still adjacent *at receipt*, not at
        # model execution, when append_chat would already have changed history.
        chat = store.read_chat(run_id)
        adjacent = bool(displayed and displayed.get('assistant')
                        and len(chat) == displayed.get('chat_entries')
                        and chat[-1] == displayed['assistant'])
        from .interactions import pending_question
        question = pending_question(store, run_id)
        receipt = store.append_event(run_id, 'user_message_received', role='user',
            message_id=message_id, request_id=request_id, text=text,
            display_context_id=display_context_id, displayed=displayed,
            adjacent_question=adjacent, feedback_target=feedback_target,
            request_kind=request_kind, question_id=question_id, attachments=attachments,
            pending_question_id=question['id'] if question else None,
            reference_consent=reference_consent, reference_target=reference_target,
            reference_recovery=reference_recovery)
        return receipt


def validate_media_input(store, run_id, attachments=None, reference_target=None):
    """Validate user-selected media metadata without starting observation."""
    from .materials import resolve, kind_of
    if attachments is not None:
        names = set()
        for attachment in attachments:
            name = attachment['name']
            if name in names or resolve(store, run_id, name) is None:
                raise PipelineError('attachment_changed', '附件引用无效或重复，请重新核对当前项目的素材。')
            names.add(name)
            if attachment.get('purpose') == 'reference' and kind_of(name) != 'video':
                raise PipelineError('attachment_purpose', '参考视频入口需要实际视频文件。')
    if reference_target is not None:
        from .creation_mode import reference_candidate, digest
        if reference_target['run_id'] != run_id:
            raise PipelineError('reference_target', '这条参考不属于当前项目。')
        try:
            candidate = reference_candidate(store, run_id, reference_target['reference_id'])
        except ValueError as exc:
            raise PipelineError('reference_target', str(exc)) from None
        if (candidate['analysis_id'] != reference_target['analysis_id']
                or digest(candidate) != reference_target['candidate_sha256']):
            raise PipelineError('reference_target', '参考分析已更新，请重新打开准确版本后再适配。')


def verified_video_aliases(store, run_id, source):
    """Aliases belong to one downloaded source with unchanged local bytes."""
    from .materials import resolve, kind_of
    name = source.get('name')
    if not name or kind_of(name) != 'video':
        return set()
    path = resolve(store, run_id, name)
    if path is None or hashlib.sha256(path.read_bytes()).hexdigest() != source.get('sha256'):
        return set()
    return {value for key in ('name', 'source_url', 'canonical_url')
            if isinstance(value := source.get(key), str) and value}


def _same_video_source(store, run_id, left, right):
    if left == right:
        return True
    sources = {}
    events = store.read_events(run_id)
    for event in events:
        identity = event.get('reference_id')
        if event['kind'] == 'reference_video_started':
            sources[identity] = dict(event.get('request') or {})
        elif event['kind'] == 'reference_video_source' and identity in sources:
            sources[identity].update(event['source'])
    downloaded = [*sources.values(), *(event for event in events
                  if event['kind'] == 'product_video_source')]
    for item in downloaded:
        aliases = {item.get(key) for key in ('name', 'source_url', 'canonical_url')}
        if left in aliases and right in aliases and verified_video_aliases(store, run_id, item):
            return True
    return False


def validate_observation_source(store, run_id, receipt, source):
    """Free source proof, shared by interpretation binding and observation."""
    from .materials import resolve, kind_of
    from .tiktok_reference import checked_url
    if not isinstance(source, str) or not source:
        raise PipelineError('observation_source', '请使用真实用户提供的TikTok链接或当前项目视频。')
    if not source.startswith('https://'):
        if resolve(store, run_id, source) is None or kind_of(source) != 'video':
            raise PipelineError('observation_source', '请使用当前项目实际上传的视频文件名或用户提供的TK链接。')
        return source
    checked = checked_url(source)
    target = (receipt or {}).get('reference_target')
    if target:
        validate_media_input(store, run_id, reference_target=target)
        target_urls = [item.get('source_url') for event in store.read_events(run_id)
                       if event.get('reference_id') == target['reference_id']
                       for item in [event.get('source') or event.get('request') or {}]]
        if checked in target_urls:
            return checked
        raise PipelineError('reference_target', '本次适配只能读取已绑定的准确参考，不能换来源。')
    supplied = [row.get('text', '') for _, row in visible_chat(store, run_id) if row.get('role') == 'user']
    supplied.extend([store.read_run(run_id).brief, (receipt or {}).get('text', '')])
    urls = set()
    for text in supplied:
        for url in re.findall(r'https://[^\s<>"\u3000]+', text):
            try:
                urls.add(checked_url(url.rstrip('。。，，、；;!！?？)）]】\'')))
            except PipelineError:
                continue
    if checked not in urls and not any(_same_video_source(store, run_id, url, checked) for url in urls):
        raise PipelineError('observation_source', '该视频链接不是用户提供的。')
    return checked


def observation_admission(store, run_id, receipt, interpretation, source, purpose, quote):
    """Ground a video purpose in this input, never in a connection switch.

    Semantic intent belongs to the model's bound interpretation. The server
    checks its real source and lifetime, explicit UI purposes, and the absence
    of any substantive request in a bare-link input; it does not classify intent
    using words such as "analyze" or "reference".
    """
    if not receipt or not interpretation or interpretation['source_message_id'] != receipt['message_id']:
        raise PipelineError('observation_scope', '先按本条真实原话解释任务与视频用途，再进行观察。')
    if interpretation['task']['mode'] in {'discuss', 'status', 'stop', 'clarify'}:
        raise PipelineError('observation_scope', '本轮只讨论、查询、停止或澄清，不能启动视频观察。')
    if interpretation.get('image_use'):
        raise PipelineError('observation_scope', '本轮已固定为只读图片分析，不能改为视频观察。')
    _quoted(receipt['text'], quote)
    validate_media_input(store, run_id, receipt.get('attachments'), receipt.get('reference_target'))
    supplied = [item for item in receipt.get('attachments') or []
                if _same_video_source(store, run_id, item['name'], source)]
    if any(item.get('purpose') and item['purpose'] != purpose for item in supplied):
        raise PipelineError('observation_purpose', '本条输入已标明另一种视频用途，不能由模型改成参考或商品证据。')
    target = receipt.get('reference_target')
    if target:
        from .creation_mode import reference_candidate
        candidate = reference_candidate(store, run_id, target['reference_id'])
        exact_sources = {candidate['name']}
        for event in store.read_events(run_id):
            if event.get('reference_id') != target['reference_id']:
                continue
            recorded = event.get('source') or event.get('request') or {}
            if recorded.get('source_url'):
                exact_sources.add(recorded['source_url'])
        if purpose != 'reference' or source not in exact_sources:
            raise PipelineError('reference_target', '本次适配只能读取已绑定的准确参考，不能换来源或用途。')
        if any(item['name'] == candidate['name'] and item.get('purpose') not in {None, 'reference'}
               for item in receipt.get('attachments') or []):
            raise PipelineError('observation_purpose', '本条输入同时把准确参考标为商品视频，请先澄清用途。')
        return
    if any(item.get('purpose') for item in supplied):
        return
    use = interpretation.get('video_use')
    if not use or use['purpose'] != purpose or not _same_video_source(store, run_id, use['source'], source):
        raise PipelineError('observation_purpose', '视频用途尚未绑定真实输入；用途不明时先问一次用于商品信息还是参考表达。')
    # A URL alone cannot contain a request or a purpose, however the model labels it.
    substantive = re.sub(r'https://[^\s<>"\u3000]+', '', use['quote']).strip()
    if not substantive or not any(char.isalnum() for char in substantive):
        raise PipelineError('observation_purpose', '只有链接，尚未明确视频用途；请先澄清用途，不开始下载或分析。')


def accept(store, run_id, receipt):
    """Only accepted jobs enter model-visible conversation history."""
    with store.run_lock(run_id):
        if any(e['kind'] == 'user_message_accepted' and e.get('message_id') == receipt['message_id']
               for e in store.read_events(run_id)):
            return
        chat_index = len(store.read_chat(run_id))
        store.append_chat(run_id, 'user', receipt['text'])
        store.append_event(run_id, 'user_message_accepted', message_id=receipt['message_id'],
                           request_id=receipt['request_id'], chat_index=chat_index)


def start_execution(store, run_id, receipt):
    with store.run_lock(run_id):
        if not any(e['kind'] == 'user_message_execution_started' and e.get('message_id') == receipt['message_id']
                   for e in store.read_events(run_id)):
            store.append_event(run_id, 'user_message_execution_started', message_id=receipt['message_id'])


def visible_chat(store, run_id):
    """Indexed model history excludes receipts accepted for a future turn.

    A receipt assigned to a real job remains background even if cancellation
    arrived before its agent started. This visibility never grants a current
    decision: that still binds only the current receipt. Future steer receipts
    have no job yet and remain hidden until their own turn is assigned.

    UI history stays complete. Legacy accepted rows are matched in acceptance
    order to exact chat text; all new receipts bind the original chat index.
    """
    chat, events = store.read_chat(run_id), store.read_events(run_id)
    receipts = {e['message_id']: e for e in events if e['kind'] == 'user_message_received'}
    executed = {e.get('message_id') for e in events if e['kind'] in {
        'user_message_execution_started', 'user_message_consumed'}}
    executed.update(e.get('user_input_id') for e in events if e['kind'] == 'agent_turn_scope_started')
    executed.update(e.get('user_input_id') for e in events if e['kind'] == 'job_created')
    assigned, pending = set(), set()
    for event in events:
        if event['kind'] != 'user_message_accepted' or event['message_id'] not in receipts:
            continue
        receipt = receipts[event['message_id']]
        index = event.get('chat_index')
        if not (type(index) is int and 0 <= index < len(chat)
                and chat[index].get('role') == 'user' and chat[index].get('text') == receipt['text']):
            index = next((i for i, row in enumerate(chat) if i not in assigned
                          and row.get('role') == 'user' and row.get('text') == receipt['text']), None)
        if index is not None:
            assigned.add(index)
            if event['message_id'] not in executed:
                pending.add(index)
    return [(index, row) for index, row in enumerate(chat) if index not in pending]


def message(store, run_id, message_id):
    receipt = next((e for e in store.read_events(run_id)
                    if e['kind'] == 'user_message_received' and e.get('message_id') == message_id), None)
    if not receipt or receipt.get('role') != 'user':
        raise PipelineError('user_message', '这个动作没有绑定本次收到的用户消息。')
    return receipt


def validate_proposal(receipt, proposed, action):
    """Provenance and conservative ambiguity checks, not general NL proof.

    The model still interprets natural language. Its interpretation cannot
    invent a user option, rebase onto an unseen version, quote another speaker,
    or manufacture the question needed to make a bare answer meaningful.
    """
    text = receipt['text'].strip()
    if proposed.get('message_id') and proposed['message_id'] != receipt['message_id']:
        return 'message_mismatch'
    if proposed.get('display_context_id') and proposed['display_context_id'] != receipt.get('display_context_id'):
        return 'display_context_mismatch'
    evidence = proposed.get('evidence_quote')
    if evidence is not None and (not isinstance(evidence, str) or not evidence.strip()
                                 or evidence not in text):
        return 'evidence_not_user_words'
    source_clause = _decision_clause(text)
    if evidence is not None and (not text.startswith(evidence.strip())
                                 or not evidence.strip().startswith(source_clause)):
        # A substring after "do not", "if", or another speaker is not the
        # complete decision clause. The model cannot supply its own boundary.
        return 'evidence_not_decision_clause'
    decision_part = _without_selected_title(source_clause, action)
    # These are deliberately conservative refusal guards. The only quotation
    # removed is the exact offered option's title immediately after its id.
    # A quoted whole decision, attribution, or condition still refuses adoption.
    # Later copy corrections may themselves quote text or mention other options;
    # they do not change who spoke this initial, complete decision clause.
    # A later conditional can qualify whether adoption or continuation takes
    # effect. Keep the existing whole-message guard until that condition is
    # resolved, rather than treating an initial affirmative as unconditional.
    if re.search(r'如果|假如|假设|要是|if\b|suppos|what if', text, re.I):
        return 'hypothetical_message'
    if re.search(r'[「」“”\"\']|他说|她说|别人|朋友说|引用|quoted?|he said|she said', decision_part, re.I):
        return 'quoted_message'
    if action['action'] != 'return_stage' and re.search(
            r'不要|别(?:用|选|采用|批准)|不(?:采用|选|用|批准|同意)|没(?:有)?(?:同意|决定)|'
            r'不行|不合适|不满意|不通过|不采纳|还没|尚未|犹豫|再想|考虑|'
            r'not\b|don.t\b|do not\b|reject\b', decision_part, re.I):
        return 'negative_message'
    if action['action'] in {'select_direction', 'approve_stage'} and re.search(
            r'修改|调整|改(?:一下|一改|成|为)|先.*改|比較|比较|看看|聊聊|edit\b|change\b|compare\b', decision_part, re.I):
        return 'revision_not_adoption'
    if '?' in decision_part or '？' in decision_part or re.search(
            r'多少|为什么|怎么看|怎样|如何|是否|能否|会不会|what\b|how\b|why\b', decision_part, re.I):
        return 'question_not_decision'
    named = set(re.findall(r'(?<![A-Za-z])[ABC](?![A-Za-z])', decision_part.upper()))
    if len(named) > 1 or re.search(r'(方向|脚本|方案).*(以及|还有|同时|和|及).*(方向|脚本|方案)', decision_part):
        return 'several_targets_named'
    if action['action'] in {'select_direction', 'approve_stage'}:
        conflict = _later_decision_conflict(text[len(source_clause):], action)
        if conflict:
            return conflict
    option = action.get('target', {}).get('option_id')
    if option and named and named != {option.upper()}:
        return 'option_not_user_choice'
    short = not named and len(re.sub(r'[\s。.!！]', '', text)) <= 5
    if short:
        # The bounded yes/no shorthand is supported only here, after an exact
        # stored question. Longer natural-language decisions remain model
        # interpretations; this is not an outcome keyword classifier.
        if not re.fullmatch(r'(?:行|好|好的|可以|是|同意|确认|yes|ok|okay|sure)[\s。.!！]*', text, re.I):
            return 'short_reply_not_confirmation'
        view = receipt.get('displayed') or {}
        prior = view.get('assistant') or {}
        question = str(prior.get('text') or '')
        # Explicit adjacent yes/no questions only; an A/B/C stage card is not
        # such a question and cannot supply the model's preferred B.
        expected = None
        picked = re.fullmatch(r'(?:是否采用(?:方向)?\s*|按\s*)([ABC])\s*(?:继续吗|继续吗？|[？?])', question.strip(), re.I)
        if picked:
            expected = ('select_direction', picked[1].upper())
        elif re.fullmatch(r'(?:是否采用当前(?:脚本|完整方案)|采用当前(?:脚本|完整方案)并继续吗)[？?]', question.strip()):
            expected = ('approve_stage', None)
        if not receipt.get('adjacent_question') or expected != (action['action'], option):
            return 'no_explicit_adjacent_question'
        if len(view.get('decisions') or []) != 1 or view.get('interaction') or view.get('questions'):
            return 'several_questions_open'
    elif option and not named:
        return 'option_not_in_user_message'
    elif action['action'] in {'select_direction', 'approve_stage'}:
        # A matching option and an available button establish the object, not
        # the user's intent. Only bounded, affirmative constructions are
        # supported here; unfamiliar language needs one clarification.
        if not _affirmative_decision(decision_part, option):
            return 'adoption_not_explicit'
    return None


def _decision_clause(text):
    """The initial complete clause, with punctuation inside quotes preserved.

    This is a refusal boundary for the model's proposal, not an intent parser.
    It cannot start after a conditional, negation, or attribution in the input.
    """
    quote_ends = {'「': '」', '“': '”', '"': '"', "'": "'"}
    closing = []
    for index, char in enumerate(text):
        if closing and char == closing[-1]:
            closing.pop()
        elif char in quote_ends:
            closing.append(quote_ends[char])
        elif not closing and (char in ',，;；。.!！\n' or text.startswith('但', index)
                              or (text[index:index + 3].lower() == 'but'
                                  and re.match(r'but\b', text[index:], re.I))):
            return text[:index].strip()
    return text.strip()


def _without_selected_title(text, action):
    """Remove only an exact title attached to its trusted selected option id."""
    if action['action'] != 'select_direction':
        return text
    option = action.get('target', {}).get('option_id')
    offered = next((row for row in action.get('options') or []
                    if row['id'].upper() == str(option).upper()), {})
    title = offered.get('title')
    if not option or not isinstance(title, str) or not title:
        return text
    quoted = '|'.join(re.escape(left + title + right)
                      for left, right in [('「', '」'), ('“', '”'), ('"', '"'), ("'", "'")])
    return re.sub(rf'(?P<option>(?<![A-Za-z])(?i:{re.escape(option)})(?![A-Za-z]))'
                  rf'\s*(?:{quoted})', lambda match: match['option'], text)


def _later_decision_conflict(tail, action):
    """Do not lose a second adoption or countermand while excluding task scope.

    Only complete clauses accepted by the same bounded adoption grammar count.
    Mentioning a script/plan, asking a fact, or requesting its preparation does
    not itself become another decision.
    """
    option = action.get('target', {}).get('option_id')
    options = [row['id'] for row in action.get('options') or []]
    remaining = tail
    while remaining:
        remaining = remaining.lstrip(' ,，;；。.!！\n')
        remaining = re.sub(r'^(?:但(?:是)?|but\b|以及|还有|同时|并且|并|也|然后|另外|再)\s*',
                           '', remaining, flags=re.I)
        source = _decision_clause(remaining)
        if not source:
            return None
        clause = _without_selected_title(source, action)
        # Changing the chosen route is a conflict as well as a second adoption.
        # This normalization is used only to refuse the model's proposal; it
        # cannot authorize an option or replace any stored user text.
        choice = re.sub(r'^(?:我(?:们)?\s*)?(?:改(?:用|选(?:择)?|为|成)|换(?:成|用))\s*',
                        '用', clause)
        if _affirmative_decision(choice, None) or any(
                _affirmative_decision(choice, other) for other in options):
            return 'several_targets_named'
        countermand = re.sub(r'^(?:我(?:们)?\s*)?(?:暂时)?'
                            r'(?:不要|不|别|还没决定|尚未决定|没有决定)\s*', '', clause)
        if countermand != clause and _affirmative_decision(countermand, option):
            return 'negative_message'
        if re.fullmatch(r'(?:我(?:们)?\s*)?(?:还没|尚未|没有)(?:决定|同意)', clause):
            return 'negative_message'
        remaining = remaining[len(source):]
    return None


def _affirmative_decision(text, option):
    text = text.strip()
    if option:
        named = rf'(?:当前\s*)?(?:方向\s*|方案\s*)?{re.escape(option)}(?:\s*(?:方向|方案))?'
        # Only an explicit adoption verb may introduce a bounded route
        # description. "A 的收纳结构主线" alone is still only a mention.
        route = r'(?:\s*的\s*[^,，;；。.!！\n？?]{1,40}?(?:主线|路线|思路))?'
        return bool(re.fullmatch(
            rf'(?:我(?:们)?\s*)?(?:就|先|还是|那就)?\s*'
            rf'(?:(?:决定|确定|确认|同意)\s*)?(?:采用|选(?:择)?|用|按)\s*{named}{route}'
            rf'(?:\s*(?:吧|了|并?继续|往下做|执行|来做|做下去))*[。.!！\s]*'
            rf'|{named}\s*(?:吧|了|就它(?:了)?)?[。.!！\s]*'
            rf'|(?:please\s+)?(?:choose|select|use|adopt|go with)\s+{named}[.!\s]*', text, re.I))
    return bool(re.fullmatch(
        r'(?:我(?:们)?\s*)?(?:就|先|那就)?(?:采用|通过|批准|确认|同意采用)'
        r'(?:当前|这个|这份|这版|本版)?(?:脚本|完整方案|方案)(?:并?继续|吧|了)*[。.!！\s]*'
        r'|(?:当前|这个|这份|这版)?(?:脚本|完整方案|方案)(?:可以|通过|没问题|就这样)(?:了|吧|并?继续)*[。.!！\s]*'
        r'|(?:approve|adopt|use) (?:the |this |current )?(?:script|plan)[.!\s]*', text, re.I))


def task_constraints(receipt):
    """Freeze narrow, explicit limits; this is not general language inference.

    Unknown requests still go to the model. These guards prevent a model's
    first route from overriding recognisable limits in the trusted input.
    """
    from .steer_intent import classify
    text = receipt['text']
    target = receipt.get('feedback_target') or {}
    from .production_feedback import PRODUCTION_TARGETS
    production_feedback = target.get('kind') in PRODUCTION_TARGETS
    state = classify(text)['class']
    analysis_only = bool(re.search(r'(?:只|仅)(?:要|做|需要)?(?:分析|拆解|解读)|(?:不要|先不)(?:开始)?创作', text))
    prompt_only = bool(re.search(r'(?:只|仅)(?:要|做|需要|交付)?(?:参考适配)?提示词', text))
    local_only = bool(production_feedback or target.get('path') or re.search(
        r'(?:只|仅)(?:改|修改|调整|替换|把)|(?:其[他余]|剩[下余]).{0,8}(?:保留|不[动变改])', text))
    formats = [fmt for fmt, pattern in (
        ('photo', r'(?:只|仅)(?:要|做|需要|制作)?图文|(?:不要|不做)视频'),
        ('video', r'(?:只|仅)(?:要|做|需要|制作)?视频|(?:不要|不做)图文'),
    ) if re.search(pattern, text)]
    return {'message_id': receipt['message_id'], 'text': text,
            'request_kind': receipt.get('request_kind'), 'feedback_target': target,
            'attachments': receipt.get('attachments'), 'reference_target': receipt.get('reference_target'),
            'reference_recovery': receipt.get('reference_recovery'),
            'reference_consent': receipt.get('reference_consent', False),
            'readonly': state in {'stop', 'status', 'discussion'} or analysis_only,
            'analysis_only': analysis_only, 'prompt_only': prompt_only,
            'production_feedback': production_feedback,
            **({'allowed_stages': []} if production_feedback else {}),
            'local_only': local_only, 'format': formats[0] if len(formats) == 1 else None}


def continue_after(receipt, proposed, *, interpretation=None):
    """A grounded pause can suppress continuation, never grant permission.

    Callers pass the saved interpretation for this exact receipt. Commit reads
    it again under the run lock rather than accepting interpretation fields in
    a model's proposed action.
    """
    if interpretation is not None:
        if interpretation.get('source_message_id') != receipt['message_id']:
            raise PipelineError('message_mismatch', '接续条件没有绑定这条真实用户消息。')
        if interpretation['task']['pause_after']:
            return False
    text = receipt['text']
    if re.search(r'(?:不要|别|不)(?:自动)?(?:继续|接续|往下|推进)|先(?:暂停|停)|'
                 r'等我|等(?:确认|通知)|暂缓|采用.*(?:暂停|别动)|'
                 r'don.t continue|do not continue|pause|hold off|wait for', text, re.I):
        return False
    return proposed.get('continue_after', True) is not False


def project_constraints(store, run_id):
    """Project explicit constraints from the journal, without reading old chat."""
    result = {}
    for event in store.read_events(run_id):
        if event['kind'] == 'user_constraint_recorded':
            result[event['constraint']['id']] = dict(event['constraint'])
        elif event['kind'] == 'user_constraint_superseded':
            for identity in event['constraint_ids']:
                if identity in result:
                    result[identity]['status'] = 'superseded'
                    result[identity]['superseded_by'] = event['source_message_id']
        elif event['kind'] == 'user_constraint_scope_replaced':
            for identity in event['constraint_ids']:
                if identity in result:
                    result[identity].setdefault('exceptions', []).append({
                        'scope': event['scope'], 'source_message_id': event['source_message_id'],
                        'replacement_id': event['replacement_id'],
                        **{name: event[name] for name in ('quote', 'operation', 'value') if name in event}})
    return list(result.values())


def _explicit_project_clauses(text):
    """Bounded explicit constructions, not extraction from arbitrary prose.

    Unknown requirements remain in the current original message; they are not
    silently promoted to permanent project policy. Local edits stay local.
    """
    if re.search(r'[？?「」“”"\']|如果|假如|考虑|是否|能否|他说|她说|同事说', text):
        return
    for quote in re.split(r'[，,。；;\n]', text):
        quote = quote.strip()
        if not quote or len(quote) > 300 or re.search(r'[？?「」“”"\']|如果|假如|考虑|是否|能否|他说|她说', quote):
            continue
        if re.fullmatch(r'(?:整片|全片)?(?:不要|不用|禁止)(?:再)?街拍', quote):
            yield 'visual.street_shooting', quote, 'forbid', False
        elif re.fullmatch(r'(?:现在)?(?:取消|撤销)(?:之前的)?(?:不要|禁止)街拍(?:的限制)?', quote):
            yield 'visual.street_shooting', quote, 'released', True
        else:
            match = re.fullmatch(
                r'(?:现在)?(?P<keep>保留|继续保持)?(?:整片|全片)(?:的场景)?(?P<change>改为|改成|改)?'
                r'(?:都|全部|必须)?(?:在|放在)(?P<place>家里|家中|室内|办公室|公园|街上|户外)(?:拍摄|拍|完成)?', quote)
            if match:
                yield 'visual.setting', quote, match['place'], bool(match['change'] or match['keep'])


def record_project_constraints(store, run_id, receipt):
    """Called only when this real input starts execution, never from accept()."""
    with store.run_lock(run_id):
        for key, quote, value, replacing in _explicit_project_clauses(receipt['text']):
            rows = project_constraints(store, run_id)
            identity = _digest({'message_id': receipt['message_id'], 'key': key, 'quote': quote})[:24]
            if any(row['id'] == identity for row in rows):
                continue
            current = [row for row in rows if row['key'] == key and row['status'] in {'active', 'needs_clarification'}]
            active = [row for row in current if row['status'] == 'active']
            if active and active[-1]['value'] == value and not replacing:
                continue
            if replacing and current:
                store.append_event(run_id, 'user_constraint_superseded',
                    constraint_ids=[row['id'] for row in current], source_message_id=receipt['message_id'])
            status = ('released' if value == 'released' else
                      'needs_clarification' if active and not replacing else 'active')
            store.append_event(run_id, 'user_constraint_recorded', constraint={
                'id': identity, 'source_message_id': receipt['message_id'], 'quote': quote,
                'key': key, 'value': value, 'status': status,
                'scope': {'stages': ['direction', 'material_plan', 'writing'], 'aspect': 'visual_design'}})
        return [row for row in project_constraints(store, run_id)
                if row['status'] in {'active', 'needs_clarification'}]


# This tool asks the model for semantics rather than trying to infer arbitrary
# creative requirements from a growing keyword list. Its result is grounded
# interpretation, never approval, evidence adoption, or a change to ActiveBrief.
_CONSTRAINT_KEY_PATTERN = r'[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+'
_PROTECTED_CONSTRAINT_ROOTS = {'goal', 'approval', 'authorization', 'permission',
                               'budget', 'evidence', 'product', 'decision'}

TASK_INTERPRETATION_TOOL = {
    'name': 'propose_task_interpretation',
    'description': ('解释本轮真实用户原话的交付范围及内容条件。原话复杂、包含长期条件、撤销或替换时先调用。'
                    'quote 必须逐字引用当前原话；转述、假设、问题不作用户指令。'
                    '条件默认仅本轮；project 必须另引明确持续范围。替换/撤销用 read_project_state '
                    '中的准确 constraint id。没有确定解释时用 clarify 并说明歧义。'
                    '此工具不采用方向、不改商品事实或项目目标、不批准图片、不授权费用。'
                    + CONSTRAINT_SCOPE_GUIDANCE + EDIT_SCOPE_GUIDANCE + REPLY_GUIDANCE),
    'input_schema': {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'image_use': {'type': 'object', 'additionalProperties': False,
                          'description': '可选的只读图片分析来源绑定。省略时服务端使用本条图片附件或当前完整源图集合；子集须准确引用原话中的图片名。photo 仅表示源图片，不切换项目创作格式。',
                          'properties': {'quote': {'type': 'string'},
                                         'images': {'type': 'array', 'minItems': 1, 'maxItems': 12,
                                                    'items': {'type': 'object', 'additionalProperties': False,
                                                              'properties': {'name': {'type': 'string'},
                                                                             'sha256': {'type': 'string'},
                                                                             'purpose': {'type': 'string', 'enum': ['product', 'reference']}},
                                                              'required': ['name', 'sha256', 'purpose']}}},
                          'required': ['quote', 'images']},
            'reuse_artifact': {'type': 'object', 'additionalProperties': False,
                              'description': '只有用户明确要求沿用已有文字交付时填写。绑定其指定的当前准确版本及表达沿用的原话；重新创作不能填写。默认要求本轮新交付。',
                              'properties': {'stage': {'type': 'string', 'enum': ['intake', 'direction', 'writing']},
                                             'version': {'type': 'integer', 'minimum': 1},
                                             'sha256': {'type': 'string'}, 'quote': {'type': 'string'}},
                              'required': ['stage', 'version', 'sha256', 'quote']},
            'video_use': {'type': 'object', 'additionalProperties': False,
                          'description': '仅自然语言明确的视频用途或当前用途回答。附件/准确参考元数据已给用途时可省略。裸链接无用途时用 clarify，不填推测用途。quote 引用本条实际表达用途或要求的原话，不只引用 URL。',
                          'properties': {'source': {'type': 'string'},
                                         'purpose': {'type': 'string', 'enum': ['product', 'reference']},
                                         'quote': {'type': 'string'}},
                          'required': ['source', 'purpose', 'quote']},
            'task': {'type': 'object', 'additionalProperties': False,
                     'properties': {
                         'quote': {'type': 'string'},
                         'mode': {'type': 'string', 'enum': ['produce', 'revise', 'analyze', 'discuss',
                                                            'status', 'stop', 'clarify']},
                         'deliverable': {'type': 'string',
                              'description': READ_ONLY_TASK_GUIDANCE,
                              'enum': ['intake', 'direction', 'writing',
                              'production_input', 'production_media', 'revision', 'analysis', 'reference_prompt', 'follow_up']},
                         'format': {'type': 'string', 'enum': ['video', 'photo', 'none']},
                         'allowed_stages': {'type': 'array', 'items': {'type': 'string',
                                                   'enum': ['intake', 'direction', 'material_plan', 'writing']}},
                         'pause_after': {'type': 'boolean'}},
                     'required': ['quote', 'mode', 'deliverable', 'format', 'allowed_stages', 'pause_after']},
            'constraints': {'type': 'array', 'maxItems': 32, 'items': {
                'type': 'object', 'additionalProperties': False,
                'properties': {
                    'quote': {'type': 'string'},
                    'key': {'type': 'string', 'pattern': '^' + _CONSTRAINT_KEY_PATTERN + '$',
                            'description': '内容条件名称必须为小写带点名称，例如 visual.setting、script.duration、style.dialogue、content.sources；不能只用下划线。goal、approval、authorization、permission、budget、evidence、product、decision 是受保护的根名称，不能作内容条件。'},
                    'value': {'type': 'string'},
                    'operation': {'type': 'string', 'enum': ['add', 'replace', 'release', 'clarify'],
                                  'description': '本字段只处理内容条件：没有现存准确 constraint id 时用 add；replace/release 须指向同名既有条件。制作输入的替换由 prepare_production.supersedes 绑定 draft_id，不能因用户说替换输入就把这里写成 replace。'},
                    'polarity': {'type': 'string', 'enum': ['require', 'forbid', 'prefer']},
                    'intent': {'type': 'string', 'enum': ['instruction', 'question', 'hypothesis',
                                                          'reported', 'uncertain']},
                    'scope': {'type': 'object', 'additionalProperties': False,
                              'properties': {
                                  'lifetime': {'type': 'string', 'enum': ['turn', 'project']},
                                  'stages': {'type': 'array', 'minItems': 0, 'uniqueItems': True,
                                             'items': {'type': 'string', 'enum': ['intake', 'direction',
                                                                            'material_plan', 'writing']}},
                                  'paths': {'type': 'array', 'items': {'type': 'array', 'minItems': 1,
                                            'items': {'type': ['string', 'integer']}}},
                                  'quote': {'type': 'string'}},
                              'required': ['lifetime', 'stages', 'paths', 'quote']},
                    'supersedes': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string'}},
                    'conflict_with': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string'}},
                    'ambiguity': {'type': 'string',
                                  'description': '只填写真实原话中仍未确定、且阻止执行的具体问题；明确要求时必须为空字符串。范围限制、保留要求、执行方式与已确定的局部对象放在 value/scope，不属于歧义。非空会把条件记为 needs_clarification 并阻断工作，不能当备注或推理说明。'}},
                'required': ['quote', 'key', 'value', 'operation', 'polarity', 'intent', 'scope',
                             'supersedes', 'conflict_with', 'ambiguity']}},
        },
        'required': ['task', 'constraints']},
}


def interpretation_for(store, run_id, message_id):
    events = store.read_events(run_id)
    original = next((dict(event['interpretation']) for event in reversed(events)
                    if event['kind'] == 'user_task_interpretation_recorded'
                    and event['source_message_id'] == message_id), None)
    if original and 'video_use' not in original:
        bound = next((event for event in events if event['kind'] == 'user_video_use_bound'
                      and event['source_message_id'] == message_id
                      and event['interpretation_id'] == original['id']), None)
        if bound:
            original.update(video_use=dict(bound['video_use']),
                            video_use_proposal_sha256=bound['proposal_sha256'])
    if original and 'image_use' not in original:
        bound = next((event for event in events if event['kind'] == 'user_image_use_bound'
                      and event['source_message_id'] == message_id
                      and event['interpretation_id'] == original['id']), None)
        if bound:
            original['image_use'] = bound['image_use']
    return original


def _quote_valid(text, quote, *, optional=False):
    return ((optional and quote == '')
            or isinstance(quote, str) and bool(quote.strip()) and quote in text)


def _quoted(text, quote, *, optional=False):
    if not _quote_valid(text, quote, optional=optional):
        raise PipelineError('task_interpretation', '解释必须绑定本轮用户原话中的准确连续引用。')


def _complete_interpretation_shape(proposal):
    """A bounded shape gate for this tool, never a semantic acceptance check."""
    remaining = 2048

    def fits(value, schema):
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            return False
        kinds = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
        actual = ('boolean' if type(value) is bool else 'integer' if type(value) is int else
                  'string' if isinstance(value, str) else 'array' if isinstance(value, list) else
                  'object' if isinstance(value, dict) else None)
        if actual not in kinds or 'enum' in schema and value not in schema['enum']:
            return False
        if actual == 'object':
            properties = schema['properties']
            return (set(schema.get('required', [])) <= set(value) <= set(properties)
                    and all(fits(child, properties[key]) for key, child in value.items()))
        if actual == 'array':
            return (schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', remaining)
                    and all(fits(child, schema['items']) for child in value))
        return True

    return fits(proposal, TASK_INTERPRETATION_TOOL['input_schema'])


def _preflight_interpretation_fields(text, proposal):
    """Report independent fixed field errors before first binding; never repair."""
    if not _complete_interpretation_shape(proposal):
        return
    issues = []

    def quote(path, value, *, optional=False):
        valid = _quote_valid(text, value, optional=optional)
        if not valid:
            issues.append({'path': path, 'code': 'quote_not_contiguous',
                           'message': '解释必须绑定本轮用户原话中的准确连续引用。'})
        return valid

    quote(['task', 'quote'], proposal['task']['quote'])
    for name in ('reuse_artifact', 'video_use', 'image_use'):
        if name in proposal:
            quote([name, 'quote'], proposal[name]['quote'])
    for index, row in enumerate(proposal['constraints']):
        prefix = ['constraints', index]
        quoted = quote([*prefix, 'quote'], row['quote'])
        quote([*prefix, 'scope', 'quote'], row['scope']['quote'],
              optional=row['scope']['lifetime'] == 'turn')
        issue = typed_condition_issue(row)
        if (issue and issue['code'] in {'reply_format_invalid', 'reply_numeric_value_invalid',
                                       'reply_numeric_witness_missing'}
                and (issue['code'] != 'reply_numeric_witness_missing' or quoted)):
            issues.append({'path': [*prefix, issue['field']], 'code': issue['code'],
                           'message': issue['message']})
    if issues:
        detail = {'code': 'interpretation_field_errors', 'issues': issues[:6],
                  'remaining_errors': max(0, len(issues) - 6)}
        raise PipelineError('task_interpretation', '整份任务解释尚未接受，本次未写入或发起观察；请纠正所列字段。'
                            + json.dumps(detail, ensure_ascii=False, separators=(',', ':')))


def _valid_constraint_scope(scope):
    """The existing shape check, separate from the reply/artifact domain rule."""
    return not (not isinstance(scope, dict) or set(scope) != {'lifetime', 'stages', 'paths', 'quote'}
                or scope['lifetime'] not in {'turn', 'project'}
                or not isinstance(scope['stages'], list)
                or any(not isinstance(stage, str) for stage in scope['stages'])
                or len(set(scope['stages'])) != len(scope['stages'])
                or any(stage not in STAGES for stage in scope['stages'])
                or not isinstance(scope['paths'], list)
                or any(not isinstance(path, list) or not path or any(
                    type(part) not in {str, int} or isinstance(part, str) and not part
                    or isinstance(part, int) and part < 0 for part in path) for path in scope['paths']))


def _raise_empty_stage_scope_error(proposal, index):
    # Enter only at the original rejection, after that row's source/content
    # and scope shape checks. Whole safe proposals permit bounded same-rule
    # feedback; unsafe proposals retain the single actual rejection.
    indices = [index]
    if _complete_interpretation_shape(proposal):
        indices = [i for i, row in enumerate(proposal['constraints'])
                   if _valid_constraint_scope(row['scope']) and is_reply_scope(row['scope'])
                   and (row['scope']['lifetime'] != 'turn' or row['scope']['paths'])]
    detail = {'code': 'constraint_scope_errors', 'issues': [
        {'path': ['constraints', i, 'scope'], 'code': 'empty_stage_scope_invalid'} for i in indices]}
    raise PipelineError('task_interpretation',
                        '回复条件必须为本轮 turn、空阶段 stages=[] 与空字段 paths=[]；'
                        '本次未接受任务解释。' + CONSTRAINT_SCOPE_GUIDANCE
                        + json.dumps(detail, ensure_ascii=False, separators=(',', ':')))


def _validate_video_use(receipt, video_use):
    if (not isinstance(video_use, dict) or set(video_use) != {'source', 'purpose', 'quote'}
            or video_use['purpose'] not in {'product', 'reference'}
            or not isinstance(video_use['source'], str) or not video_use['source'].strip()
            or len(video_use['source']) > 8192):
        raise PipelineError('task_interpretation', '视频用途必须绑定实际来源及本条原话。')
    _quoted(receipt['text'], video_use['quote'])


def _paths_overlap(left, right):
    return not left or not right or any(
        a[:min(len(a), len(b))] == b[:min(len(a), len(b))] for a in left for b in right)


def _scopes_overlap(left, right):
    if is_reply_scope(left) or is_reply_scope(right):
        return is_reply_scope(left) and is_reply_scope(right)
    return bool(set(left['stages']) & set(right['stages'])) and _paths_overlap(
        left.get('paths', []), right.get('paths', []))


def _scope_contains(outer, inner):
    if is_reply_scope(outer) or is_reply_scope(inner):
        return is_reply_scope(outer) and is_reply_scope(inner)
    if not set(inner['stages']).issubset(outer['stages']):
        return False
    parent, child = outer.get('paths', []), inner.get('paths', [])
    return not parent or bool(child) and all(any(path[:len(base)] == base for base in parent)
                                           for path in child)


def _scope_equal(left, right):
    return set(left['stages']) == set(right['stages']) and sorted(
        map(str, left.get('paths', []))) == sorted(map(str, right.get('paths', [])))


def _apply_replacements(rows, replacements):
    """Overlay a temporary turn exception without changing durable policy."""
    result = [json.loads(json.dumps(row)) for row in rows]
    for replacement in replacements:
        if replacement['status'] not in {'active', 'released'}:
            continue
        for row in result:
            if row['id'] not in replacement.get('supersedes', []):
                continue
            if _scope_equal(row['scope'], replacement['scope']):
                row['status'] = 'superseded'
                row['superseded_by'] = replacement['source_message_id']
            else:
                row.setdefault('exceptions', []).append({'scope': replacement['scope'],
                    'source_message_id': replacement['source_message_id'],
                    'replacement_id': replacement['id'], 'quote': replacement['quote'],
                    'operation': replacement['operation'], 'value': replacement['value']})
    return result


def applicable_constraints(store, run_id, *, stage=None, user_input_id=None, paths=None):
    """A reconstructable view; pending receipts have no interpretation to apply.

    A stage-wide reader receives exceptions explicitly. A reader of a concrete
    field sees only requirements that still apply at that field. An unresolved
    conflict at another role/field must not block unrelated work.
    """
    executing = user_input_id and any(e['kind'] == 'user_message_execution_started'
        and e.get('message_id') == user_input_id for e in store.read_events(run_id))
    return constraints_for_inputs(store, run_id, [user_input_id] if executing else [],
                                  stage=stage, paths=paths)


def constraints_for_inputs(store, run_id, user_input_ids, *, stage=None, paths=None, domain='artifact'):
    """Combine an executing session's consumed inputs without reviving others.

    The session supplies its trusted consumed-message list, never the model.
    Turn conditions last for that session task; latest project conditions and
    explicit later replacement/release remain authoritative. A future receipt
    cannot enter this view just because it was accepted into the UI timeline.
    """
    events = store.read_events(run_id)
    receipts = {event['message_id']: event for event in events if event['kind'] == 'user_message_received'}
    executing = {event['message_id'] for event in events if event['kind'] == 'user_message_execution_started'}
    identities = list(dict.fromkeys(user_input_ids))
    if any(identity not in receipts or identity not in executing for identity in identities):
        raise PipelineError('task_interpretation', '会话条件只能读取本任务已经执行的真实用户消息。')
    rows = project_constraints(store, run_id)
    for identity in identities:
        interpretation = interpretation_for(store, run_id, identity)
        if interpretation:
            rows += [row for row in interpretation['constraints'] if row['scope']['lifetime'] == 'turn']
    replacements = sorted((row for row in rows if row.get('supersedes')),
                          key=lambda row: receipts.get(row['source_message_id'], {}).get('seq', 0))
    return constraints_for_scope(_apply_replacements(rows, replacements), stage=stage, paths=paths,
                                 domain=domain)


def constraints_for_scope(rows, *, stage=None, paths=None, domain='artifact'):
    """Filter a pinned constraint snapshot with the same rule as live reads."""
    if domain not in {'artifact', 'reply', 'all'} or domain != 'artifact' and (stage or paths):
        raise ValueError('invalid constraint domain query')
    query = {'stages': [] if domain == 'reply' else [stage] if stage else ['intake', 'direction', 'material_plan', 'writing'],
             'paths': paths or []}
    selected = []
    for row in rows:
        if row['status'] not in {'active', 'needs_clarification'} or (
                domain != 'all' and not _scopes_overlap(row['scope'], query)):
            continue
        if stage and any(_scope_contains(ex['scope'], query) for ex in row.get('exceptions', [])):
            continue
        selected.append(row)
    return selected


def reply_constraints_for_input(store, run_id, message_id):
    """Only this executing real source's reply conditions, never old policy."""
    rows = constraints_for_inputs(store, run_id, [message_id], domain='reply')
    return [row for row in rows if row['source_message_id'] == message_id]


def _interpretation_result(store, run_id, interpretation, prior_user_input_ids):
    reply_rows = reply_constraints_for_input(store, run_id, interpretation['source_message_id'])
    return {**interpretation, 'project_constraints': constraints_for_inputs(store, run_id,
            [*prior_user_input_ids, interpretation['source_message_id']]),
            **({'reply_constraints': reply_rows} if reply_rows else {})}


def constraint_conflicts(store, run_id, *, stage=None, user_input_id=None, paths=None):
    return [row for row in applicable_constraints(store, run_id, stage=stage,
            user_input_id=user_input_id, paths=paths) if row['status'] == 'needs_clarification']


def interpreted_task_constraints(receipt, interpretation=None):
    """Add semantic limits to the narrow ingress guards; never relax a guard."""
    result = task_constraints(receipt)
    if not interpretation:
        return result
    task = interpretation['task']
    result.update({'interpretation_id': interpretation['id'], 'task_mode': task['mode'],
                   'deliverable': task['deliverable'], 'allowed_stages': task['allowed_stages'],
                   'pause_after': task['pause_after'], 'video_use': interpretation.get('video_use')})
    result['image_use'] = interpretation.get('image_use')
    result['reuse_artifact'] = interpretation.get('reuse_artifact')
    # Only the named, source-bound editing condition grants a bounded revision
    # plan. Ordinary content paths retain their content-only meaning.
    result['edit_scopes'] = json.loads(json.dumps(interpretation.get('edit_scopes', [])))
    result['local_only'] = result['local_only'] or bool(result['edit_scopes']) and all(
        scope['mode'] == 'local' for scope in result['edit_scopes'])
    if result['production_feedback']:
        result['allowed_stages'] = []
    result['readonly'] = result['readonly'] or task['mode'] in {'analyze', 'discuss', 'status', 'stop', 'clarify'}
    result['analysis_only'] = result['analysis_only'] or task['mode'] == 'analyze'
    if not result['format'] and task['format'] != 'none':
        result['format'] = task['format']
    # A condition's paths say where its content applies. They do not say that
    # the task may only edit those fields: a complete new script can still have
    # a tone requirement on its opening line. Non-instructions have no routing
    # authority either. Local edit limits come from the original task/target.
    return result


def _freeze_edit_scopes(store, run_id, receipt, task, rows):
    """Freeze exact bases in the existing interpretation journal, not a policy DB.

    The model still owns semantic interpretation. This gate verifies its source,
    field identities and typed limits before binding; later plans cannot replace
    that interpretation with a broader declaration.
    """
    selected = [row for row in rows if row['key'] == EDIT_SCOPE_KEY]
    guards = task_constraints(receipt)
    target = guards['feedback_target']
    text_edit = task['mode'] == 'revise' and task['deliverable'] in {'revision', 'intake', 'direction', 'writing'}
    if not selected:
        # A saved unresolved question with no allowed stage cannot edit.
        # Preserve that waiting state instead of requiring a fabricated target.
        from .condition_blocker import content_scope
        stage, paths = content_scope(task['deliverable'], target=target)
        awaiting_scope = not task['allowed_stages'] and any(
            row['status'] == 'needs_clarification' and row['intent'] == 'instruction'
            for row in constraints_for_scope(rows, stage=stage, paths=paths))
        if text_edit and not awaiting_scope and not guards['production_feedback'] and not target.get('path'):
            raise PipelineError('task_interpretation',
                '尚未绑定编辑范围：请在同一原话的首次解释中完整填写 content.edit_scope；'
                '本次未写入，可纠正解释后重试，真实范围不明确时先澄清。')
        return []
    from .pipeline import Pipeline
    from .revision_scope import _cell, _path_key, check_base
    pipe, scopes, stages = Pipeline(store), [], set()
    for row in selected:
        scope = row['scope']
        if (not text_edit or guards['production_feedback'] or row['value'] not in {'local', 'rework'}
                or row['operation'] != 'add' or row['polarity'] != 'require'
                or row['intent'] != 'instruction' or row['status'] != 'active'
                or scope['lifetime'] != 'turn' or len(scope['stages']) != 1
                or scope['stages'][0] not in task['allowed_stages']
                or not scope['quote'].strip()):
            raise PipelineError('task_interpretation', 'content.edit_scope 必须绑定本条明确 revise 任务的准确编辑范围。')
        stage, mode, paths = scope['stages'][0], row['value'], scope['paths']
        if stage in stages or (mode == 'local') != bool(paths) or len(paths) > 200:
            raise PipelineError('task_interpretation', '编辑范围须按阶段唯一绑定；local 列出字段，rework 的 paths 必须为空。')
        if task['deliverable'] in STAGES and task['deliverable'] != stage:
            raise PipelineError('task_interpretation', '编辑范围不能改变本轮的准确交付阶段。')
        if guards['local_only'] and mode != 'local':
            raise PipelineError('task_interpretation', '编辑范围不能把原话或所选字段限定的局部修改扩大为整稿。')
        state = pipe.stage_state(run_id, stage)
        if not state.latest:
            raise PipelineError('task_interpretation', '修订须先读取当前已有产物，不能把新建任务声明为已有稿修改。')
        frozen = {'stage': stage, 'version': state.latest.version, 'sha256': state.latest.sha256,
                  'mode': mode, 'paths': json.loads(json.dumps(paths)), 'quote': row['quote'],
                  'scope_quote': scope['quote'], 'constraint_id': row['id'],
                  'source_message_id': receipt['message_id'],
                  'source_sha256': hashlib.sha256(receipt['text'].encode('utf-8')).hexdigest()}
        base, inputs = check_base(store, pipe, run_id, frozen)
        if target.get('path') and (stage != target.get('stage')
                or base.version != target.get('version') or base.sha256 != target.get('artifact_sha256')
                or paths != [target['path']]):
            raise PipelineError('task_interpretation', '编辑范围须保持用户选中的准确版本和字段。')
        keys = [_path_key(path) for path in paths]
        if len(keys) != len(set(keys)):
            raise PipelineError('task_interpretation', '编辑字段不能重复。')
        for path in paths:
            _cell(base.structured, path)
        frozen['inputs'] = inputs
        scopes.append(frozen)
        stages.add(stage)
    return scopes


def validated_edit_scopes(receipt, interpretation):
    """Check saved derived limits against their exact original typed witnesses.

    Callers read the interpretation from Store, then separately check the fixed
    artifact/input fingerprints. This never grants authority from generic paths.
    """
    scopes = (interpretation or {}).get('edit_scopes', [])
    if not scopes:
        return []
    source_sha256 = hashlib.sha256(receipt['text'].encode('utf-8')).hexdigest()
    if (interpretation['source_message_id'] != receipt['message_id']
            or interpretation['task']['mode'] != 'revise' or not isinstance(scopes, list)):
        raise PipelineError('revision_scope', '编辑范围的真实来源或解释身份不一致，请核对原范围。')
    for frozen in scopes:
        witnesses = [row for row in interpretation['constraints'] if row['id'] == frozen['constraint_id']]
        if (len(witnesses) != 1 or frozen['source_message_id'] != receipt['message_id']
                or frozen['source_sha256'] != source_sha256):
            raise PipelineError('revision_scope', '编辑范围的真实来源或解释身份不一致，请核对原范围。')
        row = witnesses[0]
        if (row['key'] != EDIT_SCOPE_KEY or row['status'] != 'active'
                or row['origin'] != 'grounded_model_interpretation' or row['operation'] != 'add'
                or row['intent'] != 'instruction' or row['polarity'] != 'require'
                or row['source_message_id'] != receipt['message_id'] or row['source_sha256'] != source_sha256
                or row['value'] != frozen['mode'] or row['quote'] != frozen['quote']
                or row['scope']['lifetime'] != 'turn' or row['scope']['quote'] != frozen['scope_quote']
                or row['scope']['stages'] != [frozen['stage']] or row['scope']['paths'] != frozen['paths']):
            raise PipelineError('revision_scope', '编辑范围与首次原话条件不一致，不能扩大本轮修改。')
    return json.loads(json.dumps(scopes))


def record_task_interpretation(store, run_id, receipt, proposal, *, prior_user_input_ids=()):
    """Validate a semantic proposal against an executing, original receipt.

    This does not claim perfect natural-language recognition: the model owns
    semantic interpretation; the server owns source, lifetime, scope, conflicts
    and the inability to create decisions or expand execution authorization.
    """
    from .models import STAGES
    with store.run_lock(run_id):
        trusted = message(store, run_id, receipt.get('message_id'))
        if trusted['text'] != receipt.get('text'):
            raise PipelineError('task_interpretation', '解释不能替换真实用户原话。')
        if not any(e['kind'] == 'user_message_execution_started' and e.get('message_id') == trusted['message_id']
                   for e in store.read_events(run_id)):
            raise PipelineError('task_interpretation', '尚未执行的排队消息不能影响当前任务或项目条件。')
        if (not isinstance(proposal, dict) or not {'task', 'constraints'} <= set(proposal)
                or set(proposal) - {'task', 'constraints', 'video_use', 'image_use', 'reuse_artifact'}):
            raise PipelineError('task_interpretation', '任务解释只能包含 task、constraints 与可选视频/图片来源、reuse_artifact。')
        fingerprint = _digest(proposal)
        prior = interpretation_for(store, run_id, trusted['message_id'])
        if prior:
            if fingerprint not in {prior['proposal_sha256'], prior.get('video_use_proposal_sha256')}:
                # Only complete omitted source metadata. The original task,
                # conditions and reuse contract must remain byte-identical.
                base = {key: value for key, value in proposal.items() if key != 'video_use'}
                if ('video_use' not in prior and 'image_use' not in prior and proposal.get('video_use') is not None
                        and _digest(base) == prior['proposal_sha256']):
                    video_use = proposal['video_use']
                    _validate_video_use(trusted, video_use)
                    validate_observation_source(store, run_id, trusted, video_use['source'])
                    tentative = {**prior, 'video_use': dict(video_use)}
                    observation_admission(store, run_id, trusted, tentative,
                                          video_use['source'], video_use['purpose'], video_use['quote'])
                    store.append_event(run_id, 'user_video_use_bound',
                        source_message_id=trusted['message_id'], interpretation_id=prior['id'],
                        proposal_sha256=fingerprint, video_use=dict(video_use))
                    prior = interpretation_for(store, run_id, trusted['message_id'])
                    return _interpretation_result(store, run_id, prior, prior_user_input_ids)
                raise PipelineError('task_interpretation', '同一条用户消息的解释已经绑定；需要新原话澄清后修改。')
            return _interpretation_result(store, run_id, prior, prior_user_input_ids)
        _preflight_interpretation_fields(trusted['text'], proposal)
        task = proposal['task']
        task_fields = {'quote', 'mode', 'deliverable', 'format', 'allowed_stages', 'pause_after'}
        if not isinstance(task, dict) or set(task) != task_fields:
            raise PipelineError('task_interpretation', '任务解释字段不完整或包含未允许的权限字段。')
        _quoted(trusted['text'], task['quote'])
        task_schema = TASK_INTERPRETATION_TOOL['input_schema']['properties']['task']['properties']
        if any(task[name] not in task_schema[name]['enum'] for name in ('mode', 'deliverable', 'format')):
            raise PipelineError('task_interpretation', '任务模式、交付或格式无效。')
        if (not isinstance(task['allowed_stages'], list) or any(stage not in STAGES for stage in task['allowed_stages'])
                or type(task['pause_after']) is not bool):
            raise PipelineError('task_interpretation', '任务阶段和暂停范围无效。')
        if (task['mode'] == 'analyze' and task['deliverable'] == 'analysis'
                and task['format'] in {'photo', 'none'} and proposal.get('video_use') is None
                and task['allowed_stages']):
            raise PipelineError('task_interpretation',
                                '纯图片分析不能绑定创作阶段：allowed_stages 必须为 []；请按同一条原话纠正后重新提交解释。')
        guards = task_constraints(trusted)
        if guards['production_feedback']:
            from .production_feedback import PRODUCTION_DELIVERABLES
            if task['deliverable'] not in PRODUCTION_DELIVERABLES | {'analysis', 'follow_up'}:
                raise PipelineError('production_feedback_scope',
                                    '本轮绑定所选镜头的制作反馈，不能改成需求、方向或脚本修订。')
        reuse = proposal.get('reuse_artifact')
        if reuse is not None:
            if (not isinstance(reuse, dict) or set(reuse) != {'stage', 'version', 'sha256', 'quote'}
                    or reuse['stage'] not in {'intake', 'direction', 'writing'}
                    or reuse['stage'] != task['deliverable'] or type(reuse['version']) is not int
                    or reuse['version'] < 1 or not isinstance(reuse['sha256'], str)):
                raise PipelineError('task_interpretation', '沿用须绑定本轮文字交付的准确版本。')
            _quoted(trusted['text'], reuse['quote'])
            from .pipeline import Pipeline
            state = Pipeline(store).stage_state(run_id, reuse['stage'])
            if (not state.latest or state.stale or not state.integrity_ok or state.open_attempt
                    or state.latest.version != reuse['version'] or state.latest.sha256 != reuse['sha256']):
                raise PipelineError('task_interpretation', '指定沿用的产物已改变或不可用，请核对当前版本。')
        video_use = proposal.get('video_use')
        if video_use is not None:
            _validate_video_use(trusted, video_use)
            validate_observation_source(store, run_id, trusted, video_use['source'])
        image_use = proposal.get('image_use')
        if image_use is not None:
            if (task['mode'] != 'analyze' or task['deliverable'] != 'analysis'
                    or task['allowed_stages'] or task['format'] == 'video' or video_use is not None):
                raise PipelineError('task_interpretation', '图片来源绑定只适用于本轮独立的只读图片分析。')
            from .agent_images import validate_image_use
            validate_image_use(store, run_id, trusted, image_use)
        if guards['readonly'] and task['mode'] in {'produce', 'revise'}:
            raise PipelineError('task_interpretation', '语义解释不能覆盖用户明确的只读要求。')
        if guards['format'] and task['format'] not in {'none', guards['format']}:
            raise PipelineError('task_interpretation', '语义解释不能覆盖用户明确的交付格式。')
        raw_rows = proposal['constraints']
        if not isinstance(raw_rows, list) or len(raw_rows) > 32:
            raise PipelineError('task_interpretation', '条件列表无效。')
        existing = constraints_for_inputs(store, run_id, prior_user_input_ids, domain='all')
        by_id = {row['id']: row for row in existing}
        new_rows = []
        row_fields = {'quote', 'key', 'value', 'operation', 'polarity', 'intent', 'scope',
                      'supersedes', 'conflict_with', 'ambiguity'}
        for index, raw in enumerate(raw_rows):
            if not isinstance(raw, dict) or set(raw) != row_fields:
                raise PipelineError('task_interpretation', '条件字段不完整或包含未允许的权限字段。')
            _quoted(trusted['text'], raw['quote'])
            if not isinstance(raw['key'], str) or not re.fullmatch(_CONSTRAINT_KEY_PATTERN, raw['key']):
                raise PipelineError('task_interpretation', '条件名称格式无效：key 必须为小写带点的内容名称，例如 visual.setting 或 script.duration；不能只用下划线。')
            if raw['key'].split('.')[0] in _PROTECTED_CONSTRAINT_ROOTS:
                raise PipelineError('task_interpretation', '内容条件不能改目标、商品事实、决定或执行权限。')
            if (not isinstance(raw['value'], str) or not raw['value'].strip() or len(raw['value']) > 1000
                    or not isinstance(raw['ambiguity'], str) or len(raw['ambiguity']) > 1000
                    or raw['operation'] not in {'add', 'replace', 'release', 'clarify'}
                    or raw['polarity'] not in {'require', 'forbid', 'prefer'}
                    or raw['intent'] not in {'instruction', 'question', 'hypothesis', 'reported', 'uncertain'}):
                raise PipelineError('task_interpretation', '条件内容、操作或语气无效。')
            scope = raw['scope']
            if not _valid_constraint_scope(scope):
                raise PipelineError('task_interpretation', '条件范围无效。')
            if is_reply_scope(scope) and (scope['lifetime'] != 'turn' or scope['paths']):
                _raise_empty_stage_scope_error(proposal, index)
            _quoted(trusted['text'], scope['quote'], optional=scope['lifetime'] == 'turn')
            validate_typed_condition(raw)
            for name in ('supersedes', 'conflict_with'):
                ids = raw[name]
                if (not isinstance(ids, list) or any(not isinstance(identity, str) or identity not in by_id
                                                     for identity in ids) or len(set(ids)) != len(ids)):
                    raise PipelineError('task_interpretation', '替换或冲突必须指向现存且有效的条件。')
            if any(is_reply_scope(by_id[identity]['scope']) != is_reply_scope(scope)
                   for identity in raw['conflict_with']):
                raise PipelineError('task_interpretation', '回复条件与产物条件不能跨范围绑定冲突。')
            if raw['operation'] in {'replace', 'release'}:
                if not raw['supersedes'] or any(by_id[identity]['key'] != raw['key'] or not _scope_contains(
                        by_id[identity]['scope'], scope) for identity in raw['supersedes']):
                    raise PipelineError('task_interpretation', '替换或撤销须绑定同一条件，且不能扩大原条件范围。')
            elif raw['supersedes']:
                raise PipelineError('task_interpretation', '只有明确替换或撤销才能使旧条件失效。')
            identity = _digest({'message_id': trusted['message_id'], 'constraint': raw})[:24]
            row = json.loads(json.dumps(raw))
            row.update({'id': identity, 'source_message_id': trusted['message_id'],
                        'source_sha256': hashlib.sha256(trusted['text'].encode()).hexdigest(),
                        'origin': 'grounded_model_interpretation', 'status': 'active'})
            conflicts = [old for old in existing + new_rows if old['status'] == 'active'
                         and old['id'] not in row['supersedes'] and old['key'] == row['key']
                         and _scopes_overlap(old['scope'], scope)
                         and (old['value'] != row['value'] or old.get('polarity',
                              'forbid' if old['value'] == 'forbid' else 'require') != row['polarity'])]
            if conflicts:
                row['conflict_with'] = sorted(set(row['conflict_with']) | {old['id'] for old in conflicts})
            if raw['intent'] != 'instruction':
                row['status'] = 'not_instruction'
            elif raw['operation'] == 'clarify' or raw['ambiguity'] or row['conflict_with']:
                row['status'] = 'needs_clarification'
            elif raw['operation'] == 'release':
                row['status'] = 'released'
            new_rows.append(row)
        edit_scopes = _freeze_edit_scopes(store, run_id, trusted, task, new_rows)
        # All fields and references are validated before the first write.
        for row in new_rows:
            if row['scope']['lifetime'] != 'project' or row['status'] == 'not_instruction':
                continue
            if row['status'] != 'needs_clarification':
                full, partial = [], []
                for identity in row['supersedes']:
                    (full if _scope_equal(by_id[identity]['scope'], row['scope']) else partial).append(identity)
                if full:
                    store.append_event(run_id, 'user_constraint_superseded', constraint_ids=full,
                                       source_message_id=trusted['message_id'])
                if partial:
                    store.append_event(run_id, 'user_constraint_scope_replaced', constraint_ids=partial,
                        source_message_id=trusted['message_id'], scope=row['scope'], replacement_id=row['id'],
                        quote=row['quote'], operation=row['operation'], value=row['value'])
            store.append_event(run_id, 'user_constraint_recorded', constraint=row)
        interpretation = {'id': _digest({'message_id': trusted['message_id'], 'proposal': proposal})[:24],
                          'source_message_id': trusted['message_id'], 'proposal_sha256': fingerprint,
                          'task': dict(task), 'constraints': new_rows}
        if edit_scopes:
            interpretation['edit_scopes'] = edit_scopes
        if video_use is not None:
            interpretation['video_use'] = dict(video_use)
        if image_use is not None:
            interpretation['image_use'] = json.loads(json.dumps(image_use))
        if reuse is not None:
            interpretation['reuse_artifact'] = dict(reuse)
        store.append_event(run_id, 'user_task_interpretation_recorded', source_message_id=trusted['message_id'],
                           interpretation=interpretation)
        return _interpretation_result(store, run_id, interpretation, prior_user_input_ids)
