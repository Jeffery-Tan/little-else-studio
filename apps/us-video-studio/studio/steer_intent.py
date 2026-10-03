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

Five classes:

* ``stop`` — the user asked, in their own words, for the work to stop. Treated
  like the explicit stop control: no automatic continuation runs afterwards.
* ``status`` — a question about the current state. Persisted and answered, but
  the running turn keeps its provider task. Safe because a read-only question
  cannot need earlier work abandoned.
* ``discussion`` — explicitly discuss without editing. The running work stays
  active, then the genuine input receives its own read-only successor turn.
* ``deferred`` — the user explicitly says to keep the current work running
  while adding another request. Save it for the next turn without cancellation.
* ``work`` — everything else, including every message this module does not
  recognise. The turn ends and the successor handles the request, as before.
"""
from __future__ import annotations

import re

#: Work must stop. Deliberately narrow: only phrases that are unambiguously an
#: instruction to stop, never a question that happens to contain 停.
STOP_PATTERNS = (
    r'^(?:请|先|马上|立刻|赶紧|快)?(?:暂停|停(?:止|下|一?下)?)(?:一下)?[。.!！~\s]*$',
    r'^(?:这次|本轮)?(?:先|马上|立刻)?(?:别|不要|不用)(?:再)?(?:做|继续|生成|跑|干)了?[。.!！~\s]*$',
    r'^(?:请|先)?(?:停止|暂停|停下|停掉|取消)(?:一下)?(?:当前|这个|这次|本轮|整个|正在执行的|正在进行的|在跑的)?(?:任务|生成|制作|工作)(?:吧)?[。.!！~\s]*$',
    r'^(?:停|停止|停下|停一下|取消|取消吧|算了|别做了|不做了)[吧了。.!！~\s]*$',
)

#: A question about state, not a change to it. Each pattern is anchored on the
#: sentence level so "改一下第二镜，进度我看不到" stays classified as work.
STATUS_PATTERNS = (
    r'^(?:现在)?(?:生成|出图|视频|图片)(?:完|完成|好|出来)?了?(?:吗|没|没有)[?？。.!！~\s]*$',
    r'^(?:现在)?(?:进度|状态|进展|情况)(?:怎么样|如何|呢|怎样)?[?？。.!！~\s]*$',
    r'(?:做到|进行到|跑到|走到)哪(?:儿|里)?了?[?？。.!！~\s]*$',
    r'^(?:还)?(?:要|得|需要)多久[?？。.!！~\s]*$',
    r'^(?:(?:现在|在|正在)?(?:做|干|忙)(?:什么|啥)|在进行(?:什么|啥))[?？。.!！~\s]*$',
    r'^(?:现在|目前)?(?:什么|啥)(?:情况|状态|进度)[?？。.!！~\s]*$',
    r'^(?:好了|完成|做完|弄完|出来)了?(?:吗|没|没有|么)[?？。.!！~\s]*$',
    r'^(?:怎么|为什么|为啥)(?:还)?(?:没|没有)(?:好|完成|出来|反应|动)[?？。.!！~\s]*$',
    r'(?:查询|查一下|看一下|看看|汇报|报一下)(?:一下)?(?:进度|状态|进展|情况)',
    r'^(?:看|查|读)一下(?:当前|现在)?(?:进度|状态|进展|情况)[?？。.!！~\s]*$',
    r'^(?:你|你们|它|服务|任务)?(?:现在|目前)?(?:还|仍然)?(?:在|正在|有在)?(?:做|跑|执行|处理|工作|生成)(?:着)?(?:吗|么|没|没有|呢)[?？。.!！~\s]*$',
    r'^(?:是不是|是否|难道)?(?:现在|任务|服务|工作)?(?:又|已经|一直|还)?(?:卡住|卡死|停住|没动静|没反应)了?(?:吗|么|呢)?[?？。.!！~\s]*$',
    r'^(?:怎么|为什么|为啥)(?:还|会|要|需要)?(?:这么|那么)?(?:久|慢)(?:啊|呀|呢|了)?[?？。.!！~\s]*$',
)

def _normalised(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def classify(text: str) -> dict:
    """Return ``{'class', 'reason'}`` for one in-flight message.

    ``class`` is ``'stop'``, ``'status'``, ``'discussion'``, ``'deferred'`` or ``'work'``. ``reason`` is a short
    phrase for the record, not for the user.

    Deliberately independent of the running job's kind: a question about progress
    is a question whether the work is a chat turn or a media submission, and
    making the answer depend on the kind is how two paths drift apart.
    """
    value = _normalised(text)
    if not value:
        return {'class': 'work', 'reason': '空内容按需要停止处理'}
    clauses = [part.strip() for part in re.split(r'[，,。.!！?？；;]+', value) if part.strip()]
    wait = r'^(?:(?:先)?(?:等我|等)(?:确认|通知|决定|回复)(?:再(?:做|继续|生成))?|(?:先)?(?:让我|我先)(?:看下|看一下|看看)(?:当前|现在)?(?:结果|进展|内容)|(?:不要|不用|别)(?:再|自动)?接续)$'
    stop = lambda part: any(re.fullmatch(pattern, part) for pattern in STOP_PATTERNS)
    # Interpret the full instruction, including negation and wait clauses. A
    # prohibition on future video generation inside a writing request is not a
    # command to cancel all current work.
    if clauses and all(stop(part) or re.fullmatch(wait, part) for part in clauses):
        return {'class': 'stop', 'reason': '用户明确要求停止或等待确认'}
    keep_running = r'^(?:请|先)?(?:不要|别|不用|不必)(?:再)?(?:停止|停下|暂停|停|取消)(?:(?:当前|这个|这次|本轮|正在执行的|正在进行的)?(?:任务|生成|制作|工作))?(?:了)?$'
    def status(part):
        part = re.sub(r'^(?:我)?(?:只是|只想)(?:想)?(?:(?:问|询问)(?:一下)?|看(?:一下)?)?', '', part)
        part = re.sub(r'^(?:请你|麻烦你|帮我|你|请)', '', part)
        if re.fullmatch(keep_running, part) or any(re.fullmatch(pattern, part) for pattern in STATUS_PATTERNS):
            return True
        if re.fullmatch(r'(?:看下|看一下|看看|查下|查一下|查询|汇报)(?:目前|当前|现在)?(?:的)?(?:进度|状态|进展|情况)', part):
            return True
        if re.fullmatch(r'(?:好像|似乎|感觉)?(?:现在|目前|任务|工作|服务)?(?:又|还|一直)?(?:卡住|卡死|停住|没动静|没反应)(?:了)?(?:一样)?', part):
            return True
        # Interrogative state clauses are read-only even when their wording is
        # absent from the examples above. An action/replacement clause keeps a
        # mixed message out of this branch; every clause must pass separately.
        question = re.search(r'吗|么|呢|是不是|是否|怎么|为什么|为啥|怎样|如何|多久|哪里|哪一步|谁', part)
        state = re.search(r'进度|状态|任务|生成|出图|出片|执行|工作|卡住|卡死|慢|多久|跑|进行|等待|服务|模型', part)
        action = re.search(r'改|调整|替换|重做|重新|添加|增加|删除|换成|换掉|停止|取消|暂停', part)
        return bool(question and state and not action)
    if clauses and all(status(part) for part in clauses):
        return {'class': 'status', 'reason': '只询问当前进度或要求保持执行，不中断在途工作'}
    if any(re.fullmatch(keep_running, part) for part in clauses):
        return {'class': 'deferred', 'reason': '用户明确要求保持执行，补充原话在当前任务结束后处理'}
    if re.match(r'^(?:先)?只(?:讨论|聊)', value) and re.search(r'(?:不要|不)(?:改|写|做|生成)', value):
        return {'class': 'discussion', 'reason': '只讨论，等待原任务结束后单独回答'}
    return {'class': 'work', 'reason': '按需要结束当前一轮后处理'}


#: Classes that interrupt the running turn. ``None`` means the record predates
#: classification; it was accepted as a correction, so it keeps behaving as one.
#: A field added later must not silently drop a saved request.
STOPPING_CLASSES = ('work', 'stop', None)

#: Classes that may start a successor turn afterwards. A status question is
#: answered from the saved production projection; an explicit stop is a stop and must not
#: be recycled into a new request the user did not ask for.
CONTINUING_CLASSES = ('work', 'discussion', 'deferred', None)


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
