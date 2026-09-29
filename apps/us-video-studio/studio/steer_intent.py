"""What an in-flight message is asking for, decided before any work is stopped.

Every correction sent while a task ran used to take the same path: record it,
set the manager's cancel flag, let the old turn die, then start a successor that
re-read the original request plus the additions. That is right for "change what
you are doing" and wrong for everything else. Asking "how is it going" stopped
the generation it was asking about; a note about shot 2 stopped all six.

This module answers one narrow question before anything is cancelled: **must the
running work stop for this message to be honoured?** The answer is deliberately
conservative. A message that is not recognisably read-only is treated as work
that needs the current turn to end, because that was the previous behaviour and
guessing wrong here costs a restarted turn, not a wrong answer.

It is not a natural-language understanding layer and it does not decide what the
message *means* — the producer agent still reads the actual words and decides
what to do. It only decides whether the local work has to stop first.

Two tiers:

* ``stop`` — the user asked, in their own words, for the work to stop. Treated
  like the explicit stop control: no automatic continuation runs afterwards.
* ``status`` — a question about the current state. Persisted and answered, but
  the running turn keeps its provider task. Safe because a read-only question
  cannot need earlier work abandoned.
* ``work`` — everything else, including every message this module does not
  recognise. The turn ends and the successor handles the request, as before.
"""
from __future__ import annotations

import re

#: Work must stop. Deliberately narrow: only phrases that are unambiguously an
#: instruction to stop, never a question that happens to contain 停.
STOP_PATTERNS = (
    r'^(?:先|马上|立刻|赶紧|快)?停(?:止|下|一?下)?[。.!！~\s]*$',
    r'^(?:先|马上|立刻)?(?:别|不要|不用)(?:再)?(?:做|继续|生成|跑|干)了?[。.!！~\s]*$',
    r'(?:停止|停掉|取消)(?:当前|这个|这次|本轮|整个)?(?:任务|生成|制作|工作)',
    r'^(?:停|停止|停下|停一下|取消|取消吧|算了|别做了|不做了)[吧了。.!！~\s]*$',
)

#: A question about state, not a change to it. Each pattern is anchored on the
#: sentence level so "改一下第二镜，进度我看不到" stays classified as work.
STATUS_PATTERNS = (
    r'^(?:现在)?(?:进度|状态|进展|情况)(?:怎么样|如何|呢|怎样)?[?？。.!！~\s]*$',
    r'(?:做到|进行到|跑到|走到)哪(?:儿|里)?了?[?？。.!！~\s]*$',
    r'^(?:还)?(?:要|得|需要)多久[?？。.!！~\s]*$',
    r'^(?:(?:现在|在|正在)?(?:做|干|忙)(?:什么|啥)|在进行(?:什么|啥))[?？。.!！~\s]*$',
    r'^(?:现在|目前)?(?:什么|啥)(?:情况|状态|进度)[?？。.!！~\s]*$',
    r'^(?:好了|完成|做完|弄完|出来)了?(?:吗|没|没有|么)[?？。.!！~\s]*$',
    r'^(?:怎么|为什么|为啥)(?:还)?(?:没|没有)(?:好|完成|出来|反应|动)[?？。.!！~\s]*$',
    r'(?:查询|查一下|看一下|看看|汇报|报一下)(?:一下)?(?:进度|状态|进展|情况)',
    r'^(?:看|查|读)一下(?:当前|现在)?(?:进度|状态|进展|情况)[?？。.!！~\s]*$',
)

#: Words that mean the message is changing something. Any of these present makes
#: a status reading unsafe, however question-like the sentence looks.
WORK_MARKERS = (
    '改', '换', '调', '重做', '重写', '重新', '修改', '调整', '替换', '删', '加一',
    '生成', '制作', '出图', '出片', '再来', '继续做', '开始', '提交', '批准', '采用',
    '确认', '不要', '别用', '不对', '有问题', '太', '更', '麻烦你',
)

#: Anything longer than this is treated as work. A read-only status question is
#: short; a paragraph is a request.
STATUS_MAX_CHARS = 60


def _normalised(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def classify(text: str) -> dict:
    """Return ``{'class', 'reason'}`` for one in-flight message.

    ``class`` is ``'stop'``, ``'status'`` or ``'work'``. ``reason`` is a short
    phrase for the record, not for the user.

    Deliberately independent of the running job's kind: a question about progress
    is a question whether the work is a chat turn or a media submission, and
    making the answer depend on the kind is how two paths drift apart.
    """
    value = _normalised(text)
    if not value:
        return {'class': 'work', 'reason': '空内容按需要停止处理'}
    for pattern in STOP_PATTERNS:
        if re.search(pattern, value):
            return {'class': 'stop', 'reason': '用户明确要求停止'}
    if len(value) <= STATUS_MAX_CHARS and not any(marker in value for marker in WORK_MARKERS):
        for pattern in STATUS_PATTERNS:
            if re.search(pattern, value):
                return {'class': 'status', 'reason': '只询问当前进度，不需要中断在途工作'}
    return {'class': 'work', 'reason': '按需要结束当前一轮后处理'}


#: Classes that interrupt the running turn. ``None`` means the record predates
#: classification; it was accepted as a correction, so it keeps behaving as one.
#: A field added later must not silently drop a saved request.
STOPPING_CLASSES = ('work', 'stop', None)

#: Classes that may start a successor turn afterwards. A status question is
#: answered by the turn already running; an explicit stop is a stop and must not
#: be recycled into a new request the user did not ask for.
CONTINUING_CLASSES = ('work', None)


def steer_class_of(record: dict) -> str | None:
    """The class of a stored ``job_steer_received`` row.

    The row names the field ``steer_class``; a caller holding a bare
    classification dict names it ``class``. Reading only one of the two is how a
    stop could be mistaken for a request. ``None`` means the row predates
    classification and keeps behaving as a correction.
    """
    if not isinstance(record, dict):
        return None
    if 'steer_class' in record:
        return record.get('steer_class')
    return record.get('class')


def needs_stop(classification: dict) -> bool:
    """Whether the running turn must be interrupted for this message."""
    return steer_class_of(classification) in STOPPING_CLASSES


def continuation_allowed(classification: dict) -> bool:
    """Whether an automatic successor should run after the current turn."""
    return steer_class_of(classification) in CONTINUING_CLASSES
