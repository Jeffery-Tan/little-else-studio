"""The stage shapes, and what happens when a model does not produce them.

The module's docstring makes two promises, so those are what is tested: nothing
here raises, and nothing is dropped without being counted. The rest is the
declared shapes staying aligned with what the roles are actually asked to write.
"""
from __future__ import annotations

import pytest

from studio import structured as s
from studio.models import STAGES


# --------------------------------------------------------------- the shapes -- #


def test_every_stage_has_a_shape():
    for stage in STAGES:
        assert stage in s.SHAPES, f"{stage} would silently degrade to prose"


def test_every_stage_can_report_open_questions():
    """The field the interface leans on hardest, and the reason this exists.

    `open_questions` is the only way a stage's unresolved blanks become something
    addressed to the one person who can answer them. A stage without it is a
    stage whose questions stay buried in prose.
    """
    for stage, shape in s.SHAPES.items():
        field = shape.field("open_questions")
        assert field is not None, f"{stage} has nowhere to put its open questions"
        assert field.kind == "rows"
        assert field.columns == ("question", "blocks", "owner", "timing", "reason")


def test_the_direction_shape_covers_what_the_role_is_told_to_write():
    """The optional title and established creative attributes share one shape."""
    columns = set(s.SHAPES["direction"].field("options").columns)
    assert {"key", "title", "claim", "audience_moment", "hook"} <= columns
    assert {"gives_up", "evidence_strength", "difficulty", "asset_needs"} <= columns


def test_the_writing_shape_covers_the_timed_columns():
    columns = s.SHAPES["writing"].field("segments").columns
    assert columns == ("tc", "picture", "line", "sound", "duration", "file")


# ----------------------------------------------------------- the happy path -- #


def test_a_well_formed_payload_survives_intact():
    payload = {
        "options": [
            {"key": "A", "claim": "外观认同", "audience_moment": "通勤", "hook": "第一秒",
             "gives_up": "容量", "evidence_strength": "弱", "difficulty": "低",
             "asset_needs": "实拍图"},
            {"key": "B", "claim": "容量二选一", "audience_moment": "装饭", "hook": "对比",
             "gives_up": "外观", "evidence_strength": "强", "difficulty": "中",
             "asset_needs": "官方图"},
        ],
        "recommended": "B",
        "prerequisites": ["容量是买家纠结点", "至少两档官方图"],
        "dependency_matrix": [{"need": "实拍官方图", "options": {"A": True, "B": False}}],
        "open_questions": [{"question": "主推哪个 SKU", "blocks": "A 和 C 都定不了"}],
    }
    out = s.normalize("direction", payload)
    assert out.dropped == 0
    assert len(out.data["options"]) == 2
    assert out.data["options"][1]["claim"] == "容量二选一"
    assert out.data["recommended"] == "B"
    assert out.data["prerequisites"] == ["容量是买家纠结点", "至少两档官方图"]


def test_optional_creative_title_is_part_of_the_formal_direction_artifact():
    payload = {"options": [{"key": "A", "title": "从桌边到海边", "claim": "午餐场景切换"}]}
    s.validate_canonical_input("direction", payload)
    data = s.normalize("direction", payload).data
    assert data["options"][0]["title"] == "从桌边到海边"
    assert "- **标题**：从桌边到海边" in s.canonical_document("direction", data)
    fields = s.view("direction", data)[0]["cards"][0]["fields"]
    assert fields[0] == {"label": "标题", "value": "从桌边到海边"}


def test_old_direction_without_title_keeps_its_existing_canonical_reading():
    data = s.normalize("direction", {"options": [{"key": "A", "claim": "原主张"}]}).data
    assert s.canonical_document("direction", data) == (
        "# 创意方向\n\n<!-- studio:structured-document-v1 -->\n\n"
        "## 方向方案\n\n### 1\n\n- **方案**：A\n- **主张**：原主张"
    )
    assert all(field["label"] != "标题" for field in s.view("direction", data)[0]["cards"][0]["fields"])


def test_the_dependency_matrix_keeps_its_ticks_as_booleans():
    """A grid of ticks, not a nested table — the layout decision belongs in the UI."""
    out = s.normalize("direction", {
        "dependency_matrix": [{"need": "目标观众", "options": {"A": False, "B": True, "C": False}}],
    })
    assert out.data["dependency_matrix"][0]["options"] == {"A": False, "B": True, "C": False}


# ---------------------------------------------------- degrading, not failing -- #


@pytest.mark.parametrize("payload", [
    None, [], "a string", 42, {"options": "not a list"}, {"options": [1, 2, 3]},
    {"recommended": {"nested": "dict"}}, {"open_questions": [{"question": 42}]},
    {"options": [{"key": {"a": 1}}]}, {"facts": [None]},
])
def test_a_malformed_payload_degrades_rather_than_raising(payload):
    """The promise that matters: a bad payload costs a rendering, not an attempt.

    A director's attempt is the most expensive thing in the pipeline. Failing it
    because the structure did not parse would trade a document for nothing.
    """
    out = s.normalize("direction", payload)
    assert isinstance(out.data, dict)


def test_an_unknown_stage_degrades_instead_of_breaking_the_run():
    out = s.normalize("storyboard", {"anything": 1})
    assert out.data == {}


def test_cells_that_are_not_strings_are_dropped_not_stringified():
    """``str({'a': 1})`` in a table cell is not a crash, it is garbage."""
    out = s.normalize("intake", {
        "facts": [{"label": "货款", "value": {"amount": 2310}, "source": "intake §3"}],
    })
    assert out.data["facts"][0]["value"] == ""
    assert out.data["facts"][0]["label"] == "货款"


def test_a_row_with_nothing_in_it_is_not_a_row_and_is_counted():
    out = s.normalize("intake", {
        "facts": [{"label": "", "value": "", "source": ""}, {"label": "货款", "value": "¥2310"}],
    })
    assert len(out.data["facts"]) == 1
    assert out.dropped == 1, "a dropped row is reported, not silently discarded"


def test_missing_columns_come_back_empty_rather_than_absent():
    """A model that skipped a column still yields a table with that column."""
    out = s.normalize("writing", {
        "segments": [{"tc": "0-3s", "picture": "产品特写", "line": "带饭不凉"}],
    })
    segment = out.data["segments"][0]
    assert segment["sound"] == ""
    assert segment["duration"] == ""
    assert segment["line"] == "带饭不凉"


def test_extra_keys_the_model_invented_are_not_kept():
    out = s.normalize("intake", {"constraints": ["不得提及平台名"], "bonus": "unrequested"})
    assert "bonus" not in out.data


def test_an_empty_list_leaves_the_field_out_rather_than_present_and_empty():
    """A key holding [] would render as a heading with nothing under it."""
    out = s.normalize("intake", {"facts": [], "constraints": ["a rule"]})
    assert "facts" not in out.data
    assert out.data["constraints"] == ["a rule"]


def test_an_empty_payload_is_falsy_so_the_caller_can_degrade():
    out = s.normalize("intake", {})
    assert not out
    assert out.data == {}


# ------------------------------------------------------------- the ceilings -- #


def test_rows_past_the_cap_are_dropped_and_counted():
    payload = {"constraints": [f"规则 {i}" for i in range(s.MAX_ROWS + 25)]}
    out = s.normalize("intake", payload)
    assert len(out.data["constraints"]) == s.MAX_ROWS
    assert out.dropped == 25, "a cap that drops rows must say how many"


def test_an_overlong_cell_is_truncated_rather_than_refused():
    out = s.normalize("intake", {"constraints": ["x" * (s.MAX_CELL + 500)]})
    assert len(out.data["constraints"][0]) == s.MAX_CELL


# ------------------------------------------------- the three interface helpers -- #


def test_open_questions_come_back_tagged_with_their_stage():
    out = s.normalize("intake", {
        "open_questions": [{"question": "主推 SKU 是哪个", "blocks": "素材计划定不了",
                            "owner": "user", "timing": "now", "reason": "用户决定主推商品"}],
    })
    questions = s.open_questions(out.data, "intake")
    assert questions == [{
        "stage": "intake",
        "question": "主推 SKU 是哪个",
        "blocks": "素材计划定不了",
    }]


def test_open_questions_is_empty_not_missing_when_there_are_none():
    assert s.open_questions({}, "intake") == []
    assert s.open_questions(None, "intake") == []


def test_material_rows_that_cannot_be_filmed_are_singled_out():
    """A plan whose unfilmable shots are scattered through it reads as complete.

    `无法获得` and `不确定` are the two statuses that mean the shot does not
    exist, and they are the whole point of asking for a material plan.
    """
    out = s.normalize("material_plan", {
        "items": [
            {"shot": "产品特写", "status": "已有"},
            {"shot": "容量实测", "status": "无法获得"},
            {"shot": "达人实拍", "status": "不确定"},
            {"shot": "开场", "status": "需要生成"},
        ],
    })
    blocked = s.blocked_material(out.data)
    assert [r["shot"] for r in blocked] == ["容量实测", "达人实拍"]


def test_an_unrecognised_material_status_is_kept_verbatim():
    """Reassigning it would hide what the model actually wrote."""
    out = s.normalize("material_plan", {"items": [{"shot": "x", "status": "待定"}]})
    assert out.data["items"][0]["status"] == "待定"
    assert s.blocked_material(out.data) == [], "but it is not one of the blocked ones"


def _direction(recommended, keys=("A", "B", "C")):
    return s.normalize("direction", {
        "options": [{"key": k, "claim": f"{k} 的主张"} for k in keys],
        "recommended": recommended,
    }).data


def test_direction_options_come_back_in_the_order_they_were_written():
    """What the gate card draws its buttons from.

    The order is the document's order: the option he reads first on the card is
    the option whose button is first, so the two cannot be read as disagreeing
    about which one is which.
    """
    found = s.direction_options(_direction("推荐 B"))
    assert [(o["key"], o["recommended"]) for o in found] == [
        ("A", False), ("B", True), ("C", False),
    ]


def test_one_option_is_not_a_choice():
    """「就按 A 走」 beside nothing else is the plain 通过 button with a worse name."""
    assert s.direction_options(_direction("推荐 A", keys=("A",))) == [{"key": "A", "recommended": True}]


def test_an_unreadable_recommendation_leaves_no_option_to_single_out():
    """Same give-up as the highlight, for the same reason — see ``_chosen``.

    「A 和 B 都行」 names both, so no button can claim the director's backing.
    """
    assert all(not o["recommended"] for o in s.direction_options(_direction("A 和 B 都行，看你想要哪个")))
    assert len(s.direction_options(_direction(""))) == 3


def test_a_recommendation_naming_an_option_that_was_never_laid_out_is_ignored():
    """The buttons and the recorded reason both quote the recommendation.

    If it names something not on the card, the reason for the rework would read
    「导演推荐的是 」 — a blank where an author should be.
    """
    out = s.normalize("direction", {
        "options": [{"key": "A", "claim": "x"}, {"key": "B", "claim": "y"}],
        "recommended": "推荐 C",
    })
    assert s.direction_options(out.data) == [{"key": "A", "recommended": False}, {"key": "B", "recommended": False}]


def test_an_option_with_no_key_does_not_become_a_blank_button():
    out = s.normalize("direction", {
        "options": [{"key": "A", "claim": "x"}, {"key": "B", "claim": "y"}, {"claim": "z"}],
        "recommended": "推荐 A",
    })
    assert [o["key"] for o in s.direction_options(out.data)] == ["A", "B"]


def test_an_empty_direction_payload_offers_nothing():
    assert s.direction_options({}) == []
    assert s.direction_options(None) == []


# ---------------------------------------------------------- the view model -- #


def test_every_column_of_every_shape_has_words_on_screen():
    """A shape may not ship a header that renders as a wire key.

    ``view`` falls back to the key rather than raising, which is the right
    behaviour at runtime and the wrong one to discover in a screenshot.
    """
    for stage, shape in s.SHAPES.items():
        for field in shape.fields:
            for column in field.columns:
                if (field.name, column) in s.STRUCTURAL_COLUMNS:
                    continue  # the tick grid, which draws no text cell
                assert column in s.LABEL, f"{stage}.{field.name}.{column} has no label"


def test_structural_columns_are_declared_ones_that_really_exist():
    """A stale exemption would silently excuse a future column from having words."""
    declared = {(f.name, c) for sh in s.SHAPES.values() for f in sh.fields for c in f.columns}
    assert s.STRUCTURAL_COLUMNS <= declared


def test_an_ambiguous_recommendation_highlights_nothing():
    """「A 和 B 都行」 names both, so neither is marked.

    The callout below the cards quotes what the director wrote, and that is what
    he reads. A highlight that picked a side here would be the screen inventing a
    decision the document did not make.
    """
    data = s.normalize("direction", {
        "options": [{"key": "A", "claim": "x"}, {"key": "B", "claim": "y"}],
        "recommended": "A 和 B 都行，看你想要哪个",
    }).data
    cards = s.view("direction", data)[0]["cards"]
    assert [c["emphasis"] for c in cards] == ["", ""]


def test_an_empty_payload_produces_no_blocks():
    """The card falls back to prose, which is what it did before blocks existed."""
    for stage in STAGES:
        assert s.view(stage, {}) == []


def test_an_unknown_stage_produces_no_blocks():
    assert s.view("storyboard", {"anything": []}) == []


def test_direction_blocks_name_the_recommended_option():
    data = s.normalize("direction", {
        "options": [{"key": "A", "claim": "外观"}, {"key": "B", "claim": "容量"}],
        "recommended": "推荐 B，因为 A 的前提还没确认",
        "recommended_because": "容量有实测支撑。",
        "prerequisites": ["拿到官方容量图"],
        "dependency_matrix": [
            {"need": "实拍官方图", "options": {"A": True, "B": False}},
            {"need": "主推 SKU", "options": {"A": True, "B": True}},
        ],
    }).data

    blocks = s.view("direction", data)
    cards = next(b for b in blocks if b["type"] == "cards")
    chosen = [c["head"] for c in cards["cards"] if c["emphasis"] == "chosen"]
    assert chosen == ["B"]

    callout = next(b for b in blocks if b["type"] == "callout")
    # Quoted, not spliced into a title: a director that writes the whole
    # recommendation here used to produce 「推荐 推荐 B，因为…」.
    assert callout["title"] == "推荐"
    assert callout["text"] == "推荐 B，因为 A 的前提还没确认 —— 容量有实测支撑。"
    assert [i["text"] for i in callout["items"]] == ["拿到官方容量图"]

    matrix = next(b for b in blocks if b["type"] == "matrix")
    assert matrix["columns"] == ["A", "B"]
    # ``true`` means the option does not stand without that blank. Row order and
    # column order both come from the payload, so the grid cannot transpose.
    assert [r["cells"] for r in matrix["rows"]] == [[True, False], [True, True]]


def test_material_blocks_count_what_cannot_be_filmed():
    data = s.normalize("material_plan", {
        "items": [
            {"shot": "产品特写", "status": "已有"},
            {"shot": "容量实测", "status": "无法获得"},
            {"shot": "达人实拍", "status": "不确定"},
        ],
        "gaps": [{"gap": "没有实测素材", "consequence": "这条主张没法证明"}],
    }).data

    blocks = s.view("material_plan", data)
    cards = next(b for b in blocks if b["type"] == "cards")
    assert cards["title"] == "3 个镜头，其中 2 个做不出来"
    assert [c["chip"]["tone"] for c in cards["cards"]] == ["ok", "bad", "warn"]
    assert [c["emphasis"] for c in cards["cards"]] == ["", "blocked", "blocked"]

    gaps = next(b for b in blocks if b["type"] == "list")
    assert gaps["items"] == [{"text": "没有实测素材", "note": "这条主张没法证明"}]


def test_a_status_the_model_invented_is_not_given_a_tone():
    """Mapping it to a colour would present a guess as a classification."""
    data = s.normalize("material_plan", {"items": [{"shot": "x", "status": "待定"}]}).data
    cards = s.view("material_plan", data)[0]["cards"]
    assert cards[0]["chip"] == {"text": "待定", "tone": "unknown"}


def test_view_drops_no_cell_the_normaliser_kept():
    """The two must agree, or a card silently shows less than the file holds."""
    data = s.normalize("writing", {
        "segments": [{"tc": "00:00-00:03", "picture": "包型", "line": "装得下",
                      "sound": "轻音乐", "duration": "约 3 秒",
                      "file": "O1CN01XrQX.jpg"}],
        "total_duration": "约 30 秒",
        "assumptions": ["时长是估的"],
    }).data
    blocks = s.view("writing", data)
    assert [b["type"] for b in blocks] == ["text", "timeline", "cards", "list"]
    assert blocks[0]["text"] == "约 30 秒"

    strip = blocks[1]["segments"][0]
    assert strip["tc"] == "00:00-00:03"
    assert strip["seconds"] == 3.0
    assert strip["label"] == "装得下"
    assert strip["media"] == "O1CN01XrQX.jpg"

    card = blocks[2]["cards"][0]
    assert card["head"] == "00:00-00:03"
    assert [f["label"] for f in card["fields"]] == ["画面", "台词", "声音", "时长"]
    # `file` has no label — it is drawn as a picture — but it must still arrive.
    assert card["media"] == "O1CN01XrQX.jpg"


def test_a_writing_block_with_no_shot_keeps_the_assumptions():
    """A missing half of a payload must not take the other half down with it."""
    data = s.normalize("writing", {"assumptions": ["总时长是估的"]}).data
    blocks = s.view("writing", data)
    assert len(blocks) == 1
    assert blocks[0]["title"] == "这份脚本做了哪些假设"


def test_optional_caption_comes_from_the_same_versioned_writing_artifact():
    data = s.normalize("writing", {
        "segments": [{"tc": "0:00–0:03", "line": "Lunch is ready."}],
        "caption": "A compact lunch bag for the commute.",
    }).data
    assert data["caption"] == "A compact lunch bag for the commute."
    assert s.view("writing", data)[-1] == {
        "type": "text", "title": "发布文案", "text": data["caption"],
    }
    assert "## 发布文案" in s.canonical_document("writing", data)
