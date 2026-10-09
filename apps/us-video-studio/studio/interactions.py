"""Conversation questions, projected from the existing append-only run journal.

These are preferences/clarifications, never artifact or media approvals.
"""
from __future__ import annotations

import json
import uuid
from pydantic import BaseModel, Field
from .models import PipelineError


class Choice(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(default='', max_length=240)


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=240)
    context: str = Field(default='', max_length=600)
    options: list[Choice] = Field(min_length=2, max_length=4)
    recommended: int = Field(default=0, ge=0, le=3)


def pending_question(store, run_id):
    for event in reversed(store.read_events(run_id)):
        if event['kind'] == 'interaction_answered':
            return None
        if event['kind'] == 'interaction_requested':
            return event['question']
    return None


def ask(store, run_id, args):
    question = Question.model_validate(args)
    if question.recommended >= len(question.options):
        raise ValueError('推荐项必须对应一个实际选项。')
    data = {**question.model_dump(), 'id': uuid.uuid4().hex}
    data['message'] = '\n\n'.join(filter(None, [question.question, question.context,
        '\n'.join(f"{i + 1}. {choice.label}{'（推荐）' if i == question.recommended else ''}：{choice.description}"
                  for i, choice in enumerate(question.options))]))
    store.append_chat(run_id, 'assistant', data['message'])
    store.append_event(run_id, 'interaction_requested', question=data)
    return data


def check_answer(store, run_id, question_id):
    current = pending_question(store, run_id)
    if not current or current['id'] != question_id:
        raise PipelineError('question_changed', '这个问题已经更新，请查看当前问题后再回答。')


def answer(store, run_id, text, *, question_id=None, source_message_id=None):
    """Settle only an explicitly identified question, never the latest message."""
    if not question_id:
        return None
    with store.run_lock(run_id):
        prior = next((e for e in store.read_events(run_id)
                      if e['kind'] == 'interaction_answered' and e.get('question_id') == question_id
                      and source_message_id and e.get('source_message_id') == source_message_id), None)
        if prior:
            if prior['text'] != text:
                raise PipelineError('question_changed', '同一条消息的回答已经保存，不能改写。')
            return prior
        check_answer(store, run_id, question_id)
        return store.append_event(run_id, 'interaction_answered', question_id=question_id,
                                  text=text, source_message_id=source_message_id)


def answer_from_receipt(store, run_id, receipt):
    if not receipt or not receipt.get('question_id'):
        return None
    return answer(store, run_id, receipt['text'], question_id=receipt['question_id'],
                  source_message_id=receipt['message_id'])


def _complete_answer_quote(text, quote):
    """Keep the whole response sentence, including its negation/conditions.

    This is a source-span check, not an answer classifier. Meaning and whether
    an answer remains uncertain are explicit proposals; never inferred here.
    """
    value = quote.strip().rstrip('。.!！?？；;').strip()
    quoted, start, sentences = [], 0, []
    pairs = {'「': '」', '“': '”', '"': '"', "'": "'"}
    for index, char in enumerate(text):
        if quoted and char == quoted[-1]:
            quoted.pop()
        elif char in pairs:
            quoted.append(pairs[char])
        elif char in '。.!！?？；;\n' and not quoted:
            sentences.append(text[start:index].strip())
            start = index + 1
    sentences.append(text[start:].strip())
    # Multiple complete consecutive sentences are allowed too. The quoted
    # target may itself contain punctuation, so do not split inside quotes.
    offset = text.find(quote)
    before = text[:offset].rstrip() if offset >= 0 else ''
    after = text[offset + len(quote):].lstrip() if offset >= 0 else ''
    return bool(value and (value in sentences or (
        offset >= 0 and (not before or before[-1] in '。.!！?？；;\n')
        and (not after or after[0] in '。.!！?？；;\n'))))


def _artifact_question(store, run_id, receipt, question_id, target):
    from .models import STAGES
    from .structured import artifact_question_id, is_ask_user_question
    if (not isinstance(target, dict) or set(target) != {'stage', 'version', 'artifact_sha256'}
            or target['stage'] not in STAGES or type(target['version']) is not int
            or target['version'] < 1 or not isinstance(target['artifact_sha256'], str)):
        raise PipelineError('question_changed', '产物问题需要准确 stage/version/hash。')
    offered = [row for row in (receipt.get('displayed') or {}).get('questions', [])
               if isinstance(row, dict) and row.get('ask_user')
               and all(row.get(key) == value for key, value in target.items())
               and artifact_question_id(target['stage'], target['version'], target['artifact_sha256'],
                                        row.get('question')) == question_id]
    if len(offered) != 1:
        raise PipelineError('question_changed', '这条消息没有展示该准确产物问题，不能替换问题或版本。')
    artifact = store.read_artifact(run_id, target['stage'], target['version'])
    if artifact.sha256 != target['artifact_sha256'] or not store.verify_artifact(
            run_id, target['stage'], target['version']):
        raise PipelineError('question_changed', '产物问题的指纹无法核对，请查看准确版本。')
    question = offered[0]['question']
    rows = [row for row in artifact.structured.get('open_questions', [])
            if is_ask_user_question(row) and row['question'] == question]
    # Durable product questions may be displayed again after timing=later.
    from .structured import question_kind
    if not rows:
        rows = [row for row in artifact.structured.get('open_questions', [])
                if isinstance(row, dict) and row.get('question') == question
                and row.get('owner') == 'user' and row.get('reason')
                and question_kind(row) == 'product_choice']
    if len(rows) != 1:
        raise PipelineError('question_changed', '这不是准确产物中可回答的用户问题。')
    return question


def artifact_responses(store, run_id):
    """The shared read projection; original artifacts/chat are never rewritten."""
    events = store.read_events(run_id)
    rows = [event for event in events if event['kind'] == 'artifact_question_response_recorded']
    if not rows:
        return []
    store.require_healthy(run_id)
    from .conversation_input import message
    for row in rows:
        receipt = message(store, run_id, row.get('source_message_id'))
        if (row.get('source_request_id') != receipt['request_id']
                or row.get('display_context_id') != receipt.get('display_context_id')
                or row.get('answer_status') not in {'answered', 'unknown', 'needs_clarification'}
                or not isinstance(row.get('quote'), str) or row['quote'] not in receipt['text']
                or not _complete_answer_quote(receipt['text'], row['quote'])
                or not any(e['kind'] == 'user_message_execution_started'
                           and e.get('message_id') == receipt['message_id'] and e['seq'] < row['seq']
                           for e in events)
                or _artifact_question(store, run_id, receipt, row.get('question_id'), row.get('target'))
                   != row.get('question')):
            raise PipelineError('question_answer', '已保存的问题回答来源无法核对，不能当作已回答。')
    return rows


def record_answer(store, run_id, receipt, *, question_id, quote,
                  artifact_target=None, answer_status=None):
    """Ground a model's semantic answer proposal in the executing real input."""
    from .conversation_input import message, interpretation_for
    if not receipt or not isinstance(question_id, str) or not question_id:
        raise PipelineError('question_answer', '内部接续不能创造用户回答。')
    with store.run_lock(run_id):
        store.require_healthy(run_id)
        trusted = message(store, run_id, receipt['message_id'])
        if trusted['text'] != receipt.get('text'):
            raise PipelineError('question_answer', '回答不能替换真实用户原话。')
        if not any(e['kind'] == 'user_message_execution_started'
                   and e.get('message_id') == trusted['message_id'] for e in store.read_events(run_id)):
            raise PipelineError('question_answer', '未执行的排队消息不能作为当前回答。')
        if not isinstance(quote, str) or not quote.strip() or quote not in trusted['text']:
            raise PipelineError('question_answer', '回答必须逐字来自本条真实用户原话。')
        from .steer_intent import classify
        interpretation = interpretation_for(store, run_id, trusted['message_id'])
        mode = (interpretation or {}).get('task', {}).get('mode')
        if (classify(trusted['text'])['class'] in {'stop', 'status', 'discussion'}
                or mode in {'stop', 'status', 'discuss', 'clarify'}):
            raise PipelineError('question_answer', '只查询或停止的消息不能作为问题回答。')
        if artifact_target is not None:
            if answer_status not in {'answered', 'unknown', 'needs_clarification'}:
                raise PipelineError('question_answer', '回答状态只能是已回应、事实未知或待澄清。')
            if not _complete_answer_quote(trusted['text'], quote):
                raise PipelineError('question_answer', '须引用完整连续回答，不能截掉否定、条件、转述或疑问。')
            question = _artifact_question(store, run_id, trusted, question_id, artifact_target)
            prior = next((event for event in store.read_events(run_id)
                          if event['kind'] == 'artifact_question_response_recorded'
                          and event.get('source_message_id') == trusted['message_id']
                          and event.get('question_id') == question_id), None)
            if prior:
                if (prior['quote'] != quote or prior['answer_status'] != answer_status
                        or prior['target'] != artifact_target):
                    raise PipelineError('question_answer', '同一条消息的问题回答已保存，不能改写。')
                return prior
            from .pipeline import Pipeline
            current = Pipeline(store).stage_state(run_id, artifact_target['stage'])
            if (not current.latest or current.latest.version != artifact_target['version']
                    or current.latest.sha256 != artifact_target['artifact_sha256']
                    or current.stale or not current.integrity_ok):
                # Existing durable product choices survive later drafts; bind
                # their still-offered original identity, never a replacement.
                from .project_brief import durable_questions
                durable = durable_questions(store, run_id, chat=store.read_chat(run_id))
                if not any(row.get('question_id') == question_id for row in durable):
                    raise PipelineError('question_changed', '产物问题已变更或失效，请查看当前准确版本后回答。')
            return store.append_event(run_id, 'artifact_question_response_recorded',
                question_id=question_id, target=dict(artifact_target), question=question,
                quote=quote, answer_status=answer_status,
                source_message_id=trusted['message_id'], source_request_id=trusted['request_id'],
                display_context_id=trusted.get('display_context_id'))
        if answer_status not in {None, 'answered'}:
            raise PipelineError('question_answer', '旧对话问题入口不接受产物回答状态。')
        offered = trusted.get('pending_question_id') or ((trusted.get('displayed') or {}).get('interaction') or {}).get('id')
        if question_id not in {offered, trusted.get('question_id')}:
            raise PipelineError('question_changed', '这条消息没有绑定该问题，不能回答后来出现的问题。')
        prior = next((event for event in store.read_events(run_id)
                      if event['kind'] == 'interaction_answered'
                      and event.get('source_message_id') == trusted['message_id']
                      and event.get('question_id') == question_id), None)
        if prior and prior['text'] == trusted['text']:
            return prior
        return answer(store, run_id, quote, question_id=question_id,
                      source_message_id=trusted['message_id'])


def question_context(store, run_id, receipt=None):
    return ('\n# 当前待答问题\n' + json.dumps(pending_question(store, run_id), ensure_ascii=False)
            + '\n只有本条原话确实回答该问题时才 record_question_answer，quote逐字绑定回答部分。'
              '自由文本可以回答，不需要重复题目；查询、停止、暂不选择或无关反馈不作回答。'
              '问题回答只记录偏好，不等于采用作品、图审或费用授权。'
            + '\n本条真实消息当时展示的产物问题：' + json.dumps(
                (receipt.get('displayed') or {}).get('questions', []) if receipt else [], ensure_ascii=False)
            + '\n自然原话同时采用并回答产物问题时，逐一用 record_question_answer 绑定服务端 question_id '
              '及 artifact_target(stage/version/artifact_sha256)，quote引用完整连续回答句，保留否定、条件及疑问。'
              'answer_status=unknown表示用户已回应但事实仍未知，不能改成确认事实；歧义用needs_clarification。'
              '同轮采用前后均可记录这些回答，不创建另一决定，不改范围或费用；内部接续无新receipt不能回答。')
