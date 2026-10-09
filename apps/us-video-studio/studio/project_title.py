"""Rule-only short project titles from a creator prompt. No model calls."""
from __future__ import annotations

import re
from datetime import date

_FILLER = (
    "帮我", "请帮我", "请你", "麻烦", "我想", "我想要", "我要", "需要",
    "做一个", "做一条", "做一个", "拍一条", "拍个", "来一条", "来个",
    "出一条", "做下", "做点", "生成", "写一条", "写个", "弄一条", "弄个",
    "给这个", "给那", "给", "这个", "那个", "一条", "一款", "一个",
    "能不能", "可以", "一下", "谢谢",
)
_FORMS = (
    "带货视频", "带货短视频", "短视频", "竖屏视频", "竖屏短片",
    "TikTok视频", "tiktok视频", "口播视频", "口播", "分镜视频",
    "图文", "图文带货", "带货图文", "图片文案", "成片", "宣传片",
    "开箱视频", "种草视频", "vlog", "Vlog", "视频", "短片", "图文视频",
)
_VERY_LIKE = re.compile(r"^(?:的|了|和|与|及|或|在|是|要|把|被|从|对|向|给|用|做|拍|写|来|想|要)+")
_VERB_BREAK = re.compile(r"(?:做|拍|写|出|弄|生成|想要|需要|希望|变成|改成|来一|一条)")
_CLAUSE_SPLIT = re.compile(r"[。！？!?\n]")
_COMMA_SPLIT = re.compile(r"[，,；;：:]")
_ATTACHMENT_HEAD = re.compile(
    r"^(?:请\s*)?(?:(?:使用|根据|基于|参考|实际查看|查看|分析|用|看)\s*)?"
    r"(?:我(?:已|刚刚|刚)?\s*)?(?:附上(?:了)?|上传(?:了)?|附件|随附)"
    r"(?:里面(?:的)?|里(?:的)?|中(?:的)?|的)\s*")
_ATTACHMENT_ONLY = re.compile(
    r"^(?:请\s*)?(?:(?:使用|根据|基于|参考|实际查看|查看|分析|用|看)\s*)?"
    r"(?:我(?:已|刚刚|刚)?\s*)?(?:附上(?:了)?|上传(?:了)?|附件|随附)$")
_SOURCE_LABEL = re.compile(r"(?:的)?(?:商品图|产品图|参考图|(?:商品|产品|参考)?(?:图片|照片|素材|视频))$")
_MAX = 16
_MIN = 2


def _clip_theme(theme: str) -> str:
    """Keep the noun-ish head: stop before verbs or a trailing description."""
    theme = _VERY_LIKE.sub("", theme).strip("的了和与及或，, ")
    verb = _VERB_BREAK.search(theme)
    if verb:
        theme = theme[:verb.start()]
    head, sep, _rest = theme.partition("的")
    if sep and len(head) >= _MIN:
        theme = head
    theme = re.sub(r"^(?:一条|一款|一个|一张|一种|自然|一点)", "", theme)
    theme = _VERY_LIKE.sub("", theme).strip("的了和与及或 ")
    return theme



def _strip_filler(text: str) -> str:
    leftover = text.strip()
    changed = True
    while changed and leftover:
        changed = False
        for word in _FILLER:
            if leftover.startswith(word) and len(leftover) > len(word):
                leftover = leftover[len(word):].lstrip()
                changed = True
                break
    return leftover


def _first_clause(text: str) -> str:
    line = _CLAUSE_SPLIT.split(text.strip())[0].strip()
    # Keep the part before a trailing requirement comma when the head already
    # has a theme or form; the tail is usually style/length constraints.
    head = _COMMA_SPLIT.split(line)[0].strip()
    return head or line


def _attachment_subject(text: str) -> tuple[str, bool]:
    """Remove an explicit leading source reference, keeping its named topic."""
    scan = _strip_filler(text)
    matched = _ATTACHMENT_HEAD.match(scan)
    if matched:
        subject = scan[matched.end():].strip()
        return ('' if subject == '图' else _SOURCE_LABEL.sub('', subject).strip()), True
    if _ATTACHMENT_ONLY.fullmatch(scan):
        return '', True
    return text, False


def _find_form(text: str) -> str | None:
    lower = text.lower()
    for form in _FORMS:
        if form.lower() in lower:
            # Preserve original casing for latin tokens, else the table form.
            idx = lower.find(form.lower())
            return text[idx:idx + len(form)] if form.isascii() else form
    return None


def _find_theme(text: str, form: str | None) -> str | None:
    # Drop the form segment so the theme is not "短视频短视频".
    scan = text
    if form:
        idx = scan.lower().find(form.lower())
        if idx >= 0:
            scan = scan[:idx] + scan[idx + len(form):]
    scan = scan.strip()

    patterns = (
        r"这个([\u4e00-\u9fffA-Za-z0-9]{1,12})",
        r"那(?:个|条|款|张)?([\u4e00-\u9fffA-Za-z0-9]{1,12})",
        r"给([\u4e00-\u9fffA-Za-z0-9]{2,12})(?:做|拍|写|出|来|弄|生成)",
        r"关于([\u4e00-\u9fffA-Za-z0-9]{1,12})",
        r"商品([\u4e00-\u9fffA-Za-z0-9]{1,12})",
        r"([\u4e00-\u9fffA-Za-z0-9]{2,8})(?:的)?(?:短视频|视频|图文|口播|带货|短片)",
    )
    for pattern in patterns:
        match = re.search(pattern, scan)
        if match:
            theme = _clip_theme(match.group(1))
            if len(theme) >= _MIN:
                return theme[:10]

    # Leading noun-ish chunk before a verb, after filler strip.
    cleaned = _strip_filler(scan)
    lead = re.split(r"(?:做|拍|写|出|来|弄|生成|想要|需要|希望)", cleaned, maxsplit=1)[0]
    theme = _clip_theme(lead)
    if _MIN <= len(theme) <= 10 and theme not in _FORMS:
        return theme
    return None


def derive_project_title(text: str) -> str:
    """Turn a free-form prompt into a short, human project name."""
    # URLs identify sources, not the product/theme. Keep the original brief
    # untouched; only the new title's scan excludes them.
    raw = re.sub(r"https?://[^\s<>，。；！？「」“”\"']+", ' ', text or '', flags=re.I).strip()
    if not raw:
        return f"作品{date.today().strftime('%m%d')}"

    head, attachment_reference = _attachment_subject(_first_clause(raw))
    # Later clauses describe constraints or sources. They must not turn a
    # neutral theme into a video title merely because videos were prohibited.
    form = _find_form(head)
    # A source-only head provides no product identity. In particular, don't
    # scan later restrictions to invent one after stripping an attachment.
    theme = _find_theme(head, form) or (None if attachment_reference else _find_theme(raw, form))

    if theme and form and theme not in form and form not in theme:
        title = f"{theme}{form}"
    elif theme:
        title = theme
    elif form:
        title = form
    else:
        title = _strip_filler(head)[:_MAX]

    title = re.sub(r"\s+", "", title).strip("的了和与及或，,。.！!？?；;：:")
    if not title:
        title = f"作品{date.today().strftime('%m%d')}"
    if len(title) > _MAX:
        title = title[:_MAX - 1] + "…"
    return title
