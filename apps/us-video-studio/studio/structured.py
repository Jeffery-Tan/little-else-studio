"""The structure a document has, beyond being prose.

Why this exists: the artifacts were always structured documents that the screen
flattened into a Markdown blob. ``direction-v2`` carries three options across
creative attributes, a dependency matrix and seven open questions — all of it
present, none of it visible without opening a reader and scrolling. The research
note on MoneyPrinterTurbo describes the same failure from the other end: its
backend records ``failure = {"failed_stage": ..., "error": ...}`` and its frontend
never reads ``failed_stage``. Data present, interface discards it.

This is **not** a new mechanism. ``evidence`` and ``limitations`` have been
model-authored ``list[str]`` on every artifact since the first run; this extends
that arrangement to fields whose shape depends on the stage.

Two rules, and they are the whole point of the module:

* **Structure is optional and never load-bearing.** A stage that cannot produce
  it still succeeds. ``normalize`` drops what it cannot understand and returns
  what is left, including ``{}``. Nothing here raises. A malformed payload must
  cost a *rendering*, never an attempt the director already paid for.
* **Degrade visibly, not silently.** Every dropped item is counted and reported
  back, so the pipeline can record how often the model fails to produce the
  shape it was asked for. Silent coercion would make a broken prompt look like a
  working one.

The shapes mirror what ``agents/roles.py`` already instructs each role to write.
They are not invented here — the director was always asked for 主张 / 给谁看 /
前3秒钩子 / 放弃什么 / 证据强度 / 执行难度 / 素材依赖, and the writer for
时码 / 画面 / 台词 / 声音 / 时长. This module only gives those existing
requirements a type so the screen can lay them out.

It lives in Python rather than JavaScript for the same reason ``vocabulary.py``
does: one copy, testable, and the same on the live stream as on reload.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Longest a single prose cell may be before it is truncated. Generous — this is
#: a guard against a runaway payload, not an editorial limit.
MAX_CELL = 4000
#: Most rows kept per list field. A model that emits 400 shots has misunderstood
#: something, and the sidecar should not grow to match.
MAX_ROWS = 200
QUESTION_COLUMNS = ("question", "blocks", "owner", "timing", "reason", "kind")
#: How a user-facing question must be answered. ``brief_amendment`` is never
#: an ask_user blank — it means the brief itself has to change (see amend_brief).
QUESTION_KINDS = ("fact", "product_choice", "local_design", "brief_amendment")

#: Kinds whose answer is a property of the *product*, not of one draft. A
#: `product_choice` ("which colourway, which size") stays open until it is
#: answered, even across a new direction version — its author has no reason to
#: repeat a question about the user's own SKU, and the user found it vanishing
#: the moment they adopted a version (observed 2026-09-29). `local_design` and
#: `fact` belong to the draft that raised them and go when it is superseded.
DURABLE_QUESTION_KINDS = ("product_choice",)


@dataclass(frozen=True)
class Field:
    """One key of a stage's payload.

    ``kind`` is one of:

    * ``"text"`` — a single string.
    * ``"texts"`` — a list of strings.
    * ``"rows"`` — a list of objects, each keeping exactly ``columns``.
    """

    name: str
    kind: str
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Shape:
    stage: str
    fields: tuple[Field, ...]

    def field(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)


# --------------------------------------------------------------------------- #
# The shapes
# --------------------------------------------------------------------------- #

#: What each stage's structure looks like, keyed by stage name.
#:
#: ``open_questions`` is on every stage and is the one field the interface leans
#: on hardest: it is what turns "the document says it could not decide X" into a
#: question put to the person who can. It was always being written — intake
#: lists four blanks in ``limitations`` and direction lists seven at the end —
#: and was never once collected into something addressed to him.
SHAPES: dict[str, Shape] = {
    "intake": Shape("intake", (
        # §3 采购事实表: what is already true and sourced.
        Field("facts", "rows", ("label", "value", "source")),
        # §4 硬约束: rules that must survive every downstream stage.
        Field("constraints", "texts"),
        Field("open_questions", "rows", QUESTION_COLUMNS),
    )),
    "direction": Shape("direction", (
        # A/B/C × the existing attributes, with an optional short creative title.
        # satisfies / violates / amend_* bind each option to ActiveBrief clause
        # ids (see active_brief.py). Cells are comma-separated ids so the
        # normaliser's string cells stay the only wire type.
        Field("options", "rows", (
            "key", "title", "claim", "audience_moment", "hook", "story",
            "gives_up", "evidence_strength", "difficulty", "asset_needs",
            "satisfies", "violates", "amend_clause", "amend_proposal",
        )),
        Field("recommended", "text"),
        Field("recommended_because", "text"),
        # The two premises the recommendation rests on. Kept apart from the
        # reason because they are what has to be checked first, and a reader
        # skimming the reason will not find them.
        Field("prerequisites", "texts"),
        # Which current execution is affected by a condition; not a verdict on
        # the whole creative direction or a substitute for asking the user.
        Field("dependency_matrix", "rows", ("need", "options")),
        Field("open_questions", "rows", QUESTION_COLUMNS),
    )),
    "material_plan": Shape("material_plan", (
        # One row per shot, carrying its availability. `status` is one of the
        # five the role is told to use — see `MATERIAL_STATUSES`. `file` is a
        # filename in `runs/<id>/materials/`; it is the one field the role
        # cannot invent, which is why it has a read tool (`list_materials`) and
        # a visible failure when the name matches nothing.
        Field("items", "rows", ("shot", "purpose", "status", "note", "file")),
        Field("gaps", "rows", ("gap", "consequence", "blocking")),
        Field("open_questions", "rows", QUESTION_COLUMNS),
    )),
    "writing": Shape("writing", (
        # One row per timed shot. `duration` is an assumption, and the role is
        # told to say so; it stays a string so "约 3 秒" survives as written.
        # `file` names the material this shot would use, when one exists.
        Field("segments", "rows", ("tc", "picture", "line", "sound", "duration", "file")),
        Field("total_duration", "text"),
        Field("caption", "text"),
        Field("assumptions", "texts"),
        Field("open_questions", "rows", QUESTION_COLUMNS),
    )),
}

#: The five states a material row may be in. Not enforced — an unrecognised
#: status is kept verbatim and rendered as-is, because silently reassigning it
#: would be more misleading than showing what the model actually wrote.
MATERIAL_STATUSES: tuple[str, ...] = (
    "已有", "需要拍", "需要生成", "无法获得", "不确定",
)

#: Statuses that mean "this shot cannot be made". The interface groups these
#: separately; a plan whose unfilmable shots are scattered through it reads as
#: more complete than it is.
MATERIAL_BLOCKED: tuple[str, ...] = ("无法获得", "不确定")


# --------------------------------------------------------------------------- #
# How each shape is explained to the role that must produce it
# --------------------------------------------------------------------------- #

#: Kept beside the shapes so the two cannot drift — a test asserts every stage
#: has one. Written as instructions to a person rather than as a field list,
#: because the model reads this once and has to act on it.
BRIEF: dict[str, str] = {
    "intake": """\
- `facts`：已经确认的事实，一条一行。`label` 是什么事实，`value` 是内容，
  `source` 是你从哪看到的（「Jeffery 本轮原话」「上文 intake」「我算的」）。
  **没有出处的不要写进来。**
- `constraints`：硬约束，一条一条。下游每个阶段都要遵守的规矩。
- `open_questions`：你没能确认、只能他来定的事。""",

    "direction": """\
- `options`：一个方案一行。`key` 用 A/B/C。`title` 可选，写能区分方案的短创意名称，
  不要用 A/B/C 作标题，也不要在标题里编造未证实的商品效果。`claim` 主张一句话。
  `audience_moment` 给谁看、在什么场景下被看到。`hook` 用一句话写开场识别点。
  `story` 用两三句话（约 100–150 字）概括核心动作或页面推进、商品作用和结尾回报。
  这里只产出供选择的方向，不写时间段、逐镜头/逐页方案、台词或字幕原文；选定方向后才做素材计划和完整脚本。
  `gives_up` 是创作方案的取舍，不要求角色必须放弃物品或承担代价。`evidence_strength` 证据强度。
  `difficulty` 执行难度。`asset_needs` 素材依赖。
  `satisfies`：本方案满足的有效简报条款 id，逗号分隔（如 `goal.primary,not.abc12345`）。
  `violates`：未修订即不满足的条款 id；有违反就不要把本方案写成推荐。
  `amend_clause` / `amend_proposal`：若要推荐本方案必须先修订哪条简报、怎么改；
  不需要修订则留空。不要只在正文里承认「做不到种草」却空着这些字段。
- `recommended` / `recommended_because`：你推荐哪个，为什么。
- `prerequisites`：推荐时依赖的已知事实或待核实假设，一条一行；待核实不等于不成立。
  若假设不成立，先考虑换镜头、换素材或调整表达，再重新比较方向。不能把缺少一种拍法
  直接判为整个创意方向不可做；无证据的商品效果也不能当作已证实事实来表达。
- `dependency_matrix`：一个待核实条件一行，`options` 写成
  `{"A": true, "B": false, "C": true}`，true 仅表示该方向的当前拍法会受到影响，
  不表示方向已失败或用户必须先补齐该素材。团队能核实或改拍的先自行处理；
  只有用户独有的事实或创意取舍才写入 `open_questions`，注明 owner=user、timing=now、reason。
  提问时允许用户回答「没有」「不确定」，再据此调整方案。""",

    "material_plan": """\
- `items`：一个镜头或一段一行。`shot` 是什么画面，`purpose` 它证明或传达什么，
  `status` 五选一（已有 / 需要拍 / 需要生成 / 无法获得 / 不确定），`note` 补充。
  **不确定就写不确定，不要往「已有」上靠。**
  `file`：**这个镜头用 `materials/` 里的哪个文件**，必须先调 `list_materials`
  看有什么，然后**逐字照抄**文件名。没有对应素材就别填，在 `note` 里写缺什么。
  编一个文件名会在界面上显示成「文件不在」，比空着更糟。
 - `gaps`：`gap` 缺什么，`consequence` 会导致什么后果，`blocking` 写「阻塞」或「可后补」。
   若核心卖点或必要画面没有可信的拍摄、生成或替代办法，标「阻塞」；先调整素材方案，
   不让编剧在这个缺口上写完整脚本。可在脚本后解决的细节标「可后补」。""",

    "writing": """\
- `segments`：一段一行。`tc` 时码，`picture` 画面（拍什么/显示什么），
  `line` 台词或字幕原文，`sound` 声音，`duration` 时长。
  `duration` 是假设就写成假设的样子（「约 3 秒」）。
  `file`：这一段用 `materials/` 里的哪个文件，**先调 `list_materials` 再照抄
  文件名**；没有就先别填——这一段还没到能配画面的地步。
  `tc` 请写成 `0:00–0:03` 这样的区间，界面按它画时间条的长度。
- `total_duration`：总时长，要和各段对得上。
- `caption`：可直接用于美国平台发布的英文文案；只写稿件支持的商品事实，缺少依据时留空。
- `assumptions`：你在时长、可执行性上做的假设，一条一行。""",
}


def brief(stage: str) -> str:
    """The structure instruction appended to the system prompt of ``stage``'s role.

    Returns ``""`` for a stage with no shape, so adding a stage without one
    degrades to prose rather than breaking its prompt.
    """
    text = BRIEF.get(stage)
    if not text:
        return ""
    return (
        "# 一份内容，多种展示\n\n"
        "优先只在 `structured` 中写完整产物，`content` 传空字符串。程序从同一份内容生成正文与创作板，"
        "不要再写第二份重复正文。不能用现有结构完整表达的产物可以使用非空 content 正文，但必须保持展示一致。\n\n"
        f"{text}\n"
        "- `open_questions`：仅保留实际未决事项，可以为空。每条包含 question、blocks、owner、timing、reason、kind。"
        "owner 为 user 或 team；timing 为 now 或 later；reason 说明为什么必须由该责任人处理。"
        "kind 四选一：`fact`（缺事实）、`product_choice`（产品二选一）、`local_design`（本版局部设计）、"
        "`brief_amendment`（要改产品目标/有效简报——不要用普通提问，走简报修订）。"
        "只有 owner=user、timing=now、kind≠brief_amendment 且理由明确的事项会请用户回答。素材检查、版本对应、工具能力、"
        "试读时长和内部审核都由 team 处理；执行细节在相应阶段处理。不要重复已回答的问题，"
        "不要为了填满字段制造问题。创意假设不自动成为必须由用户证明的事实。\n"
        "结构化正文缺少核心内容或发生字段丢失会返回错误，请补全后再提交；不要用空正文掩盖丢失内容。"
    )


# --------------------------------------------------------------------------- #
# Normalising
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Outcome:
    """What survived, and what did not.

    ``dropped`` counts individual cells and rows discarded. The pipeline records
    it as an event so that a prompt quietly failing to produce its shape is
    visible in the log rather than only in a screen that looks emptier than it
    should.
    """

    data: dict[str, Any]
    dropped: int

    def __bool__(self) -> bool:
        return bool(self.data)


def _text(value: Any) -> str:
    """A cell, or empty. Only strings are accepted.

    ``str()`` on an int or a dict would store something that renders as
    ``{'a': 1}`` in a table cell — technically not a crash, actually garbage.
    """
    if not isinstance(value, str):
        return ""
    return value.strip()[:MAX_CELL]


def _texts(value: Any) -> tuple[list[str], int]:
    if value is None:
        return [], 0
    if not isinstance(value, list):
        return [], 1
    kept = [t for t in (_text(v) for v in value) if t]
    dropped = len(value) - len(kept)
    if len(kept) > MAX_ROWS:
        # Counted, not silently sliced. A cap that drops rows without saying so
        # is the same failure this module exists to avoid.
        dropped += len(kept) - MAX_ROWS
        kept = kept[:MAX_ROWS]
    return kept, dropped


def _rows(value: Any, columns: tuple[str, ...]) -> tuple[list[dict[str, str]], int]:
    """Rows of exactly ``columns``. A row with nothing in it is not a row.

    Models routinely emit the right shape with one extra key, or drop a column
    they had nothing to say about. Keeping the columns we asked for and ignoring
    the rest is the difference between a usable table and no table.
    """
    if value is None:
        return [], 0
    if not isinstance(value, list):
        return [], 1

    kept: list[dict[str, str]] = []
    dropped = 0
    for item in value:
        if not isinstance(item, dict):
            dropped += 1
            continue
        row = {}
        for column in columns:
            cell = item.get(column)
            if column == "options" and isinstance(cell, dict):
                # The dependency matrix's option map: {"A": true, "B": false}.
                # Kept as a mapping of truthy/falsy, which is what a grid of
                # ticks needs; a nested table would be a layout decision made
                # in the wrong layer.
                row[column] = {str(k)[:32]: bool(v) for k, v in list(cell.items())[:12]}
                continue
            row[column] = _text(cell)
        if any(v for v in row.values()):
            kept.append(row)
        else:
            dropped += 1
    if len(kept) > MAX_ROWS:
        dropped += len(kept) - MAX_ROWS
        kept = kept[:MAX_ROWS]
    return kept, dropped


def normalize(stage: str, payload: Any) -> Outcome:
    """Coerce ``payload`` to ``stage``'s shape, dropping what does not fit.

    Never raises, and never returns a partial shape that would render as a
    half-built table: a field is present only if something survived in it.

    An unknown stage returns empty — a stage added to the pipeline without a
    shape here degrades to prose rather than breaking the run that produced it.
    """
    shape = SHAPES.get(stage)
    if shape is None or not isinstance(payload, dict):
        return Outcome({}, 1 if payload else 0)

    data: dict[str, Any] = {}
    dropped = 0
    for field in shape.fields:
        raw = payload.get(field.name)
        if field.kind == "text":
            value = _text(raw)
            if value:
                data[field.name] = value
        elif field.kind == "texts":
            values, lost = _texts(raw)
            dropped += lost
            if values:
                data[field.name] = values
        else:
            values, lost = _rows(raw, field.columns)
            dropped += lost
            if values:
                data[field.name] = values
    return Outcome(data, dropped)


# --------------------------------------------------------------------------- #
# What the interface needs to know
# --------------------------------------------------------------------------- #


def is_user_question(row: Any) -> bool:
    """The one place that decides whether an open question is his to answer.

    A row is addressed to him only when it names him as owner, asks for *now*,
    and says why the answer has to come from him. Everything else — no owner, a
    team owner, "later", or a missing reason — is still shown, but as something
    for the team to triage rather than a question put to him.

    Two surfaces read this rule: the stage's own card (``open_questions``) and
    the run-wide standing list (``project_brief.stage_issues``). They used to
    spell it out separately, which meant a change to the rule could land in one
    and leave the card and the list disagreeing about the same document.
    """
    if not isinstance(row, dict) or not row.get("question"):
        return False
    reason = row.get("reason")
    return (row.get("owner") == "user" and row.get("timing") == "now"
            and isinstance(reason, str) and bool(reason.strip()))


def question_kind(row: Any) -> str:
    """Normalized kind for one open_questions row (default ``fact``)."""
    if not isinstance(row, dict):
        return "fact"
    kind = str(row.get("kind") or "").strip()
    return kind if kind in QUESTION_KINDS else "fact"


def is_ask_user_question(row: Any) -> bool:
    """A question the conversation may actually put to him as ask_user.

    ``brief_amendment`` is excluded on purpose: 「这算不算种草」 is not a blank
    to fill — it changes the product goal and must go through amend_brief.
    """
    return is_user_question(row) and question_kind(row) != "brief_amendment"


def open_questions(structured: dict[str, Any], stage: str) -> list[dict[str, str]]:
    """This stage's unanswered questions, tagged with where they came from.

    Read from the payload rather than from ``limitations``, because
    ``limitations`` is the model explaining itself and reads as a disclaimer.
    The same facts routed through a field called ``open_questions`` are a
    request addressed to the only person who can answer them.

    ``brief_amendment`` rows are omitted here — they are not ask_user work.
    """
    rows = (structured or {}).get("open_questions") or []
    return [
        {"stage": stage, "question": r.get("question", ""), "blocks": r.get("blocks", "")}
        for r in rows
        if is_ask_user_question(r)
    ]


def question_answers(chat: list[dict[str, Any]] | None, question: str,
                     since: str = "") -> list[dict[str, str]]:
    """Exact user replies to one saved question, with their source time."""
    if not chat or not question:
        return []
    pattern = re.compile(r"(?:^|\n)(?:\d+\.\s*)?(?:回答|关于)「" + re.escape(question)
                         + r"」[：:][ \t]*(\S[^\n]*)")
    answers = []
    for row in chat:
        if not isinstance(row, dict) or row.get("role") != "user" or (since and row.get("at", "") < since):
            continue
        for match in pattern.finditer(str(row.get("text") or "")):
            answers.append({"question": question, "answer": match.group(1).strip(),
                            "at": str(row.get("at") or "")})
    return answers


def question_answered(chat: list[dict[str, Any]] | None, question: str, since: str = "") -> bool:
    """Whether the user answered this exact artifact question after it was saved."""
    return bool(question_answers(chat, question, since))


def material_blockers(structured: dict[str, Any]) -> list[str]:
    """Critical material gaps that must be resolved before full script writing."""
    if not isinstance(structured, dict):
        return []
    blockers = [str(row.get('shot') or '未命名画面') for row in structured.get('items', [])
                if isinstance(row, dict) and row.get('status') == '无法获得']
    blockers.extend(str(row.get('gap') or '未命名缺口') for row in structured.get('gaps', [])
                    if isinstance(row, dict) and str(row.get('blocking') or '').strip() == '阻塞')
    return list(dict.fromkeys(blockers))


def canonical_document(stage: str, data: dict[str, Any]) -> str:
    """Render the normalized source, never a second model-authored document."""
    core = {"intake": "facts", "direction": "options", "material_plan": "items", "writing": "segments"}
    if not data.get(core.get(stage, "")):
        raise ValueError("结构化正文缺少本阶段核心内容；请补全或提交完整正文。")
    labels = {
        "intake": "创作需求", "direction": "创意方向", "material_plan": "素材计划", "writing": "脚本与分镜",
        "facts": "已知事实", "constraints": "制作要求", "open_questions": "待处理事项",
        "options": "方向方案", "recommended": "推荐方向", "recommended_because": "推荐理由",
        "prerequisites": "需要验证的前提", "dependency_matrix": "方案依赖", "items": "素材安排",
        "gaps": "素材缺口", "segments": "分镜内容", "total_duration": "总时长", "caption": "发布文案", "assumptions": "时长假设",
        "label": "事实", "value": "内容", "source": "来源", "key": "方案", "title": "标题", "claim": "主张",
        "audience_moment": "观众与处境", "hook": "开场", "story": "推进概念", "gives_up": "方案取舍",
        "evidence_strength": "依据", "difficulty": "执行难度", "asset_needs": "素材需求",
        "need": "前提", "shot": "画面", "purpose": "作用", "status": "状态", "note": "说明", "file": "素材文件",
        "gap": "缺口", "consequence": "影响", "blocking": "是否阻塞脚本", "tc": "段落", "picture": "画面", "line": "台词或字幕",
        "sound": "声音", "duration": "时长", "question": "事项", "blocks": "影响", "owner": "责任人",
        "timing": "处理时机", "reason": "责任说明", "kind": "类型",
    }
    lines = [f"# {labels[stage]}", "", "<!-- studio:structured-document-v1 -->"]
    for field in SHAPES[stage].fields:
        value = data.get(field.name)
        if not value:
            continue
        lines += ["", f"## {labels.get(field.name, field.name)}", ""]
        if isinstance(value, str):
            lines.append(value)
        elif field.kind == "texts":
            lines.extend(f"- {item}" for item in value)
        else:
            for index, row in enumerate(value, 1):
                lines += [f"### {index}", ""]
                for key, cell in row.items():
                    if isinstance(cell, dict):
                        cell = "；".join(f"{k}：{'需要' if v else '不依赖'}" for k, v in cell.items())
                    if cell:
                        lines.append(f"- **{labels.get(key, key)}**：{cell}")
                lines.append("")
    return "\n".join(lines).strip()


def is_canonical_document(stage: str, data: dict, text: str) -> bool:
    try:
        return text == canonical_document(stage, data)
    except ValueError:
        return False


def validate_canonical_input(stage: str, payload: dict) -> None:
    """Single-source authoring cannot silently truncate or drop source content."""
    shape = SHAPES[stage]
    def check_text(value):
        if value is not None and (not isinstance(value, str) or len(value.strip()) > MAX_CELL):
            raise ValueError("结构化文字必须为字符串且不超过字段容量；请调整字段或使用完整正文。")
    for key, value in payload.items():
        field = shape.field(key)
        if field is None:
            if value:
                raise ValueError(f"结构化正文不支持字段 {key}，请放入对应内容字段或使用完整正文。")
            continue
        if field.kind == 'text':
            check_text(value)
        elif isinstance(value, list):
            for item in value:
                if field.kind == 'texts':
                    check_text(item)
                elif isinstance(item, dict):
                    for column, cell in item.items():
                        if column not in field.columns and cell:
                            raise ValueError(f"结构化正文不支持 {key}.{column}，请使用已定义字段。")
                        if column == 'options':
                            if not isinstance(cell, dict) or any(not isinstance(v, bool) for v in cell.values()):
                                raise ValueError("方案依赖必须明确写 true 或 false。")
                            if len(cell) > 12 or any(not isinstance(k, str) or len(k) > 32 for k in cell):
                                raise ValueError("方案依赖最多 12 项，方案标识不超过 32 字符。")
                        else:
                            check_text(cell)
                    if key == 'open_questions' and item.get('question'):
                        if item.get('owner') not in {'user', 'team'} or item.get('timing') not in {'now', 'later'} or not (item.get('reason') or '').strip():
                            raise ValueError("待处理事项需要明确 owner、timing 和 reason；内部工作归 team。")
                        kind = item.get('kind')
                        if kind and kind not in QUESTION_KINDS:
                            raise ValueError(f"待处理事项 kind 必须是 {'/'.join(QUESTION_KINDS)} 之一。")


def blocked_material(structured: dict[str, Any]) -> list[dict[str, str]]:
    """The material rows that cannot be filmed, in the order they were listed."""
    return [
        r for r in (structured or {}).get("items") or []
        if isinstance(r, dict) and r.get("status") in MATERIAL_BLOCKED
    ]


def direction_options(structured: dict[str, Any]) -> list[dict[str, Any]]:
    """Actual choices stay selectable even without a readable recommendation."""
    options = (structured or {}).get("options") or []
    chosen = _chosen((structured or {}).get("recommended", ""))
    keys = [str(r.get("key") or "").strip() for r in options
            if isinstance(r, dict) and str(r.get("key") or "").strip()]
    if len(set(keys)) != len(keys):
        return []
    return [{"key": k, "recommended": k.upper() == chosen} for k in keys]




# --------------------------------------------------------------------------- #
# The view model
# --------------------------------------------------------------------------- #

#: Wire key -> the words on screen. One table for every stage rather than labels
#: carried in ``Column``, because the same key means the same thing wherever it
#: appears (``status`` is 状态 on a material row and nowhere else) and because
#: the normaliser's column tuples stay plain strings. A test asserts every
#: column of every shape has an entry here, so a new field cannot ship as a
#: blank header.
LABEL: dict[str, str] = {
    "label": "项目",
    "value": "内容",
    "source": "出处",
    "question": "问题",
    "blocks": "卡住了什么",
    "key": "方案",
    "title": "标题",
    "claim": "主张",
    "audience_moment": "给谁看、什么场景",
    "hook": "开场识别点",
    "gives_up": "方案取舍",
    "story": "推进概念",
    "owner": "责任人",
    "timing": "处理时机",
    "reason": "需要确认的原因",
    "kind": "类型",
    "evidence_strength": "证据强度",
    "difficulty": "执行难度",
    "asset_needs": "素材依赖",
    "satisfies": "满足简报",
    "violates": "违反简报",
    "amend_clause": "须先修订",
    "amend_proposal": "修订提案",
    "shot": "画面",
    "purpose": "证明什么",
    "status": "状态",
    "note": "备注",
    "gap": "缺什么",
    "consequence": "会导致什么",
    "blocking": "脚本是否受阻",
    "tc": "时码",
    "picture": "画面",
    "line": "台词",
    "sound": "声音",
    "duration": "时长",
    "need": "空白",
}

#: Status -> how it should read before anyone reads the word.
#:
#: The tone names are this module's, not the stylesheet's: a status the model
#: invented is not mapped to a colour, it is left as ``unknown`` and shown
#: verbatim, which is the honest outcome — see ``MATERIAL_STATUSES``.
STATUS_TONE: dict[str, str] = {
    "已有": "ok",
    "需要拍": "todo",
    "需要生成": "gen",
    "无法获得": "bad",
    "不确定": "warn",
}

#: Option keys are A/B/C. Used only to decide which card gets the highlight.
_OPTION_KEY = re.compile(r"[A-D]")

#: Option letters named immediately after 「推荐」. The director writes prose, so
#: the letter it recommends has to be read out of a sentence that also names the
#: ones it did not choose — 「推荐 B，因为 A 的前提还没确认」 mentions both.
_RECOMMENDED = re.compile(r"推荐[^A-Da-d]{0,6}([A-Da-d])")

#: ``(field name, column)`` pairs that are structural rather than prose, so they
#: have no label to render. Named as pairs rather than bare column names so an
#: unrelated field called ``options`` cannot inherit the exemption.
STRUCTURAL_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("dependency_matrix", "options"),
        # A filename is drawn as a picture or not at all — a `素材: O1CN01.jpg`
        # row beside the picture it names is the same fact printed twice.
        ("items", "file"),
        ("segments", "file"),
    }
)


def label(key: str) -> str:
    """The display name for a wire key, or the key itself.

    Falling back to the key rather than raising keeps an unmapped column
    readable; the coverage test is what stops that from shipping.
    """
    return LABEL.get(key, key)


def _fields(row: dict[str, Any], keys: tuple[str, ...], secondary=()) -> list[dict[str, Any]]:
    """The non-empty columns of one row, labelled, in the order given."""
    return [
        {"label": label(k), "value": str(row.get(k) or ""), **({"secondary": True} if k in secondary else {})}
        for k in keys
        if row.get(k)
    ]


def _chip(status: str) -> dict[str, str]:
    return {"text": status, "tone": STATUS_TONE.get(status, "unknown")}


def _mentioned(text: str) -> set[str]:
    """The option letters named in a sentence like 「推荐 B」."""
    return set(_OPTION_KEY.findall((text or "").upper()))


def _chosen(recommended: str) -> str:
    """The option the recommendation names, or "" when it is not clear.

    Tries the 「推荐 X」 shape first, because that is how the field is written and
    because a sentence that also explains itself names the other options too.
    Falls back to a single unambiguous letter, and gives up otherwise: the
    highlight is decoration, and a wrong one is worse than none — what the
    director wrote is rendered verbatim in the callout beside it.
    """
    match = _RECOMMENDED.search(recommended or "")
    if match:
        return match.group(1).upper()
    letters = _mentioned(recommended)
    return next(iter(letters)) if len(letters) == 1 else ""


def _intake(data: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if data.get("facts"):
        blocks.append(
            {
                "type": "table",
                "title": "已经确认的事实",
                "columns": [label("label"), label("value"), label("source")],
                "rows": [
                    [r.get("label", ""), r.get("value", ""), r.get("source", "")]
                    for r in data["facts"]
                ],
            }
        )
    if data.get("constraints"):
        blocks.append(
            {
                "type": "list",
                "title": "硬约束",
                "items": [{"text": t} for t in data["constraints"]],
            }
        )
    return blocks


def _direction(data: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    options = data.get("options") or []

    if options:
        # The highlight is decoration. What the director actually wrote about
        # which option it recommends is rendered as a callout below, in its own
        # words, and that is the authoritative statement — a highlight that
        # disagreed with the prose would be the screen lying.
        chosen = _chosen(data.get("recommended", ""))
        blocks.append(
            {
                "type": "cards",
                "title": f"{len(options)} 个方向，摊开比",
                "cards": [
                    {
                        "head": o.get("key", ""),
                        "emphasis": "chosen" if o.get("key", "").upper() == chosen else "",
                        "fields": _fields(
                            o,
                            (
                                "title", "claim", "audience_moment", "hook", "story", "gives_up",
                                "evidence_strength", "difficulty", "asset_needs",
                                "satisfies", "violates", "amend_clause", "amend_proposal",
                            ),
                            secondary=(
                                "gives_up", "evidence_strength", "difficulty", "asset_needs",
                                "satisfies", "violates", "amend_clause", "amend_proposal",
                            ),
                        ),
                    }
                    for o in options
                ],
            }
        )

    if data.get("recommended") or data.get("recommended_because"):
        # The title is fixed and the fields are quoted, rather than concatenated.
        # The brief asks for a bare letter here, but a director that writes the
        # whole recommendation into `recommended` produced 「推荐 推荐 B，因为…」.
        # Quoting both fields verbatim cannot double anything.
        blocks.append(
            {
                "type": "callout",
                "title": "推荐",
                "text": " —— ".join(
                    p for p in (data.get("recommended", ""), data.get("recommended_because", "")) if p
                ),
                "items_title": "推荐时仍需核实：",
                "items": [{"text": t} for t in data.get("prerequisites") or []],
            }
        )

    if data.get("dependency_matrix"):
        keys = [o.get("key", "") for o in options] or sorted(
            {k for r in data["dependency_matrix"] for k in (r.get("options") or {})}
        )
        blocks.append(
            {
                "type": "matrix",
                "collapsed": True,
                "title": "方向涉及的待核实条件",
                "columns": keys,
                # ``true`` means the current execution is affected. The UI
                # must not turn it into an irreversible verdict.
                "rows": [
                    {
                        "label": r.get("need", ""),
                        "cells": [bool((r.get("options") or {}).get(k)) for k in keys],
                    }
                    for r in data["dependency_matrix"]
                ],
            }
        )
    return blocks


def _material(data: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    items = data.get("items") or []

    if items:
        blocked = blocked_material(data)
        title = f"{len(items)} 个镜头"
        if blocked:
            title += f"，其中 {len(blocked)} 个做不出来"
        blocks.append(
            {
                "type": "cards",
                "title": title,
                "cards": [
                    {
                        "head": r.get("shot", ""),
                        "chip": _chip(r.get("status", "")) if r.get("status") else None,
                        "emphasis": "blocked" if r.get("status") in MATERIAL_BLOCKED else "",
                        "fields": _fields(r, ("purpose", "note")),
                        # A filename, not a URL: this module knows nothing about
                        # the data root or the run id, and the screen is the layer
                        # that can also find out whether the file is really there.
                        "media": r.get("file", ""),
                    }
                    for r in items
                ],
            }
        )

    if data.get("gaps"):
        blocks.append(
            {
                "type": "list",
                "title": "缺口",
                "items": [
                    {"text": r.get("gap", ""),
                     "note": " · ".join(part for part in (
                         "阻塞脚本" if r.get("blocking") == "阻塞" else "",
                         r.get("consequence", "")) if part)}
                    for r in data["gaps"]
                ],
            }
        )
    return blocks


#: A timecode range as the writer produces it: `0:03–0:07`. Either dash, because
#: a model writes an en dash when it is being careful and a hyphen when it is not.
_TC_RANGE = re.compile(r"(\d+):(\d+)\s*[–\-—~]\s*(\d+):(\d+)")
_FIRST_NUMBER = re.compile(r"(\d+(?:\.\d+)?)")

#: Lines that mean "nothing is said here". Printed as a bar label they would
#: read as words the voice-over actually speaks.
_NO_LINE = frozenset({"（无台词）", "(无台词)", "无台词", "无", "—", "-", ""})


def _seconds(tc: str, duration: str) -> float | None:
    """How long one shot is, or None when nothing says.

    The timecode is preferred over ``duration``: it is what the rest of the shot
    list is aligned to, whereas ``duration`` is prose ("约 3 秒"). None rather
    than 0, so the screen can tell "no length given" from "no length" — a bar of
    width zero would drop the shot out of the strip without saying so.
    """
    match = _TC_RANGE.search(tc or "")
    if match:
        start = int(match.group(1)) * 60 + int(match.group(2))
        end = int(match.group(3)) * 60 + int(match.group(4))
        if end > start:
            return float(end - start)
    match = _FIRST_NUMBER.search(duration or "")
    if match and float(match.group(1)) > 0:
        return float(match.group(1))
    return None


def _strip_segment(row: dict[str, Any]) -> dict[str, Any]:
    """One bar of the storyboard strip.

    Carries the whole line and picture rather than a shortened label: the bar
    clips with CSS, because how much fits is a question about the screen and not
    about the shot list.
    """
    line = row.get("line", "")
    return {
        "tc": row.get("tc", ""),
        "seconds": _seconds(row.get("tc", ""), row.get("duration", "")),
        "label": row.get("picture", "") if line in _NO_LINE else line,
        "media": row.get("file", ""),
    }


def _writing(data: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if data.get("total_duration"):
        blocks.append({"type": "text", "title": "总时长", "text": data["total_duration"]})
    segments = data.get("segments") or []
    if segments:
        # The strip comes first and the per-shot cards second on purpose. The
        # strip answers "does this fill 15 seconds, and where is the slow part"
        # without reading a word — which is the question a shot list exists to
        # answer, and the one a wall of text is worst at.
        blocks.append(
            {
                "type": "timeline",
                "title": f"分镜（{len(segments)} 段）",
                "total": data.get("total_duration", ""),
                "segments": [_strip_segment(s) for s in segments],
            }
        )
        blocks.append(
            {
                "type": "cards",
                "title": "逐段",
                "cards": [
                    {
                        "head": s.get("tc", ""),
                        "fields": _fields(s, ("picture", "line", "sound", "duration")),
                        "media": s.get("file", ""),
                    }
                    for s in segments
                ],
            }
        )
    if data.get("assumptions"):
        blocks.append(
            {
                "type": "list",
                "title": "这份脚本做了哪些假设",
                "items": [{"text": t} for t in data["assumptions"]],
            }
        )
    if data.get("caption"):
        blocks.append({"type": "text", "title": "发布文案", "text": data["caption"]})
    return blocks


_VIEW = {
    "intake": _intake,
    "direction": _direction,
    "material_plan": _material,
    "writing": _writing,
}


def view(stage: str, structured: dict[str, Any]) -> list[dict[str, Any]]:
    """A stage's structure as blocks the screen can draw, in reading order.

    Blocks rather than the raw payload, for the same reason ``vocabulary.py``
    returns sentences rather than field names: the frontend then holds one
    generic renderer and no idea what a 素材板 or a 分镜条 is. Stage knowledge
    stays here, where the shapes are, and there is one copy of it.

    Empty payload returns ``[]``, and the card falls back to the prose it has
    always shown. A stage with no builder does the same.
    """
    if not structured:
        return []
    build = _VIEW.get(stage)
    return build(structured) if build else []
