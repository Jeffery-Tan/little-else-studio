"""Rule-only project titles from creator prompts; no model calls."""
from studio.project_title import derive_project_title


def test_one_sentence_entry_builds_short_theme_plus_form():
    title = derive_project_title(
        "给这个午餐包做一条自然一点的短视频，突出防水面料，风格参考你之前海边那条"
    )
    assert "午餐包" in title
    assert "短视频" in title
    assert len(title) <= 16
    assert "风格参考" not in title
    assert "防水面料" not in title


def test_form_only_prompt_does_not_stay_full_sentence():
    title = derive_project_title("做一条竖屏带货视频")
    assert title == "竖屏带货视频"


def test_theme_without_form_stays_short():
    title = derive_project_title("研究这个保温杯的表达方式")
    assert "保温杯" in title
    assert len(title) <= 16


def test_long_prompt_is_capped_and_not_first_forty_eight_chars():
    text = "帮我写一个非常非常长的需求说明" + "填充内容" * 20 + "关于登山包的图文"
    title = derive_project_title(text)
    assert len(title) <= 16
    assert title.endswith("…") or "登山包" in title or "图文" in title


def test_empty_prompt_falls_back_to_dated_placeholder():
    title = derive_project_title("   \n  ")
    assert title.startswith("作品")
    assert len(title) <= 16
