"""Seed a throwaway run so the界面 can be looked at without paying for it.

WHY THIS EXISTS: an interface change can only be judged by looking at it, and a
real run costs 20–24 万 token and two of Jeffery's clicks. So the four stages are
written here by hand into a throwaway data root, and he judges the screen for
free. Re-run it after every UI change.

WHAT IS REAL HERE: `Store`, `Pipeline`, `Ledger`, `STAGE_SPEC`, the artifact
files, the event log, the nonce gate, and every line of `timeline.py` /
`vocabulary.py` / `structured.py` / `materials.py` that turns them into a screen.
With `--media-root`, the images and clip are copied from an explicitly chosen
product folder. Without it, the fixture keeps its material references empty and
shows the missing-file state. This writes to its own data root and never touches
`J:/E-Business-Video-Data/us-video-studio`.

WHAT IS NOT REAL: the content. No model ran. The four documents, the reviews and
the ledger rows below are written by hand, so the numbers in the header are
fixture numbers, not spend. The product the prose describes is a 保温午餐包; if
media is supplied, those files are only stand-ins for testing the layout.

ONE DELIBERATE FAILURE: the last storyboard segment names a `.jpg` where the file
on disk is `.png` — a filename the model half-remembered instead of looked up,
which is exactly what `list_materials` exists to prevent. It has to be in here or
the whole degradation path is untested. It reads 「文件不在」, not a broken image.

Left in the state a real run reaches just before its second click: all four
artifacts on disk, both reviews recorded, the first gate approved, and the
writing gate sitting open with a `return` on it — so the override path is on
screen too.

A SECOND RUN sits one step earlier than that: ② is written and its review
passed, and his click has not happened. That is the only state where the gate
card grows the option buttons, so the in-place controls cannot be judged
without it.

A THIRD RUN is finished: all four stages written, both gates approved, nothing
waiting on him at all. That is the most common state a run is in — he spends
most of a run not being asked anything — and until it exists in here it is the
one state nobody can look at. It is also the only one where the rail has no
`needs` step and the page therefore has to be legible without an amber band
telling him where to look.

Three runs rather than one, because the three states draw three different pages
and no one of them can stand in for another:

    preview-layout-01      ④ 的门禁开着（审核退回）— override 路径在这里
    preview-direction-01   ② 的门禁开着（审核通过）— 选项按钮在这里
    preview-done-01        四个阶段都完成、两个门都批过 — 没有任何东西在等你

    python scripts/seed_preview.py [data-root] [--media-root PRODUCT-DIRECTORY]
"""
from __future__ import annotations

import copy
import argparse
import shutil
import sys
from pathlib import Path

#: Derived from this file, so the app runs whether the repo sits at J:/E-Business
#: or anywhere else.
APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
sys.stdout.reconfigure(encoding="utf-8")

from studio.connections import ConnectionStore
from studio.config import ROLES, Pricing, Settings  # noqa: E402
from studio.ledger import Ledger, LedgerEntry  # noqa: E402
from studio.pipeline import Pipeline  # noqa: E402
from studio.store import Store  # noqa: E402

#: Name on disk -> what it stands in for. Copied verbatim. The names are
#: deliberately ugly, because a real marketplace export is.
SHOTS = {
    "O1CN01GCUZu92HsPJSGuB3r_!!2214430619206-0-cib.jpg": "两档同框",
    "O1CN01XCRadi2HsPJSacqq2_!!2214430619206-0-cib.jpg": "装满的样子",
    "O1CN01XrQXPu2HsPJRwv4is_!!2214430619206-0-cib.jpg": "放进通勤包",
    "O1CN01sgvX9u2HsPJSsZSkw_!!2214430619206-0-cib.jpg": "三色并排",
    "O1CN01s7UJXI2HsPJSm8ibm_!!2214430619206-0-cib.jpg": "细节",
    "50233e7f-350e-406e-802f-1f1f734c1bef.png": "另一张细节",
    "2026 -.mp4": "一段实拍",
}

#: The filenames the artifacts below reference, in the order the shots use them.
SHOT_TWO, SHOT_FULL, SHOT_BAG, SHOT_COLORS, SHOT_DETAIL, SHOT_PNG, SHOT_CLIP = SHOTS

RUN = "preview-layout-01"
parser = argparse.ArgumentParser(description="Create a disposable offline Studio preview")
parser.add_argument("data_root", nargs="?", default="J:/E-Business-Video-Data/us-video-studio-preview")
parser.add_argument("--media-root", type=Path, help="optional local product folder containing every SHOTS file")
args = parser.parse_args()
ROOT = Path(args.data_root)
PRODUCT = args.media_root
if PRODUCT is not None:
    missing = [name for name in SHOTS if not (PRODUCT / name).is_file()]
    if missing:
        raise SystemExit(f"media root is missing {len(missing)} preview files: {PRODUCT}")

if ROOT.exists():
    raise SystemExit(f"Refusing to replace existing preview data: {ROOT}; choose a new directory")
ROOT.mkdir(parents=True)
# Preview must not inherit the new default Codex login or any real API key.
ConnectionStore(ROOT).save({
    "expected_revision": 0,
    "connections": [{"id": "offline-preview", "name": "离线预览（无模型调用）",
                     "provider": "openai_compat", "base_url": "http://127.0.0.1:9",
                     "pricing": {}}],
    "roles": {role: {"connection": "offline-preview", "model": "offline-preview"} for role in ROLES},
})

# The shelf, filled by hand — the first of the two ways in, before the run
# exists. `create_run` does not make this folder; `materials.py` does, on first
# upload. So a folder he filled himself has to be here first.
shelf = ROOT / "runs" / RUN / "materials"
shelf.mkdir(parents=True)
if PRODUCT is not None:
    for name in SHOTS:
        shutil.copy2(PRODUCT / name, shelf / name)
print(f"materials: {len(SHOTS) if PRODUCT is not None else 0} files -> {shelf}")

settings = Settings(
    data_root=ROOT,
    base_url="https://example.invalid/anthropic",
    budget_limit_usd=10.0,
    pricing=Pricing(source="预览用的假价目表", input_usd_per_mtok=0.28,
                    output_usd_per_mtok=0.42, as_of="2026-09-17"),
)
store = Store(ROOT)
pipeline = Pipeline(store)
ledger = Ledger(store, settings)

store.create_run(RUN, "保温午餐包 15 秒（界面预览）",
                 "给已采购的保温午餐包做一条 15 秒短视频，投 TikTok Shop 美国线。")

chat = [
    ("user", "给已经采购的保温午餐包做一条 15 秒的短视频，投 TikTok Shop 美国线。"),
    ("assistant", "好，我先让需求整理把你说的整理成一份明确的需求记录，没答的会单独标出来。"),
]
for role, text in chat:
    store.append_chat(RUN, role, text)

# ------------------------------------------------------------------ ① intake --

intake = {
    "facts": [
        {"label": "品", "value": "保温午餐包，三个颜色（深灰 / 米白 / 藏青），两个容量档", "source": "你的消息"},
        {"label": "采购量", "value": "15L 三色各 10 件、25L 三色各 20 件，共 90 件", "source": "你的消息"},
        {"label": "用途", "value": "一条 15 秒短视频，投 TikTok Shop 美国线", "source": "你的消息"},
        {"label": "可售日期", "value": "未确认", "source": "你没说，我没有替你填"},
    ],
    "constraints": [
        "15 秒，不能再长",
        "没有实拍素材——有没有官方产品图也没确认",
        "零预算，只能用已有素材",
    ],
    "open_questions": [
        {"question": "有没有实拍素材或者官方产品图？", "blocks": "没有画面素材，多数方向连开工都开不了。"},
        {"question": "主推哪个 SKU / 哪个容量档？", "blocks": "定不了主推，卖点就落不到具体的东西上。"},
        {"question": "什么时候能开始卖？", "blocks": "现在测不了转化，只能测角度成不成立。"},
        {"question": "目标观众是谁？", "blocks": "不填不会卡住流程，但可能直接否掉某些方向。"},
    ],
}

attempt = pipeline.start_attempt(RUN, "intake")
pipeline.submit(
    RUN, "intake", attempt.token,
    content=(
        "# intake — 保温午餐包 15 秒（US 线）\n\n"
        "## 1. 你说的\n\n"
        "给已经采购的保温午餐包做一条 15 秒的短视频，投 TikTok Shop 美国线。\n\n"
        "## 2. 已核实的事实\n\n"
        "- 三个颜色、两个容量档（15L / 25L），共 90 件\n"
        "- 用途：15 秒短视频，美国线\n\n"
        "## 3. 未确认（我没有替你填）\n\n"
        "目标观众、主推 SKU、可售日期、有没有素材——均为未确认。\n"
    ),
    summary="把你说的话整理成需求记录，四件事没答，单独标出来了。",
    evidence=["三个颜色、两个容量档、90 件——来自你的消息"],
    limitations=["可售日期、目标观众、主推 SKU、素材情况均未确认"],
    structured=intake,
)

store.append_chat(RUN, "assistant", "需求整理完了。有四件事没答，我放在顶部清单里了。接着让导出去出方向。")

# --------------------------------------------------------------- ② direction --

direction = {
    "options": [
        {"key": "A", "claim": "把外观/视觉当成唯一主张。可用且已核实的事实只有一条：三个颜色、两个容量档。",
         "audience_moment": "在信息流里消费带饭/日用品外观类内容、对颜色与生活质感有反应的人。",
         "hook": "只说画面，不给卖点、不给价格。",
         "gives_up": "放弃「讲清楚为什么值得买」，信息量最低；主张完全无法事前验证。",
         "evidence_strength": "弱", "difficulty": "中",
         "asset_needs": "零实拍不成立。连官方图也没有 → A 现在即失效。"},
        {"key": "B", "claim": "主张「先帮你决定 15L 还是 25L」，不主推单一 SKU，把两条容量线摆成一道选择题。",
         "audience_moment": "正在挑午餐包、在「小一点够用 / 大一点更能装」之间犹豫的人。",
         "hook": "把两档容量的差异变成一个待回答的问题。",
         "gives_up": "放弃外观吸引与情绪；对无购买意图的泛流量近乎无效。",
         "evidence_strength": "事实中—强 / 假设弱", "difficulty": "中",
         "asset_needs": "需两档官方产品图（不需实拍视频）。拿不到两档图 → B 不成立。"},
        {"key": "C", "claim": "主张不是给观众购买理由，而是测产线：没有实拍、没有官方图，能不能出一条 15 秒。",
         "audience_moment": "目标观众不是消费者，是分发端——判断这条线要不要继续的人。",
         "hook": "「一条没有产品画面的 15 秒」本身构成反差。",
         "gives_up": "放弃消费者说服力；跑不动只能说明形态不行，不能说明产品不行。",
         "evidence_strength": "事实最强 / 内容价值最弱", "difficulty": "低",
         "asset_needs": "零实拍成立，零官方图也成立。"},
    ],
    "recommended": "推荐 B",
    "recommended_because": (
        "主张锚在唯一被核实的产品差异上（两个容量档），不需要声称任何没看到的产品属性；"
        "三个未答项里只需要①，不需要②和③；拿到两档静态图即可开工，且「比较」本身就是内容。"
    ),
    "prerequisites": [
        "容量是美国午餐包买家的真实纠结点。零证据——没有评论、没有搜索词、没有竞品观察。",
        "至少能拿到两档官方产品图（不必实拍视频）。拿不到 → B 直接不成立，不是改投别条。",
    ],
    "dependency_matrix": [
        {"need": "①实拍素材 / 官方产品图", "options": {"A": True, "B": True, "C": False}},
        {"need": "②指定目标观众", "options": {"A": False, "B": False, "C": False}},
        {"need": "③主推 SKU", "options": {"A": True, "B": False, "C": False}},
    ],
    "open_questions": [
        {"question": "是否有实拍素材或官方产品图？", "blocks": "A 的存亡线；B 需要两档官方图；C 不受影响。"},
        {"question": "目标观众是谁？", "blocks": "三条都自带人群；但你一旦指定，可能直接否掉 A 或 B。"},
        {"question": "主推哪个 SKU？", "blocks": "只有 A 需要。B、C 不需要。"},
        {"question": "美国可售日期？", "blocks": "三条都不依赖「现在就买得到」，但都测不了转化。"},
        {"question": "「AI-first 录播」的确切含义？", "blocks": "影响 C 的可执行程度，不改变三条的主张。"},
        {"question": "平台名边界？", "blocks": "三条都不依赖平台名，但 writing 阶段会撞到，建议先答。"},
    ],
}

attempt = pipeline.start_attempt(RUN, "direction")
pipeline.submit(
    RUN, "direction", attempt.token,
    content=(
        "# direction — 午餐包 15 秒（US 线）\n\n"
        "## 1. 三条选项\n\n### A. 外观即理由（视觉认同型）\n\n"
        "- **角度**：把外观/视觉当成唯一主张。\n"
        "- **证据强度**：弱。外观值不值得看，零证据。\n\n"
        "### B. 两个容量档的二选一（决策减负型）\n\n"
        "- **角度**：主张「先帮你决定 15L 还是 25L」，不主推单一 SKU。\n"
        "- **证据强度**：事实层面中—强（90 件采购，唯一被核实的产品差异）；假设层面弱。\n\n"
        "### C. 零物料下限测试（形态自测型）\n\n"
        "- **角度**：测产线，不给观众购买理由。\n"
        "- **素材依赖**：零实拍成立，零官方图也成立。\n\n"
        "## 2. 机制差异\n\n三条的说服路径不同，对未答项的依赖方向也不同。\n\n"
        "## 3. 推荐：B\n\n理由、代价与前提见下。\n\n"
        "## 4. 未决问题\n\n六条，见结构化视图。\n"
    ),
    summary="三个方向摊开比，推荐 B，附两个必须成立的前提和六个没解决的问题。",
    evidence=["90 件采购（15L 三色各 10、25L 三色各 20），来自 intake §3"],
    limitations=["容量是不是真实纠结点，零证据", "有没有官方产品图未确认"],
    structured=direction,
)

pipeline.record_review(
    RUN, "direction", verdict="pass",
    findings=[
        {"kind": "unverified", "issue": "「推荐理由只由已核实事实构成」这个表述会被下游读成效果也被证据背书，建议区分「证据安全」与「角度有效」。"},
        {"kind": "unverified", "issue": "B 的素材可行性依赖两个未验证条件：两档官方图拿不拿得到、纯静态图 + 信息卡能不能撑起 15 秒。"},
        {"kind": "gap", "issue": "三条的成功判据都不可在现阶段验证——没有转化数据。"},
        {"kind": "scope", "issue": "A 的更正写得比结论长，占了推荐段一半篇幅。"},
    ],
)

nonce = pipeline.issue_nonce(RUN, "direction")
pipeline.record_approval(RUN, "direction", nonce=nonce, decision="approved", selected_option_id="B")

store.append_chat(RUN, "user", "就 B 走。素材那边你看着办。")
store.append_chat(RUN, "assistant", "好。让素材统筹把 B 需要的镜头排出来。")

# ----------------------------------------------------------- ③ material_plan --

material = {
    "items": [
        {"shot": "15L 与 25L 同框，平放并排", "status": "已有", "purpose": "让「两档差多少」变成一眼能看出来的事",
         "note": "官方图里有一张同框，分辨率够切开场", "file": SHOT_TWO},
        {"shot": "25L 装下一整份便当 + 水杯 + 水果盒", "status": "已有", "purpose": "把「大一点更能装」落到具体物品上",
         "file": SHOT_FULL},
        {"shot": "15L 放进标准通勤双肩包侧袋", "status": "已有", "purpose": "回答「我平时背的包放不放得下」",
         "file": SHOT_BAG},
        {"shot": "三个颜色并排色卡", "status": "已有", "purpose": "补足颜色信息，不需要实拍",
         "note": "官方图里有三色并排的一张，直接用", "file": SHOT_COLORS},
        {"shot": "内衬 / 保温层剖面对比", "status": "无法获得", "purpose": "证明保温效果",
         "note": "需要拆一只，且采购量只有 90 件，拆一只少一只；不拆就没有这个画面。目录里没有这张，所以不填 file。"},
        {"shot": "真人从包里取出午餐包的使用场景", "status": "不确定", "purpose": "给内容一个人味儿的锚点",
         "note": "取决于你能不能出镜或者有没有人可用——这一点你没有说过。目录里也没有，不填 file。"},
    ],
    "gaps": [
        {"gap": "官方图里没有剖面，也没有真人使用场景", "consequence": "保温证据只能靠文案说，不能靠画面证；这条片子说服力到此为止。"},
        {"gap": "拆不拆一只做剖面？", "consequence": "不拆就没有保温画面；拆了 90 件变 89 件。"},
    ],
    "open_questions": [
        {"question": "肯不肯拆一只做保温剖面？", "blocks": "不拆就没有保温证据，只能靠文案声称。"},
        {"question": "要不要补拍真人使用场景？", "blocks": "不补就全片都是产品图，没有人味儿。"},
    ],
}

attempt = pipeline.start_attempt(RUN, "material_plan")
pipeline.submit(
    RUN, "material_plan", attempt.token,
    content=(
        "# material_plan — B 方案（US 线）\n\n"
        "## 素材清单\n\n六个镜头：四个用目录里已有的官方图，两个目录里没有。\n\n"
        "## 缺口\n\n剖面与真人场景没有物料，全片因此没有保温证据。\n"
    ),
    summary="六个镜头，四个用目录里已有的官方图，两个没有；没有保温证据。",
    evidence=["镜头清单对应 direction 的 B 方案", "四个镜头的 file 取自 runs/<id>/materials/ 的实际文件名"],
    limitations=["剖面与真人场景没有物料，保温只能靠文案声称"],
    structured=material,
)

store.append_chat(RUN, "assistant", "素材排完了：六个镜头，四个用目录里现成的官方图，两个没有。接着写脚本。")

# ---------------------------------------------------------------- ④ writing --

writing = {
    "total_duration": "15 秒（0:00–0:15）",
    "segments": [
        # The one shot with a clip instead of a photo. Its bar draws a frame at
        # 0.5s with a play badge, which is the only way to see that videos work
        # without spending anything.
        {"tc": "0:00–0:03", "picture": "两只包并排平放，居中，只看得见轮廓和大小差",
         "line": "（无台词）", "sound": "一句低频提示音", "duration": "3s", "file": SHOT_CLIP},
        {"tc": "0:03–0:07", "picture": "25L 被塞进便当、水杯、水果盒，一件件落进去",
         "line": "25L 装得下这些。", "sound": "物件落袋的实录音", "duration": "4s", "file": SHOT_FULL},
        {"tc": "0:07–0:10", "picture": "15L 滑进通勤双肩包侧袋，包带出画",
         "line": "15L 放得进你每天背的那个包。", "sound": "拉链声", "duration": "3s", "file": SHOT_BAG},
        {"tc": "0:10–0:13", "picture": "两档并排 + 三色色卡，信息卡节奏",
         "line": "两档，三个颜色。", "sound": "节奏卡点", "duration": "3s", "file": SHOT_COLORS},
        # Deliberately wrong: `.jpg` where the file on disk is `.png`. This is
        # the exact failure `list_materials` exists to prevent — a filename the
        # model half-remembered instead of one it looked up. It must render as
        # 「文件不在」 and not as a broken image, so it stays in the fixture.
        {"tc": "0:13–0:15", "picture": "两档同框定格，画面下方留价格位",
         "line": "先想清楚你要哪一种。", "sound": "收尾音", "duration": "2s",
         "file": "50233e7f-350e-406e-802f-1f1f734c1bef.jpg"},
    ],
    "assumptions": [
        "官方图有四张可用，第五张想用细节图收尾——目录里那张细节图是竖构图，可能撑不满",
        "不拆包做剖面，因此全片不出现保温证据",
        "价格位留白，可售日期未定之前不放价格",
    ],
    "open_questions": [
        {"question": "结尾要不要放价格？", "blocks": "可售日期没定，现在放价格等于给一个会过期的数字。"},
        {"question": "平台名要不要出现？", "blocks": "脚本目前完全没提平台，如果投放需要点名，结尾要改。"},
    ],
}

attempt = pipeline.start_attempt(RUN, "writing")
pipeline.submit(
    RUN, "writing", attempt.token,
    content=(
        "# writing — B 方案 15 秒\n\n"
        "## 分镜\n\n五段，总时长 15 秒。\n\n"
        "## 假设\n\n两档官方图拿得到、可以拍实拍、不拆包。\n"
    ),
    summary="五段分镜铺满 15 秒，开场只给大小差、不喊卖点。",
    evidence=["镜头结构对应 material_plan 的六个镜头"],
    limitations=["收尾用的细节图在目录里找不到同名文件，可能拿不到", "全片没有保温证据"],
    structured=writing,
)

# A `return` here, so the second gate shows the override path rather than the
# plain one. Nothing downstream depends on it — writing is the last stage.
pipeline.record_review(
    RUN, "writing", verdict="return",
    findings=[
        {"kind": "unsupported", "issue": "结尾「先想清楚你要哪一种」把选择推给观众，但全片没有给任何帮他选的依据——两档的差别只在容量数字上，没有落到「什么情况下选哪个」。"},
        {"kind": "gap", "issue": "全片不出现保温证据，但产品叫「保温午餐包」。观众看完不知道它保不保温。"},
        {"kind": "unverified", "issue": "0:13–0:15 挂的那张收尾图，文件名在素材目录里找不到。要么名字写错了，要么图根本不在。"},
    ],
)

# ------------------------------------------------------------------- ledger --
#
# Fixture numbers. Shaped like a real run so the header and the gate card have
# something to read, but nothing was spent producing this run.
for role, stage, tokens in [
    ("orchestrator", "intake", 48_365),
    ("intake", "intake", 12_400),
    ("director", "direction", 37_790),
    ("reviewer", "direction", 26_253),
    ("material", "material_plan", 41_820),
    ("writer", "writing", 52_610),
]:
    store.append_ledger(RUN, LedgerEntry(
        at="2026-09-17T12:00:00Z", run_id=RUN, role=role, stage=stage,
        model="deepseek-flash", input_tokens=tokens, output_tokens=0,
        estimated_usd=tokens / 1_000_000 * 0.28, pricing_source="预览用的假价目表",
    ))

# ------------------------------------------- run 2: the ② gate still open --
#
# ② is written and the review passed; his click has not happened. This is the
# only state in which `_pending_action` fills in `options`, so it is the only
# way to look at the option buttons without paying for a run.
#
# Written as its own run rather than by rolling run 1 back: run 1 is the state
# *after* the click and is the only place the override wording appears. Neither
# run can stand in for the other. The two documents below are the same payloads
# run 1 uses — deep-copied so a future edit to one run cannot silently change
# the other's fixture.

RUN_TWO = "preview-direction-01"
SHELF_TWO = ROOT / "runs" / RUN_TWO / "materials"
SHELF_TWO.mkdir(parents=True)
if PRODUCT is not None:
    for name in SHOTS:
        shutil.copy2(PRODUCT / name, SHELF_TWO / name)

store.create_run(RUN_TWO, "保温午餐包 15 秒（②门禁预览）",
                 "给已采购的保温午餐包做一条 15 秒短视频，投 TikTok Shop 美国线。")

for role, text in [
    ("user", "先把方向出了，我看完再定。"),
    ("assistant", "好，让导出去出三条。出来我把三个选项放在门禁卡上给你点。"),
]:
    store.append_chat(RUN_TWO, role, text)

attempt = pipeline.start_attempt(RUN_TWO, "intake")
pipeline.submit(
    RUN_TWO, "intake", attempt.token,
    content=(
        "# intake — 保温午餐包 15 秒（US 线）\n\n"
        "## 1. 你说的\n\n"
        "给已经采购的保温午餐包做一条 15 秒的短视频，投 TikTok Shop 美国线。\n\n"
        "## 2. 未确认（我没有替你填）\n\n"
        "目标观众、主推 SKU、可售日期、素材情况——四件事没答。\n"
    ),
    summary="把你说的话整理成需求记录，四件事没答，单独标出来了。",
    evidence=["三个颜色、两个容量档、90 件——来自你的消息"],
    limitations=["可售日期、目标观众、主推 SKU、素材情况均未确认"],
    structured=copy.deepcopy(intake),
)

store.append_chat(RUN_TWO, "assistant", "需求整理完了，四件事没答。接着让导出去出方向。")

attempt = pipeline.start_attempt(RUN_TWO, "direction")
pipeline.submit(
    RUN_TWO, "direction", attempt.token,
    content=(
        "# direction — 午餐包 15 秒（US 线）\n\n"
        "## 三条选项\n\n"
        "A 外观即理由 / B 两个容量档的二选一 / C 零物料下限测试。\n\n"
        "## 推荐：B\n\n理由、代价与两个必须成立的前提见结构化视图。\n\n"
        "## 未决问题\n\n六条，见结构化视图。\n"
    ),
    summary="三个方向摊开比，推荐 B，附两个必须成立的前提和六个没解决的问题。",
    evidence=["90 件采购（15L 三色各 10、25L 三色各 20），来自 intake"],
    limitations=["容量是不是真实纠结点，零证据", "有没有官方产品图未确认"],
    structured=copy.deepcopy(direction),
)

# A pass, so the card offers the choice. A `return` here would suppress the
# option buttons on purpose — the gate then asks whether the document stands at
# all — and run 1 already covers what that card looks like.
pipeline.record_review(
    RUN_TWO, "direction", verdict="pass",
    findings=[
        {"kind": "unverified", "issue": "B 的素材可行性依赖两个未验证条件：两档官方图拿不拿得到、纯静态图 + 信息卡能不能撑起 15 秒。"},
        {"kind": "gap", "issue": "三条的成功判据都不可在现阶段验证——没有转化数据。"},
    ],
)

# No `record_approval` call, and none is missing: the click is his, and this run
# exists precisely because it has not happened.

store.append_chat(RUN_TWO, "assistant",
                  "三条方向好了，审核通过。三个选项我放在门禁卡上了——点一个我就往下派。")

for role, stage, tokens in [
    ("orchestrator", "intake", 46_120),
    ("intake", "intake", 11_800),
    ("director", "direction", 39_240),
    ("reviewer", "direction", 24_905),
]:
    store.append_ledger(RUN_TWO, LedgerEntry(
        at="2026-09-17T12:00:00Z", run_id=RUN_TWO, role=role, stage=stage,
        model="deepseek-flash", input_tokens=tokens, output_tokens=0,
        estimated_usd=tokens / 1_000_000 * 0.28, pricing_source="预览用的假价目表",
    ))

# ---------------------------------------- run 3: nothing is waiting on him --
#
# The state a run spends most of its life in, and the one the rail has no amber
# step for. Same four documents as run 1, with the ④ gate approved as well — so
# the difference between this run and run 1 is exactly one click, which is the
# point.

RUN_THREE = "preview-done-01"
SHELF_THREE = ROOT / "runs" / RUN_THREE / "materials"
SHELF_THREE.mkdir(parents=True)
if PRODUCT is not None:
    for name in SHOTS:
        shutil.copy2(PRODUCT / name, SHELF_THREE / name)

store.create_run(RUN_THREE, "保温午餐包 15 秒（全部完成）",
                 "给已采购的保温午餐包做一条 15 秒短视频，投 TikTok Shop 美国线。")

for role, text in [
    ("user", "给已经采购的保温午餐包做一条 15 秒的短视频，投 TikTok Shop 美国线。"),
    ("assistant", "好，我先让需求整理把你说的话整理成需求记录，没答的单独标出来。"),
]:
    store.append_chat(RUN_THREE, role, text)

attempt = pipeline.start_attempt(RUN_THREE, "intake")
pipeline.submit(
    RUN_THREE, "intake", attempt.token,
    content=(
        "# intake — 保温午餐包 15 秒（US 线）\n\n"
        "## 1. 你说的\n\n"
        "给已经采购的保温午餐包做一条 15 秒的短视频，投 TikTok Shop 美国线。\n\n"
        "## 2. 未确认（我没有替你填）\n\n"
        "目标观众、主推 SKU、可售日期、素材情况——四件事没答。\n"
    ),
    summary="把你说的话整理成需求记录，四件事没答，单独标出来了。",
    evidence=["三个颜色、两个容量档、90 件——来自你的消息"],
    limitations=["可售日期、目标观众、主推 SKU、素材情况均未确认"],
    structured=copy.deepcopy(intake),
)

store.append_chat(RUN_THREE, "assistant", "需求整理完了，四件事没答。接着让导出去出方向。")

attempt = pipeline.start_attempt(RUN_THREE, "direction")
pipeline.submit(
    RUN_THREE, "direction", attempt.token,
    content=(
        "# direction — 午餐包 15 秒（US 线）\n\n"
        "## 三条选项\n\n"
        "A 外观即理由 / B 两个容量档的二选一 / C 零物料下限测试。\n\n"
        "## 推荐：B\n\n理由、代价与两个必须成立的前提见结构化视图。\n"
    ),
    summary="三个方向摊开比，推荐 B，附两个必须成立的前提和六个没解决的问题。",
    evidence=["90 件采购（15L 三色各 10、25L 三色各 20），来自 intake"],
    limitations=["容量是不是真实纠结点，零证据", "有没有官方产品图未确认"],
    structured=copy.deepcopy(direction),
)

pipeline.record_review(
    RUN_THREE, "direction", verdict="pass",
    findings=[
        {"kind": "unverified", "issue": "B 的素材可行性依赖两个未验证条件：两档官方图拿不拿得到、纯静态图 + 信息卡能不能撑起 15 秒。"},
        {"kind": "gap", "issue": "三条的成功判据都不可在现阶段验证——没有转化数据。"},
    ],
)

nonce = pipeline.issue_nonce(RUN_THREE, "direction")
pipeline.record_approval(RUN_THREE, "direction", nonce=nonce, decision="approved", selected_option_id="B")

store.append_chat(RUN_THREE, "user", "就 B 走。素材那边你看着办。")
store.append_chat(RUN_THREE, "assistant", "好。让素材统筹把 B 需要的镜头排出来。")

attempt = pipeline.start_attempt(RUN_THREE, "material_plan")
pipeline.submit(
    RUN_THREE, "material_plan", attempt.token,
    content=(
        "# material_plan — B 方案（US 线）\n\n"
        "## 素材清单\n\n六个镜头：四个用目录里已有的官方图，两个目录里没有。\n\n"
        "## 缺口\n\n剖面与真人场景没有物料，全片因此没有保温证据。\n"
    ),
    summary="六个镜头，四个用目录里已有的官方图，两个没有；没有保温证据。",
    evidence=["镜头清单对应 direction 的 B 方案", "四个镜头的 file 取自 runs/<id>/materials/ 的实际文件名"],
    limitations=["剖面与真人场景没有物料，保温只能靠文案声称"],
    structured=copy.deepcopy(material),
)

store.append_chat(RUN_THREE, "assistant", "素材排完了：六个镜头，四个用目录里现成的官方图，两个没有。接着写脚本。")

attempt = pipeline.start_attempt(RUN_THREE, "writing")
pipeline.submit(
    RUN_THREE, "writing", attempt.token,
    content=(
        "# writing — B 方案 15 秒\n\n"
        "## 分镜\n\n五段，总时长 15 秒。\n\n"
        "## 假设\n\n两档官方图拿得到、可以拍实拍、不拆包。\n"
    ),
    summary="五段分镜铺满 15 秒，开场只给大小差、不喊卖点。",
    evidence=["镜头结构对应 material_plan 的六个镜头"],
    limitations=["收尾用的细节图在目录里找不到同名文件，可能拿不到", "全片没有保温证据"],
    structured=copy.deepcopy(writing),
)

# A pass here, unlike run 1's return. Run 1 exists to show what an open gate
# looks like when the reviewer refused the document; this one exists to show
# what the page looks like when there is nothing to decide at all, so both
# gates have to be shut.
pipeline.record_review(
    RUN_THREE, "writing", verdict="pass",
    findings=[
        {"kind": "gap", "issue": "全片不出现保温证据，但产品叫「保温午餐包」。观众看完不知道它保不保温。"},
        {"kind": "unverified", "issue": "0:13–0:15 挂的那张收尾图，文件名在素材目录里找不到。要么名字写错了，要么图根本不在。"},
    ],
)

nonce = pipeline.issue_nonce(RUN_THREE, "writing")
pipeline.record_approval(
    RUN_THREE, "writing", nonce=nonce, decision="approved",
    note="收尾图那张我确认过目录里没有，先按现在这条走，图我回头补。",
)

store.append_chat(RUN_THREE, "user", "就这样，脚本收了。")
store.append_chat(RUN_THREE, "assistant",
                  "四个阶段都定了，两个门你都批过了。这条链路走完，没有下一步在等你。")

for role, stage, tokens in [
    ("orchestrator", "intake", 48_365),
    ("intake", "intake", 12_400),
    ("director", "direction", 37_790),
    ("reviewer", "direction", 26_253),
    ("material", "material_plan", 41_820),
    ("writer", "writing", 52_610),
    ("reviewer", "writing", 28_140),
]:
    store.append_ledger(RUN_THREE, LedgerEntry(
        at="2026-09-17T12:00:00Z", run_id=RUN_THREE, role=role, stage=stage,
        model="deepseek-flash", input_tokens=tokens, output_tokens=0,
        estimated_usd=tokens / 1_000_000 * 0.28, pricing_source="预览用的假价目表",
    ))

print("seeded:", ROOT)
print("run 1 :", RUN)
print("state : ①②③④ 都已产出，①的门禁已通过，④的门禁开着（审核退回）")
print("run 2 :", RUN_TWO)
print("state : ①② 已产出，②的门禁开着（审核通过）—— 选项按钮在这里")
print("run 3 :", RUN_THREE)
print("state : ①②③④ 都已产出，两个门都批过 —— 没有任何东西在等你")
