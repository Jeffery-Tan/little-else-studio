r"""Seed isolated, offline creator-UX states in a new data root.

All creative text, review decisions, approvals, media, and usage are synthetic
fixture data. The records use the application's Store/Pipeline/production event
contracts; they are not a real customer project or a claim that a model ran.
The optional server injects providers that refuse every model/generation call.

    python scripts/seed_ux_rework_preview.py "$env:TEMP\studio-ux-preview-<unique>" --serve

The destination must not exist and must be outside the Git repository. To
restart the read-only preview server without reseeding, use --serve-existing.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

APP = Path(__file__).resolve().parent.parent
REPOSITORY = APP.parents[1]
sys.path.insert(0, str(APP))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from studio.config import Pricing, ROLES, Settings  # noqa: E402
from studio.connections import ConnectionStore  # noqa: E402
from studio.ledger import Ledger  # noqa: E402
from studio.materials import accept_upload  # noqa: E402
from studio.models import utcnow  # noqa: E402
from studio.pipeline import Pipeline  # noqa: E402
from studio.production import DraftIn, prepare_draft, validate_video  # noqa: E402
from studio.store import Store  # noqa: E402


RUNS = (
    ("ux-old-prose", "旧项目回看", "历史方向与脚本是原文，阅读后再决定是否修改。"),
    ("ux-direction-pending", "新方向待选择", "选择一条创意方向，团队才会继续写稿。"),
    ("ux-direction-adopted", "方向已采用", "已采用方向 B；现在要看接下来如何写成脚本。"),
    ("ux-writing-pending", "阅读并修改脚本", "阅读台词、画面与发布文案，并指出要修改的地方。"),
    ("ux-visual-script", "视觉短片脚本", "不用展开详情也能读懂无口播的开场、变化和结尾。"),
    ("ux-image-review", "图片待确认", "核对这张参考图，确认后才能用于视频。"),
    ("ux-video-prepare", "图片已确认，准备视频", "基于已确认的这张图继续准备视频输入，生成仍须另行确认。"),
    ("ux-video-ready", "视频可生成", "核对准确参考图、制作输入与费用，再明确启动视频。"),
    ("ux-video-completed", "视频已完成", "播放并核对视频，然后查看发布文案。"),
    ("ux-video-retry-failed", "新尝试失败，旧片仍在", "对照上一份可播放结果和本次失败，再决定如何修改。"),
    ("ux-video-unknown", "生成结果待核实", "核实原视频任务，避免重复提交和费用。"),
    ("ux-analysis-only", "只分析参考", "阅读已保存的参考拆解，不自动进入制作。"),
    ("ux-reference-in-project", "已有项目只分析新参考", "最终仍要交付视频；本轮只阅读新参考分析。"),
)

BRIEF = (
    "为美国线一款便携午餐包准备 TikTok Shop 竖屏短视频。"
    "需要能看清商品和真实使用场景，目标是一条可核对的视频与英文发布文案。"
    "这是离线 UX 验收数据；商品效果、受众判断和创意判断均不是市场结论。"
)

DIRECTION = {
    "options": [
        {"key": "A", "title": "出门前十秒", "claim": "把装午餐的早晨动作拍成一段轻快开场。", "satisfies": "goal.primary",
         "audience_moment": "早上准备带饭，正在收拾随身物品的人。",
         "hook": "手在桌前把午餐放进包里，立刻看到包的大小。",
         "story": "先放入午餐，再提起包走向门口；商品始终可见。",
         "gives_up": "不做价格比较。", "evidence_strength": "仅演示已拍到的放置动作",
         "difficulty": "中", "asset_needs": "需要实物和真实动作镜头。"},
        {"key": "B", "title": "带上你的午餐", "claim": "让包在通勤途中自然出现，建立带饭情境。", "satisfies": "goal.primary",
         "audience_moment": "准备出门上班，想把午餐与日常物品一起带走的人。",
         "hook": "先见午餐盒，再见它被放入包中。",
         "story": "装好、提起、走出门；用三个动作讲完整使用场景。",
         "gives_up": "不声明未经核实的保温时长。", "evidence_strength": "仅演示画面可见的动作",
         "difficulty": "低", "asset_needs": "需要准确商品图或实拍。"},
        {"key": "C", "title": "桌上有什么", "claim": "把桌面整理与午餐包的出现作为画面推进。", "satisfies": "goal.primary",
         "audience_moment": "午休前整理东西的人。",
         "hook": "凌乱桌面先入画，商品最后落位。",
         "story": "逐件整理物品，最后展示带走的整体画面。",
         "gives_up": "不使用人物表演。", "evidence_strength": "画面概念，未验证效果",
         "difficulty": "中", "asset_needs": "需要多个可核对的物件。"},
    ],
    "recommended": "B",
    "recommended_because": "动作少，商品出现早；可见行为能支撑文案，不依赖未证实的性能。",
    "prerequisites": ["商品形状、颜色与使用方式必须与真实素材一致。"],
}

WRITING = {
    "segments": [
        {"tc": "0–3s", "duration": "3 秒", "line": "Lunch is packed. Let's go.",
         "picture": "午餐盒放入打开的包中，商品正面清楚可见。", "sound": "拉链与轻快环境声。"},
        {"tc": "3–7s", "duration": "4 秒", "line": "Everything I need is right here.",
         "picture": "提起包走向门口；不展示未经核实的容量或保温效果。", "sound": "脚步与自然口播。"},
        {"tc": "7–12s", "duration": "5 秒", "line": "Ready for the day.",
         "picture": "定格在手持午餐包的完整画面，留出结尾呼吸。", "sound": "音乐轻落。"},
    ],
    "total_duration": "约 12 秒（演示素材仅 2 秒，用于检查播放布局）",
    "caption": "Pack lunch. Head out. Keep your day moving. #LunchRoutine",
    "assumptions": ["本 fixture 的媒体是合成测试画面；真实商品信息和成片质量尚未核对。"],
}

VISUAL_WRITING = {
    "segments": [
        {"tc": "0:00–0:03", "duration": "约 3 秒", "line": "",
         "picture": "特写：午餐盒停在桌边，手把敞开的午餐包推入画面；先让观众看清物件与包的相对大小。",
         "sound": "餐盒轻触桌面、拉链滑开；这段明确不使用口播。"},
        {"tc": "0:03–0:07", "duration": "约 4 秒", "line": "",
         "picture": "午餐盒进入包内；手整理肩带，镜头跟随这一动作转到门边，画面保持商品清楚可见。",
         "sound": "物件声接到轻节拍，人物不说话。"},
        {"tc": "0:07–0:10", "duration": "约 3 秒", "line": "",
         "picture": "门边短暂停顿，手提起包，镜头交代从桌面到出门的变化。",
         "sound": ""},
        {"tc": "0:10–0:14", "duration": "约 4 秒", "line": "Lunch is coming with me.",
         "picture": "人物走出门，最后一帧停在完整的包与手部动作，商品没有被裁出画面。",
         "sound": "一句简短自然口播后只留脚步与环境声。"},
    ],
    "total_duration": "约 14 秒；仅为离线脚本阅读示例，没有实际成片。",
    "caption": "Lunch is coming with me. #LunchRoutine",
    "assumptions": ["以上动作和声音是合成创意示例；第三段没有口播或声音记录，不能推定为刻意静音。"],
}

OLD_WRITING = """# 历史脚本原文（离线合成长稿）

这是一份保留原格式的旧稿示例，用来检查长文阅读、准确版本与反馈定位。它不是模型对真实商品的观察，也不代表已拍摄或已批准。

## 作品目标与口径

为美国线午餐包写一条约 30 秒的日常短片，交付可阅读的画面、动作、口播、声音和英文发布文案。画面只演示实际动作；尺寸、容量、保温时长和使用感都还没有商品证据，不能在片中变成确定主张。此处的场景、角色、物件和台词均为隔离预览的合成内容。

## 开场：从桌面进入早晨

0:00–0:03。画面从普通桌面开始：钥匙、未封口的午餐盒与空包各在不同位置，手把午餐盒推到画面中央。不要先用品牌标语或抽象生活方式画面占满前三秒；观众需要先辨认当天正在收拾午餐。声音是室内环境声与餐盒轻碰桌面的声音，不安排夸张音效。若拍摄素材没有完整桌面，不要用 AI 假装已有实拍。

0:03–0:06。镜头保持相近视角，手拉开包口，午餐盒靠近包。台词候选为“Lunch is packed. Let's go.”；若动作本身足够清楚，可以只保留环境声，口播是否采用由实际剪辑决定。此处只表达“正在装午餐”，不表达可装多少份或保温多久。

## 中段：让商品参与动作

0:06–0:10。午餐盒进入包中，手整理包带。画面须留出包的完整轮廓与开口，不用特写把物件和包的关系剪断。制作前要核实镜头使用的商品颜色和结构是否对应本项目素材。若真实物件装不进包，应重写动作，而不是靠镜头裁切制造装得下的错觉。

0:10–0:14。手提起包，身体转向门口。镜头功能是把桌面动作接到出门，不是证明承重。声音为拉链、衣料摩擦与脚步，音乐只作节奏支撑；如果同期声不可用，后期应明确使用拟音，不能把它称为实录。

0:14–0:18。画面给一个短暂停顿，包与其他随身物品同时进入镜头。台词候选为“Everything I need is right here.”，但这句容易暗示容量和适配性；在未核实物件尺寸前不要使用。可替换为只描述画面的字幕或取消台词，让动作承接段落。这个取舍需要在确认商品与道具之后再定。

## 收束：给观众可见的结束

0:18–0:22。人物到门边拿起钥匙，包保持在画面里，动作向外延续。若前段节奏偏快，这里留半秒让观众看见商品；若前段已足够清楚，不再重复拉链或包口特写。声音延续脚步和门边环境，不引入突然变大的音乐。

0:22–0:26。镜头跟随出门或走廊方向，避免把商品变成一闪而过的背景。这里不拍虚构通勤地点，也不把角色的个人体验改写成普遍功效。整段可以无口播；若有字幕，也只说“Lunch is coming with me.” 这种与动作一致的话。

0:26–0:30。最后一帧是手提包的完整画面，留出能看清外观的停顿，再结束。不要用未验证的价格、赠品或性能做收尾承诺。声音自然落下；英文发布文案在下方单独给出，片内不必重复全部文字。

## 需要制作团队继续核对

- 商品真实外观、开合方式、颜色及与午餐盒的大小关系；本稿没有替代商品图审。
- 镜头是否具备从桌面到门口的连贯动作；缺素材时应标记缺口，不能把描述当作已有画面。
- 口播与环境声的最终取舍；没有声音记录不等于刻意无口播，也不等于生成的视频已有同期声。
- 时长是分镜假设，实际剪辑可能改变节奏。任何修改都要对照本版原文与依赖核对。

## 英文发布文案草案

Pack lunch. Head out. Keep the day moving. #LunchRoutine

文案只说画面能够支撑的日常动作，不声明具体容量、保温时长或用户使用结果。以上旧稿按原文字节回看；阅读体验可以变，但不能补写结构化字段或迁移批准。
"""


def png_bytes(label: str) -> bytes:
    image = Image.new("RGB", (540, 960), "#d9e0dd")
    draw = ImageDraw.Draw(image)
    draw.rectangle((92, 310, 448, 720), fill="#314b51", outline="#20353b", width=8)
    draw.rounded_rectangle((134, 242, 406, 348), radius=35, fill="#314b51", width=6)
    draw.rectangle((154, 382, 386, 418), fill="#c9d3cc")
    draw.text((90, 96), "OFFLINE UX PREVIEW", fill="#20353b")
    draw.text((90, 132), label[:30], fill="#20353b")
    draw.text((90, 828), "Synthetic image - not product evidence", fill="#20353b")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def video_bytes() -> bytes:
    """Build a tiny, fully decodable local MP4; never call a generation model."""
    with tempfile.TemporaryDirectory(prefix="studio-ux-video-") as directory:
        output = Path(directory) / "preview.mp4"
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-f", "lavfi", "-i", "color=c=#314b51:s=360x640:r=15:d=2",
                   "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                   "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(output)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=False)
        except FileNotFoundError as exc:
            raise SystemExit("需要 ffmpeg 才能生成可播放的离线验收视频。") from exc
        if result.returncode:
            raise SystemExit("ffmpeg 无法创建离线预览视频：" + result.stderr[-1200:])
        raw = output.read_bytes()
    validate_video(raw)
    return raw


def submit(pipeline: Pipeline, run_id: str, stage: str, content: str, structured: dict) -> None:
    attempt = pipeline.start_attempt(run_id, stage, actor="fixture")
    pipeline.submit(run_id, stage, attempt.token, content=content, summary=content.splitlines()[0],
                    evidence=["离线 UX fixture；非真实商品证据"],
                    limitations=["所有创意内容均为合成演示"], structured=structured, actor="fixture")


def approve(pipeline: Pipeline, run_id: str, stage: str, *, selected: str | None = None) -> None:
    pipeline.record_review(run_id, stage, verdict="pass", findings=[],
                           expected_artifact_sha256=pipeline.stage_state(run_id, stage).latest.sha256)
    nonce = pipeline.issue_nonce(run_id, stage)
    pipeline.record_approval(run_id, stage, nonce=nonce, decision="approved",
                             selected_option_id=selected, note="离线 UX fixture 决定；并非用户真实批准")


def seed_stages(pipeline: Pipeline, run_id: str, *, stop: str,
                old: bool = False, visual: bool = False) -> None:
    submit(pipeline, run_id, "intake",
           "# 需求整理\n\n目标是一条美国线午餐包短视频与英文 caption。真实商品主张待核对。",
           {"facts": [{"label": "交付", "value": "竖屏视频与英文发布文案", "source": "fixture brief"}],
            "constraints": ["不能把合成示例画面当作商品证据。"]})
    direction_text = (
        "# 创意方向（历史原文）\n\n## A. 出门前十秒\n装好午餐，提起包出门。"
        "\n\n## B. 带上你的午餐\n先看午餐，再看它进包，最后走向门口。"
        "\n\n## C. 桌上有什么\n物件依次整理，包在最后出现。"
        "\n\n推荐 B，因为商品出现较早。此历史版本没有结构化选项卡。"
        if old else "# 创意方向\n\nA 出门前十秒；B 带上你的午餐；C 桌上有什么。推荐 B。")
    submit(pipeline, run_id, "direction", direction_text, {} if old else DIRECTION)
    if stop == "direction_pending":
        pipeline.record_review(run_id, "direction", verdict="pass", findings=[])
        return
    approve(pipeline, run_id, "direction", selected=None if old else "B")
    if stop == "direction_adopted":
        return
    submit(pipeline, run_id, "material_plan",
           "# 素材安排\n\n需要核对商品参考图；测试图片仅供界面验收。",
           {"items": [{"shot": "商品入包", "purpose": "看清动作", "status": "需要生成",
                       "note": "fixture 模拟", "file": ""}]})
    script_text = OLD_WRITING if old else "# 当前脚本\n\n完整台词、画面与发布文案见结构化作品。"
    submit(pipeline, run_id, "writing", script_text, {} if old else VISUAL_WRITING if visual else WRITING)
    if stop == "writing_pending":
        pipeline.record_review(run_id, "writing", verdict="pass", findings=[])
        return
    approve(pipeline, run_id, "writing")


def revise_writing(pipeline: Pipeline, run_id: str) -> None:
    revised = {**WRITING, "caption": "A revised caption for the next video. #LunchRoutine"}
    submit(pipeline, run_id, "writing", "# 返工脚本\n\n这份新版发布文案尚未对应已生成视频。", revised)
    approve(pipeline, run_id, "writing")


def draft_for(store: Store, pipeline: Pipeline, run_id: str, kind: str,
              reference: dict | None = None, *, variant: str = "") -> dict:
    source = store.latest_artifact(run_id, "writing")
    payload = DraftIn(request_id=f"{run_id}-{kind}-draft{variant}", kind=kind,
                      prompt=("OFFLINE UX PREVIEW: synthetic lunch tote, three visible actions, "
                              "no product performance claim; no real generation requested. "
                              f"Variation: {variant or 'original'}."),
                      source_version=source.version, source_sha256=source.sha256,
                      reference=reference, model="wan3.0-video" if kind == "video" else "codex-image",
                      duration=12 if kind == "video" else None,
                      resolution="720P" if kind == "video" else None)
    draft = prepare_draft(store, pipeline, run_id, payload, actor="fixture")
    store.append_event(run_id, "production_review", draft_id=draft["draft_id"],
                       draft_sha256=draft["draft_sha256"],
                       review={"verdict": "ready", "findings": ["仅供离线 UX 验收；未调用审核模型。"],
                               "checks": {key: "fixture 数据；真实内容未审核" for key in
                                          ("opening", "progression", "payoff", "product", "execution")},
                               "at": utcnow(), "model": "offline-fixture", "scope": "合成数据"})
    return draft


def production_attempt(store: Store, ledger: Ledger, run_id: str, draft: dict,
                       status: str, output: bytes | None = None, *, number: int = 1) -> None:
    attempt_id = f"{run_id}-attempt-{number}"
    store.append_event(run_id, "production_attempt", attempt_id=attempt_id,
                       draft_id=draft["draft_id"], draft_sha256=draft["draft_sha256"],
                       media_kind=draft["kind"], status="preparing", message="离线 fixture 记录。",
                       output=None, endpoint="https://offline.cn-beijing.maas.aliyuncs.com/api/v1"
                       if draft["kind"] == "video" else None)
    ledger.begin_media_call(run_id, call_id=attempt_id,
                            model=draft["model"], provider="fixture",
                            usage_unit="video_seconds" if draft["kind"] == "video" else "images")
    if output is not None:
        suffix = ".mp4" if draft["kind"] == "video" else ".png"
        name = f"production-{hashlib.sha256(attempt_id.encode()).hexdigest()[:24]}{suffix}"
        registered = accept_upload(store, run_id, name, output)
        store.append_event(run_id, "production_output", attempt_id=attempt_id,
                           output={"name": name, "sha256": hashlib.sha256(output).hexdigest(),
                                   "url": registered["url"]})
        ledger.settle_media_call(run_id, attempt_id, quantity=2 if draft["kind"] == "video" else 1,
                                 ok=True, note="合成 UX fixture 用量；非提供方结算", pricing_source="offline-fixture")
        store.append_event(run_id, "production_status", attempt_id=attempt_id,
                           update={"status": "succeeded", "message": "合成测试结果已保存；请核对画面。"})
    else:
        message = ("离线 fixture：第二次尝试失败，未返回媒体；上一份结果仍保留。"
                   if status == "failed" else "提交结果待核实，请先查原任务，不要重新提交。")
        store.append_event(run_id, "production_status", attempt_id=attempt_id,
                           update={"status": status, "message": message})


def seed_reference_task(store: Store, run_id: str, source: dict, clip: bytes,
                        *, creative_project: bool = False) -> None:
    """Attach an analysis request to either a reference-only or creative run."""
    reference_id = f"{run_id}-reference"
    store.append_event(run_id, "reference_video_started", reference_id=reference_id,
                       request={"intent": "analyze", "name": source["name"],
                                "expected_sha256": hashlib.sha256(clip).hexdigest(),
                                "instruction": ("本轮只拆解参考；不修改本项目已有作品。" if creative_project
                                                else "只拆解参考，不开始制作。"),
                                "source_url": None})
    store.append_event(run_id, "reference_video_source", reference_id=reference_id,
                       source={"name": source["name"], "sha256": hashlib.sha256(clip).hexdigest(),
                               "origin": "user_upload", "acquired_at": utcnow()})
    store.append_event(run_id, "reference_video_ready", reference_id=reference_id,
                       analysis_id=f"{run_id}-offline-analysis",
                               report=("参考视频拆解（离线合成示例）\n\n"
                                       "1. 开场先给可见物，再进入动作。\n"
                                       "2. 注意力由动作转换推进，最后停在完整画面。\n\n"
                                       "限制：该片是合成测试视频；没有真实参考观察，不能当成商品或创意证据。"
                                       + ("本轮仅分析参考，原项目的最终交付仍是商品视频与文案。"
                                          if creative_project else "这次只交付拆解报告，不进入制作。")))
    store.append_event(run_id, "reference_video_completed", reference_id=reference_id)


def seed(root: Path) -> None:
    if root.exists():
        raise SystemExit(f"拒绝覆盖已有目录：{root}")
    if root.resolve().is_relative_to(REPOSITORY.resolve()):
        raise SystemExit(f"验收数据必须在 Git 仓库外：{root}")
    # Validate the local playable media before writing even the empty root.
    clip = video_bytes()
    picture = png_bytes("Lunch tote reference")
    result_picture = png_bytes("Generated image pending review")
    root.mkdir(parents=True)
    ConnectionStore(root).save({
        "expected_revision": 0,
        "connections": [{"id": "offline-ux", "name": "离线 UX 预览（拒绝模型调用）",
                         "provider": "openai_compat", "base_url": "http://127.0.0.1:9", "pricing": {}}],
        "roles": {role: {"connection": "offline-ux", "model": "offline-ux"} for role in ROLES},
    })
    store = Store(root)
    pipeline = Pipeline(store)
    settings = Settings(data_root=root, base_url="http://127.0.0.1:9",
                        pricing=Pricing(source="offline-fixture", input_usd_per_mtok=None,
                                        output_usd_per_mtok=None, as_of="2026-09-24"))
    ledger = Ledger(store, settings)
    for run_id, title, target in RUNS:
        analysis_only = run_id == "ux-analysis-only"
        reference_in_project = run_id == "ux-reference-in-project"
        brief = ("只分析我上传的参考视频，给我一份观察、可借鉴方法与限制。"
                 "这次不做午餐包创意、脚本、图片或视频。" if analysis_only else BRIEF)
        # The existing creative project's original brief must not absorb the
        # later, analysis-only request; that request lives in chat/task events.
        run_brief = brief if reference_in_project else brief + "\n\n当前验收目标：" + target
        store.create_run(run_id, f"UX 验收 · {title}", run_brief)
        store.append_chat(run_id, "user", "请只分析这条参考视频，不开始制作。" if analysis_only
                          else "请按当前项目目标完成这条午餐包作品。")
        store.append_chat(run_id, "assistant", "这是一份离线 UX fixture，所有内容与决定均为合成演示。")
        reference_source = (accept_upload(store, run_id, "reference-preview.mp4", clip)
                            if analysis_only or reference_in_project else None)
        if analysis_only:
            seed_reference_task(store, run_id, reference_source, clip)
            continue
        store.append_event(run_id, "creative_context_selected",
                           selection={"intent": "new", "format": "video", "tags": [], "case_ids": None})
        reference = None
        if run_id in {"ux-video-prepare", "ux-video-ready", "ux-video-completed", "ux-video-retry-failed", "ux-video-unknown"}:
            uploaded = accept_upload(store, run_id, "lunch-tote-reference.png", picture)
            reference = {"name": uploaded["name"], "sha256": hashlib.sha256(picture).hexdigest()}
        stop = {"ux-direction-pending": "direction_pending",
                "ux-direction-adopted": "direction_adopted",
                "ux-writing-pending": "writing_pending",
                "ux-visual-script": "writing_pending"}.get(run_id, "complete")
        seed_stages(pipeline, run_id, stop=stop, old=(run_id == "ux-old-prose"),
                    visual=(run_id == "ux-visual-script"))
        if reference_in_project:
            store.append_chat(run_id, "user", "这轮只分析刚上传的新参考；原来的视频目标和脚本先保留，不开始制作。")
            seed_reference_task(store, run_id, reference_source, clip, creative_project=True)
            continue
        if stop != "complete" or run_id == "ux-old-prose":
            continue
        if run_id == "ux-image-review":
            draft = draft_for(store, pipeline, run_id, "image")
            production_attempt(store, ledger, run_id, draft, "succeeded", result_picture)
            continue
        if run_id == "ux-video-prepare":
            store.append_event(run_id, "production_material_approval", name=reference["name"],
                               sha256=reference["sha256"], approved=True, actor="fixture",
                               nonce_sha256=hashlib.sha256(b"offline-ux-fixture").hexdigest())
            continue
        draft = draft_for(store, pipeline, run_id, "video", reference)
        store.append_event(run_id, "production_material_approval", name=reference["name"],
                           sha256=reference["sha256"], approved=True, actor="fixture",
                           nonce_sha256=hashlib.sha256(b"offline-ux-fixture").hexdigest())
        if run_id == "ux-video-completed":
            production_attempt(store, ledger, run_id, draft, "succeeded", clip)
        elif run_id == "ux-video-retry-failed":
            production_attempt(store, ledger, run_id, draft, "succeeded", clip)
            revise_writing(pipeline, run_id)
            revised = draft_for(store, pipeline, run_id, "video", reference, variant="-revised")
            production_attempt(store, ledger, run_id, revised, "failed", number=2)
        elif run_id == "ux-video-unknown":
            production_attempt(store, ledger, run_id, draft, "succeeded", clip)
            revise_writing(pipeline, run_id)
            revised = draft_for(store, pipeline, run_id, "video", reference, variant="-unknown")
            production_attempt(store, ledger, run_id, revised, "unknown", number=2)


class OfflineText:
    def complete(self, *args, **kwargs):
        raise RuntimeError("离线 UX fixture 拒绝模型调用")


class OfflineImage:
    def capability(self):
        return {"available": True, "verified": False, "reason": "离线 UX fixture；生成会在调用前拒绝"}

    def generate(self, *args, **kwargs):
        from studio.agents.codex_client import CodexUnavailable
        raise CodexUnavailable("离线 UX fixture 不进行图片生成")


class OfflineVideo:
    endpoint = "https://offline.cn-beijing.maas.aliyuncs.com/api/v1"

    def submit(self, *args, **kwargs):
        from studio.agents.wan_video import WanVideoError
        raise WanVideoError("离线 UX fixture 不进行视频生成", submission_unknown=False)


def serve(root: Path, port: int) -> None:
    import uvicorn
    from studio.server import build_app

    settings = Settings(data_root=root, base_url="http://127.0.0.1:9",
                        pricing=Pricing(source="offline-fixture", input_usd_per_mtok=None,
                                        output_usd_per_mtok=None, as_of="2026-09-24"))
    app = build_app(settings, client_factory=OfflineText,
                    production_image_factory=OfflineImage,
                    production_video_factory=OfflineVideo)
    print(f"离线 UX 预览：http://127.0.0.1:{port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_root", type=Path, help="尚不存在的 Git 仓库外目录")
    parser.add_argument("--serve", action="store_true", help="播种后启动仅本地、拒绝模型调用的预览服务")
    parser.add_argument("--serve-existing", action="store_true", help="只重启本脚本播种的现有根，不覆盖")
    parser.add_argument("--port", type=int, default=4762)
    args = parser.parse_args()
    root = args.data_root.expanduser().resolve()
    if args.serve_existing:
        if not all((root / "runs" / run_id / "run.json").is_file() for run_id, _, _ in RUNS):
            raise SystemExit(f"已有目录不是完整的 {len(RUNS)} 状态 UX fixture；不会修改或启动。")
    else:
        seed(root)
    print("数据根：", root)
    for run_id, title, _ in RUNS:
        print(f"  {run_id:24} {title}")
    if args.serve or args.serve_existing:
        serve(root, args.port)


if __name__ == "__main__":
    main()
