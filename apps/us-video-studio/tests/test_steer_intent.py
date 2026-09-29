"""An in-flight message is classified before any running work is stopped.

Every correction sent while a task ran used to take one path: record it, cancel
the running turn, then start a successor over the original request plus the
additions. Asking "how is it going" therefore stopped the generation it was
asking about. These tests pin the three outcomes and the ways they can be
bypassed.
"""
from __future__ import annotations

import pytest

from studio.steer_intent import classify, continuation_allowed, needs_stop


@pytest.mark.parametrize('text', [
    '现在进度怎么样？', '进度', '做到哪了', '还要多久', '在做什么',
    '在进行什么', '现在什么情况', '好了吗', '怎么还没好', '查询一下进度',
    '看一下当前状态', '报一下进展',
])
def test_a_progress_question_does_not_stop_the_running_work(text):
    result = classify(text)
    assert result['class'] == 'status', (text, result)
    assert needs_stop(result) is False
    assert continuation_allowed(result) is False


@pytest.mark.parametrize('text', ['停', '停止', '先停一下', '停止当前任务', '取消这次生成', '不做了', '算了'])
def test_an_explicit_stop_is_a_stop(text):
    result = classify(text)
    assert result['class'] == 'stop', (text, result)
    # The turn has to end: that is what stopping means.
    assert needs_stop(result) is True
    # But a stop must never be recycled into a new request the user did not ask for.
    assert continuation_allowed(result) is False


@pytest.mark.parametrize('text', [
    '按这个方向继续出分镜。第二镜不要太像广告，改得自然一点，其他先不动。视频先别生成。',
    '把第二镜换掉', '重新生成第 3 段', '这镜头不自然，改一下', '继续', '再出一版',
    '现在进度怎么样，另外第二镜改一下',
])
def test_anything_that_changes_the_request_still_stops_and_continues(text):
    result = classify(text)
    assert result['class'] == 'work', (text, result)
    assert needs_stop(result) is True
    assert continuation_allowed(result) is True


def test_a_stored_row_is_read_by_its_own_field_name():
    """The row calls it ``steer_class``; a bare dict calls it ``class``.

    Reading only one of the two is exactly how an explicit stop was mistaken for
    a correction and started a successor turn the user never asked for.
    """
    assert needs_stop({'steer_class': 'stop'}) is True
    assert continuation_allowed({'steer_class': 'stop'}) is False
    assert needs_stop({'steer_class': 'status'}) is False
    assert continuation_allowed({'steer_class': 'status'}) is False
    assert continuation_allowed({'steer_class': 'work'}) is True


def test_a_record_without_a_class_keeps_behaving_like_a_correction():
    """A field added later must not silently drop a saved request."""
    assert needs_stop({}) is True
    assert continuation_allowed({}) is True
    assert needs_stop({'class': 'status'}) is False
    assert continuation_allowed({'class': 'status'}) is False


def test_a_long_message_is_never_treated_as_a_status_question():
    """Length is the cheapest signal that this is a request, not a check-in."""
    text = '进度' + '，'.join(['还要确认一些细节'] * 8)
    assert classify(text)['class'] == 'work'


def test_an_unrecognised_message_defaults_to_work_so_nothing_is_silently_dropped():
    """The conservative direction matters: a wrong guess here loses a turn."""
    assert classify('嗯')['class'] == 'work'
    assert classify('那个颜色再想想')['class'] == 'work'
    assert classify('')['class'] == 'work'
