"""Conversation questions, projected from the existing append-only run journal.

These are preferences/clarifications, never artifact or media approvals.
"""
from __future__ import annotations

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


def record_answer(store, run_id, receipt, *, question_id, quote):
    """Ground a model's semantic answer proposal in the executing real input."""
    from .conversation_input import message, interpretation_for
    if not receipt or not isinstance(question_id, str) or not question_id:
        raise PipelineError('question_answer', '内部接续不能创造用户回答。')
    with store.run_lock(run_id):
        trusted = message(store, run_id, receipt['message_id'])
        if not any(e['kind'] == 'user_message_execution_started'
                   and e.get('message_id') == trusted['message_id'] for e in store.read_events(run_id)):
            raise PipelineError('question_answer', '未执行的排队消息不能作为当前回答。')
        offered = trusted.get('pending_question_id') or ((trusted.get('displayed') or {}).get('interaction') or {}).get('id')
        if question_id not in {offered, trusted.get('question_id')}:
            raise PipelineError('question_changed', '这条消息没有绑定该问题，不能回答后来出现的问题。')
        if not isinstance(quote, str) or not quote.strip() or quote not in trusted['text']:
            raise PipelineError('question_answer', '回答必须逐字来自本条真实用户原话。')
        from .steer_intent import classify
        interpretation = interpretation_for(store, run_id, trusted['message_id'])
        mode = (interpretation or {}).get('task', {}).get('mode')
        if (classify(trusted['text'])['class'] in {'stop', 'status', 'discussion'}
                or mode in {'stop', 'status', 'discuss', 'clarify'}):
            raise PipelineError('question_answer', '只查询或停止的消息不能作为问题回答。')
        return answer(store, run_id, quote, question_id=question_id,
                      source_message_id=trusted['message_id'])


def question_context(store, run_id):
    import json
    return ('\n# 当前待答问题\n' + json.dumps(pending_question(store, run_id), ensure_ascii=False)
            + '\n只有本条原话确实回答该问题时才 record_question_answer，quote逐字绑定回答部分。'
              '自由文本可以回答，不需要重复题目；查询、停止、暂不选择或无关反馈不作回答。'
              '问题回答只记录偏好，不等于采用作品、图审或费用授权。')
