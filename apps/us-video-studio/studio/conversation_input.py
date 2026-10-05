"""Trusted user ingress and the view that accompanied it, in the run journal.

A display receipt identifies a response offered to the browser. It does not
prove attention or human reading, and never grants execution permission.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from .models import PipelineError


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
    _quoted(receipt['text'], quote)
    validate_media_input(store, run_id, receipt.get('attachments'), receipt.get('reference_target'))
    supplied = next((item for item in receipt.get('attachments') or [] if item['name'] == source), None)
    if supplied and supplied.get('purpose') and supplied['purpose'] != purpose:
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
        matching = next((item for item in receipt.get('attachments') or [] if item['name'] == candidate['name']), None)
        if matching and matching.get('purpose') not in {None, 'reference'}:
            raise PipelineError('observation_purpose', '本条输入同时把准确参考标为商品视频，请先澄清用途。')
        return
    if supplied and supplied.get('purpose'):
        return
    use = interpretation.get('video_use')
    if not use or use['source'] != source or use['purpose'] != purpose:
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
    if evidence is not None and (not evidence.strip() or evidence not in text):
        return 'evidence_not_user_words'
    # These are deliberately conservative refusal guards. They do not assert
    # that every remaining sentence is a decision or parse arbitrary language.
    if re.search(r'如果|假如|假设|要是|if\b|suppos|what if', text, re.I):
        return 'hypothetical_message'
    if re.search(r'[「」“”\"\']|他说|她说|别人|朋友说|引用|quoted?|he said|she said', text, re.I):
        return 'quoted_message'
    decision_part = re.split(r'[,，;；]|但|but\b', text, maxsplit=1, flags=re.I)[0]
    if action['action'] != 'return_stage' and re.search(
            r'不要|别(?:用|选|采用|批准)|不(?:采用|选|用|批准|同意)|没(?:有)?(?:同意|决定)|'
            r'不行|不合适|不满意|不通过|不采纳|还没|尚未|犹豫|再想|考虑|'
            r'not\b|don.t\b|do not\b|reject\b', decision_part, re.I):
        return 'negative_message'
    if action['action'] in {'select_direction', 'approve_stage'} and re.search(
            r'修改|调整|改(?:一下|一改|成|为)|先.*改|比較|比较|看看|聊聊|edit\b|change\b|compare\b', decision_part, re.I):
        return 'revision_not_adoption'
    if '?' in text or '？' in text or re.search(r'多少|为什么|怎么看|怎样|如何|是否|能否|会不会|what\b|how\b|why\b', text, re.I):
        return 'question_not_decision'
    named = set(re.findall(r'(?<![A-Za-z])[ABC](?![A-Za-z])', text.upper()))
    if len(named) > 1 or re.search(r'(方向|脚本|方案).*(以及|还有|同时|和|及).*(方向|脚本|方案)', text):
        return 'several_targets_named'
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


def _affirmative_decision(text, option):
    text = text.strip()
    if option:
        named = rf'(?:方向\s*|方案\s*)?{re.escape(option)}(?:\s*(?:方向|方案))?'
        return bool(re.fullmatch(
            rf'(?:我(?:们)?\s*)?(?:就|先|还是|那就)?\s*'
            rf'(?:(?:决定|确定|确认|同意)\s*)?(?:采用|选(?:择)?|用|按)\s*{named}'
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
    state = classify(text)['class']
    analysis_only = bool(re.search(r'(?:只|仅)(?:要|做|需要)?(?:分析|拆解|解读)|(?:不要|先不)(?:开始)?创作', text))
    prompt_only = bool(re.search(r'(?:只|仅)(?:要|做|需要|交付)?(?:参考适配)?提示词', text))
    local_only = bool(target.get('path') or re.search(
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
TASK_INTERPRETATION_TOOL = {
    'name': 'propose_task_interpretation',
    'description': ('解释本轮真实用户原话的交付范围及内容条件。原话复杂、包含长期条件、撤销或替换时先调用。'
                    'quote 必须逐字引用当前原话；转述、假设、问题不作用户指令。'
                    '条件默认仅本轮；project 必须另引明确持续范围。替换/撤销用 read_project_state '
                    '中的准确 constraint id。没有确定解释时用 clarify 并说明歧义。'
                    '此工具不采用方向、不改商品事实或项目目标、不批准图片、不授权费用。'),
    'input_schema': {
        'type': 'object', 'additionalProperties': False,
        'properties': {
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
                         'deliverable': {'type': 'string', 'enum': ['intake', 'direction', 'writing',
                              'production_input', 'production_media', 'revision', 'analysis', 'reference_prompt', 'follow_up']},
                         'format': {'type': 'string', 'enum': ['video', 'photo', 'none']},
                         'allowed_stages': {'type': 'array', 'items': {'type': 'string',
                                                   'enum': ['intake', 'direction', 'material_plan', 'writing']}},
                         'pause_after': {'type': 'boolean'}},
                     'required': ['quote', 'mode', 'deliverable', 'format', 'allowed_stages', 'pause_after']},
            'constraints': {'type': 'array', 'maxItems': 32, 'items': {
                'type': 'object', 'additionalProperties': False,
                'properties': {
                    'quote': {'type': 'string'}, 'key': {'type': 'string'}, 'value': {'type': 'string'},
                    'operation': {'type': 'string', 'enum': ['add', 'replace', 'release', 'clarify']},
                    'polarity': {'type': 'string', 'enum': ['require', 'forbid', 'prefer']},
                    'intent': {'type': 'string', 'enum': ['instruction', 'question', 'hypothesis',
                                                          'reported', 'uncertain']},
                    'scope': {'type': 'object', 'additionalProperties': False,
                              'properties': {
                                  'lifetime': {'type': 'string', 'enum': ['turn', 'project']},
                                  'stages': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                                             'items': {'type': 'string', 'enum': ['intake', 'direction',
                                                                            'material_plan', 'writing']}},
                                  'paths': {'type': 'array', 'items': {'type': 'array', 'minItems': 1,
                                            'items': {'type': ['string', 'integer']}}},
                                  'quote': {'type': 'string'}},
                              'required': ['lifetime', 'stages', 'paths', 'quote']},
                    'supersedes': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string'}},
                    'conflict_with': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string'}},
                    'ambiguity': {'type': 'string'}},
                'required': ['quote', 'key', 'value', 'operation', 'polarity', 'intent', 'scope',
                             'supersedes', 'conflict_with', 'ambiguity']}},
        },
        'required': ['task', 'constraints']},
}


def interpretation_for(store, run_id, message_id):
    return next((dict(event['interpretation']) for event in reversed(store.read_events(run_id))
                 if event['kind'] == 'user_task_interpretation_recorded'
                 and event['source_message_id'] == message_id), None)


def _quoted(text, quote, *, optional=False):
    if optional and quote == '':
        return
    if not isinstance(quote, str) or not quote.strip() or quote not in text:
        raise PipelineError('task_interpretation', '解释必须绑定本轮用户原话中的准确连续引用。')


def _paths_overlap(left, right):
    return not left or not right or any(
        a[:min(len(a), len(b))] == b[:min(len(a), len(b))] for a in left for b in right)


def _scopes_overlap(left, right):
    return bool(set(left['stages']) & set(right['stages'])) and _paths_overlap(
        left.get('paths', []), right.get('paths', []))


def _scope_contains(outer, inner):
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


def constraints_for_inputs(store, run_id, user_input_ids, *, stage=None, paths=None):
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
    return constraints_for_scope(_apply_replacements(rows, replacements), stage=stage, paths=paths)


def constraints_for_scope(rows, *, stage=None, paths=None):
    """Filter a pinned constraint snapshot with the same rule as live reads."""
    query = {'stages': [stage] if stage else ['intake', 'direction', 'material_plan', 'writing'],
             'paths': paths or []}
    selected = []
    for row in rows:
        if row['status'] not in {'active', 'needs_clarification'} or not _scopes_overlap(row['scope'], query):
            continue
        if stage and any(_scope_contains(ex['scope'], query) for ex in row.get('exceptions', [])):
            continue
        selected.append(row)
    return selected


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
    result['reuse_artifact'] = interpretation.get('reuse_artifact')
    result['readonly'] = result['readonly'] or task['mode'] in {'analyze', 'discuss', 'status', 'stop', 'clarify'}
    result['analysis_only'] = result['analysis_only'] or task['mode'] == 'analyze'
    if not result['format'] and task['format'] != 'none':
        result['format'] = task['format']
    # A condition's paths say where its content applies. They do not say that
    # the task may only edit those fields: a complete new script can still have
    # a tone requirement on its opening line. Non-instructions have no routing
    # authority either. Local edit limits come from the original task/target.
    return result


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
                or set(proposal) - {'task', 'constraints', 'video_use', 'reuse_artifact'}):
            raise PipelineError('task_interpretation', '任务解释只能包含 task、constraints 与可选 video_use、reuse_artifact。')
        fingerprint = _digest(proposal)
        prior = interpretation_for(store, run_id, trusted['message_id'])
        if prior:
            if prior['proposal_sha256'] != fingerprint:
                raise PipelineError('task_interpretation', '同一条用户消息的解释已经绑定；需要新原话澄清后修改。')
            return {**prior, 'project_constraints': constraints_for_inputs(store, run_id,
                    [*prior_user_input_ids, trusted['message_id']])}
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
        guards = task_constraints(trusted)
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
            if (not isinstance(video_use, dict) or set(video_use) != {'source', 'purpose', 'quote'}
                    or video_use['purpose'] not in {'product', 'reference'}
                    or not isinstance(video_use['source'], str) or not video_use['source'].strip()
                    or len(video_use['source']) > 8192):
                raise PipelineError('task_interpretation', '视频用途必须绑定实际来源及本条原话。')
            _quoted(trusted['text'], video_use['quote'])
        if guards['readonly'] and task['mode'] in {'produce', 'revise'}:
            raise PipelineError('task_interpretation', '语义解释不能覆盖用户明确的只读要求。')
        if guards['format'] and task['format'] not in {'none', guards['format']}:
            raise PipelineError('task_interpretation', '语义解释不能覆盖用户明确的交付格式。')
        raw_rows = proposal['constraints']
        if not isinstance(raw_rows, list) or len(raw_rows) > 32:
            raise PipelineError('task_interpretation', '条件列表无效。')
        existing = constraints_for_inputs(store, run_id, prior_user_input_ids)
        by_id = {row['id']: row for row in existing}
        new_rows = []
        row_fields = {'quote', 'key', 'value', 'operation', 'polarity', 'intent', 'scope',
                      'supersedes', 'conflict_with', 'ambiguity'}
        for raw in raw_rows:
            if not isinstance(raw, dict) or set(raw) != row_fields:
                raise PipelineError('task_interpretation', '条件字段不完整或包含未允许的权限字段。')
            _quoted(trusted['text'], raw['quote'])
            if (not isinstance(raw['key'], str) or not re.fullmatch(r'[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+', raw['key'])
                    or raw['key'].split('.')[0] in {'goal', 'approval', 'authorization', 'permission',
                                                   'budget', 'evidence', 'product', 'decision'}):
                raise PipelineError('task_interpretation', '内容条件不能改目标、商品事实、决定或执行权限。')
            if (not isinstance(raw['value'], str) or not raw['value'].strip() or len(raw['value']) > 1000
                    or not isinstance(raw['ambiguity'], str) or len(raw['ambiguity']) > 1000
                    or raw['operation'] not in {'add', 'replace', 'release', 'clarify'}
                    or raw['polarity'] not in {'require', 'forbid', 'prefer'}
                    or raw['intent'] not in {'instruction', 'question', 'hypothesis', 'reported', 'uncertain'}):
                raise PipelineError('task_interpretation', '条件内容、操作或语气无效。')
            scope = raw['scope']
            if (not isinstance(scope, dict) or set(scope) != {'lifetime', 'stages', 'paths', 'quote'}
                    or scope['lifetime'] not in {'turn', 'project'}
                    or not isinstance(scope['stages'], list) or not scope['stages']
                    or any(not isinstance(stage, str) for stage in scope['stages'])
                    or len(set(scope['stages'])) != len(scope['stages'])
                    or any(stage not in STAGES for stage in scope['stages'])
                    or not isinstance(scope['paths'], list)
                    or any(not isinstance(path, list) or not path or any(
                        type(part) not in {str, int} or isinstance(part, str) and not part
                        or isinstance(part, int) and part < 0 for part in path) for path in scope['paths'])):
                raise PipelineError('task_interpretation', '条件范围无效。')
            _quoted(trusted['text'], scope['quote'], optional=scope['lifetime'] == 'turn')
            for name in ('supersedes', 'conflict_with'):
                ids = raw[name]
                if (not isinstance(ids, list) or any(not isinstance(identity, str) or identity not in by_id
                                                     for identity in ids) or len(set(ids)) != len(ids)):
                    raise PipelineError('task_interpretation', '替换或冲突必须指向现存且有效的条件。')
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
        if video_use is not None:
            interpretation['video_use'] = dict(video_use)
        if reuse is not None:
            interpretation['reuse_artifact'] = dict(reuse)
        store.append_event(run_id, 'user_task_interpretation_recorded', source_message_id=trusted['message_id'],
                           interpretation=interpretation)
        return {**interpretation, 'project_constraints': constraints_for_inputs(store, run_id,
                [*prior_user_input_ids, trusted['message_id']])}
