"""Rule-only project titles from creator prompts; no model calls."""
import pytest

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


def test_url_before_product_request_does_not_become_the_theme():
    request = 'https://www.tiktok.com/@example/video/123456789\n给这个午餐包做一条带货视频'
    title = derive_project_title(request)
    assert title == derive_project_title('给这个午餐包做一条带货视频')
    assert '午餐包' in title and '带货视频' in title
    assert 'https' not in title and '123456789' not in title


def test_url_path_does_not_determine_form_or_product_name():
    title = derive_project_title('https://example.com/带货视频/午餐包 研究这个保温杯的表达方式')
    assert '保温杯' in title and '午餐包' not in title and '带货视频' not in title


def test_bare_source_links_use_neutral_placeholder_without_inventing_purpose():
    title = derive_project_title('https://example.com/video/1\nhttps://example.com/video/2')
    assert title.startswith('作品') and len(title) <= 16
    assert '参考' not in title and '商品' not in title


def test_url_followed_by_chinese_punctuation_preserves_product_request():
    assert derive_project_title('https://example.com/video/1，给这个午餐包做一条带货视频') == derive_project_title('给这个午餐包做一条带货视频')


def test_later_prohibitions_do_not_add_a_form_to_the_theme():
    assert derive_project_title('图片事实边界验收。只分析这两张图片，不要创意、脚本或图片、视频。') == '图片事实边界验收'
    assert derive_project_title('图片事实边界验收，不要视频，也不要写稿') == '图片事实边界验收'
    assert derive_project_title('https://example.com/video/123\n图片事实边界验收。不要视频') == '图片事实边界验收'
    assert derive_project_title('午餐包带货视频，后面不用图文') == '午餐包带货视频'


def test_actual_attachment_head_names_product_instead_of_upload_instruction():
    text = '用附上的灰色午餐包商品图，做美国 TikTok 的自然带货图文，共三页并带一段英文发布 caption。'
    original = text
    assert derive_project_title(text) == '灰色午餐包'
    assert text == original


@pytest.mark.parametrize('head', [
    '用附上的灰色午餐包商品图',
    '使用附件里的灰色午餐包商品图',
    '根据上传的灰色午餐包照片',
    '参考我上传的灰色午餐包图片',
    '请用附件中的灰色午餐包产品图',
    '请帮我用附件里面的灰色午餐包商品图',
    '我附上的灰色午餐包商品图',
    '随附的灰色午餐包素材',
    '查看附上的灰色午餐包的商品图',
])
def test_explicit_attachment_reference_preserves_named_topic(head):
    assert derive_project_title(head + '，不要视频，也不要音乐') == '灰色午餐包'


@pytest.mark.parametrize('head', [
    '用附上的商品图', '使用附件里的图片', '根据上传的照片',
    '参考附上的素材', '用附上的商品图片', '用附上的图',
    '附件', '附上', '用附件', '查看随附的视频',
])
def test_attachment_without_a_topic_uses_neutral_placeholder(head):
    title = derive_project_title(head + '，不要视频，也不要创意')
    assert title.startswith('作品') and len(title) <= 16
    assert '附上' not in title and '附件' not in title and '视频' not in title


def test_url_and_attachment_indications_do_not_replace_real_theme():
    text = 'https://example.com/视频/产品图\n用附件里的灰色午餐包商品图，先不做视频'
    assert derive_project_title(text) == '灰色午餐包'


def test_attachment_prefix_does_not_erase_a_named_theme_or_its_own_form():
    assert derive_project_title('用附上的午餐包带货图文，别做视频') == '午餐包图文'
    assert derive_project_title('附件收纳包带货视频，不要图文') == '附件收纳包带货视频'
    assert derive_project_title('用附上的地图，先不做视频') == '地图'


def test_manual_or_saved_titles_are_not_rederived_by_project_reads(tmp_path):
    from studio.store import Store
    store = Store(tmp_path)
    brief = '用附上的灰色午餐包商品图，做美国 TikTok 的自然带货图文'
    for run_id, title in [('manually-named', '我的手工名称'), ('historically-named', '附上')]:
        store.create_run(run_id, title, brief)
        before = (store.run_dir(run_id) / 'run.json').read_bytes()
        derive_project_title(brief)
        assert store.read_run(run_id).title == title
        assert store.read_run(run_id).brief == brief
        assert (store.run_dir(run_id) / 'run.json').read_bytes() == before
