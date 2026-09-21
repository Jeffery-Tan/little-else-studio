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


def answer(store, run_id, text):
    current = pending_question(store, run_id)
    if current:
        store.append_event(run_id, 'interaction_answered', question_id=current['id'], text=text)
