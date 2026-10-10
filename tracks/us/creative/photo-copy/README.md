# US 图文带货：图片与文案

2026-09-18。根据 Jeffery 最新澄清，本板块核心是带方法依据的带货图片＋文案；音乐直接在 TikTok 发布端配。本目录取代“先合成图文音乐视频”的工作重点，旧 [photo-music 技术试验](../photo-music/README.md)仅保留历史，不继续制作配乐或视频。

## 已实现

[photo_copy_pack.py](../../../../scripts/photo_copy_pack.py) 将助手整理的策划编译成独立本地审阅 HTML、完整 JSON、逐页提示词/文案 Markdown、发布 caption 文本。每页保留顺序、图内文字、alt text、购买作用、商品证据引用与图像生成 prompt；图片可为明确标注 `planned` 的待生成页，也可通过 `bind` 绑定已有成图，生成新版本再审阅。工具本身不调用模型，不把提示词存在当作已出图，也不自动把文案绘制进图片。

依赖 Python 和现有 Pillow。**不需要 FFmpeg、音乐文件、许可音乐字段或镜头时长。** `music` 固定为 `publish-time-on-TikTok`；账号入口、平台音乐可用性与发布决定留在真实发布阶段处理，不从本工具推断。

## 方法在出图前落地

助手负责从 [案例学习](../learning/README.md) 选择适用证据与反例，填写四项创作依据：观众的具体处境、希望改变的判断、购买理由、说话方式。逐页回答“本页增加什么购买理解、哪个可见证据支持”。不能把同一商品多角度图自动当成多个购买理由，也不能把无证据的温控、容量、装载和优惠写入文案。

首图要给继续看的理由，中间图兑现所提期待，末页交代所指商品和下一步；这是一组待验证设计问题，不是固定三页/四页模板。本地编译器只检查字段、来源与图片状态，**不会认定回答有洞察、文案种草或创意通过**。新图像生成仍由现有生图工作流执行，用户反馈和实际表现回到同一案例对照记录。

## 数据配方

以下为格式示例，`ABSOLUTE_*` 和 hash 需替换真实文件；不是已存在图片或已获认可方案。

```json
{
  "schema_version": 1,
  "market": "US",
  "placement": "shoppable_photo",
  "music": "publish-time-on-TikTok",
  "product": {
    "product_family_id": "LUN",
    "offer_id": "1069569802551",
    "variant": "25L Gray",
    "sources": [
      {"id": "gray-exterior", "path": "ABSOLUTE_SOURCE_PATH", "sha256": "EXACT_SHA256", "role": "已查看的同款外观；不证明容量或保温"}
    ]
  },
  "strategy": {
    "audience_situation": "助手基于案例提出的具体处境，注明假设",
    "desired_belief": "希望改变的一个购买判断",
    "reason_to_buy": "商品为何能帮助该处境及证据边界",
    "voice": "怎样对这类人说话以及为什么",
    "method_references": ["案例/方法来源及采用条件"],
    "limitations": ["尚未验证的创意或商品判断"]
  },
  "publish_caption": "拟用英文发布文案，不含未证实优惠",
  "pages": [
    {"id": "cover", "status": "planned", "overlay_text": "拟用图内文案", "alt_text": "拟生成图片的无障碍说明", "purchase_role": "首图为什么值得继续看", "evidence_refs": ["gray-exterior"], "image_prompt": "绑定商品身份、真实结构、画面关系和拟用文字的完整生图指令"},
    {"id": "detail", "status": "planned", "overlay_text": "第二页图内文案", "alt_text": "第二页图片说明", "purchase_role": "第二页兑现什么、增加什么", "evidence_refs": ["gray-exterior"], "image_prompt": "第二页完整生图指令，不虚构商品性能"}
  ]
}
```

当前编译器仅支持 `placement: shoppable_photo`（自然挂车图文）。旧配方未写该字段时，在新输出 JSON 与 HTML 明确补入此用途，原配方不被修改。`paid_carousel` 或 `product_listing` 将被拒绝，需使用各自用途规则，不能把付费轮播或 PDP 主图要求悄然套用到自然图文。

当前编译器支持2–12页，这是本地工具范围，**不是平台页数规则**。`product.sources` 保存原始本地商品资料及 SHA256；每页 `evidence_refs` 必须指向这些来源。图像 prompt 还需要助手对照图内文字与实际意图审查，程序不会假装理解并认证它。

## 执行与绑定图片

在仓库根目录执行，运行资料放 Git 外：

```powershell
$py = 'J:/E-Business-Video-Data/US/workbench-pilot-v1/runtime-venv/Scripts/python.exe'
$root = 'J:/E-Business-Video-Data/US/photo-copy-pilot-20260918'
& $py scripts/photo_copy_pack.py validate --manifest "$root/plan-v1.json"
& $py scripts/photo_copy_pack.py compile --manifest "$root/plan-v1.json" --out "$root/review-v1"
# 后续已有成图时，绑定指定页；不生成、不改图、不覆盖旧配方：
& $py scripts/photo_copy_pack.py bind --manifest "$root/plan-v1.json" --page cover --image 'ABSOLUTE_FINAL_IMAGE_PATH' --kind generated --provenance '实际生成run与版本；审核状态另记' --output "$root/plan-v2.json"
& $py scripts/photo_copy_pack.py compile --manifest "$root/plan-v2.json" --out "$root/review-v2"
& $py -m unittest discover -s tests -p test_photo_copy_pack.py -v
```

`bind` 校验 PNG/JPEG/WebP 可解码、记录图片 hash/来源，保留上一版 manifest hash；重绑同页时保留旧图片历史。`existing`/`generated`描述来源，`bound`仅表示图片已绑定，**不代表用户已审、图内文字已正确生成或商业使用权已获确认**。编译时将图像原字节复制到交付目录，HTML 相对链接可整体搬到现有工作台。原图不被编辑，旧目录不覆盖。

交付前仍需实际看每张成图：商品是否一致、文字是否真实出现在图里且可读、逐页有没有信息增量、caption是否承接，alt text是否准确对应实际输出。未来绑定图片后，不应继续把计划里的 alt text 当成实际图像描述而不复核。工具生成的 `assessment` 始终不宣称创意或图片审核通过；用户的真实审核记录继续由现有工作流保存。
