# Studio 本地创意包编译契约

使用 `read_method_document(name="creative-package-contract")` 读取本文。使用 `compile_creative_package` 编译，不需要 shell，也不调用模型、生成、上传或发布服务。字段校验通过不等于内容成立、参考图获批、费用授权或用户采用。

先用 `read_artifact` 读取准确产物，工具末尾提供完整 `artifact:<stage>-v<N>#<sha256>`；不要补全截断的指纹。用 `list_materials` 读取本 run 实际素材名、sha256 和已登记观察。文件存在不等于观察过；引用只能支持实际已见的事实。缺少商品身份或证据时先说明缺口，不填虚构内容凑通过。

工具外层必须含 `format`（video/photo）、`stage`（intake/direction/material_plan/writing）、`version`（正整数）、`artifact_sha256`（64 位实际指纹）、`manifest`（下述完整对象）。编译格式必须与 `prepare_creative_context` 明确选择的格式相同。外层绑定不能拿最新版替代用户实际看的旧版本。生成的新包保留 manifest、库快照/库指纹和绑定的产物身份，可从返回文件 URL 阅读。

## 视频 manifest

`schema_version=1`，`market="US"`。以下全部为非空字符串：`id`, `product_family_id`, `purpose`, `audience_situation`, `current_alternative`, `buying_hesitation`, `belief_change`, `speaker_relationship`, `form_reason`, `opening_reason_to_watch`, `payoff`, `failure_condition`。

- `audience_status` 为 `hypothesis` 或 `source_supported`。后者还必须有非空 `audience_source`，引用已读证据。
- `reference_mode="references"` 时，`reference_decisions` 必须非空。每项为 `{evidence:"case-id/evidence-id",decision:"adapt"或"reject",reason:"具体理由",transfer_limit:"具体迁移边界"}`。只能引用实际检索到、属于 calibration 且有 message/storyboard 用途资格的案例证据，不可拿 holdout、偏好锚点或其他商品的事实填入。
- 无可用参考时可明确选择 `reference_mode="original_product_facts"`，填写非空 `reference_reason` 并令 `reference_decisions=[]`。这只是无参考的事实原创模式，不代表创意已验证。
- `product_facts` 为至少一项的数组，每项含唯一非空 `id`、事实 `statement`、来源 `source`、证据局限 `limit`。`source` 只能为当前 run 的实际素材 `material:文件名` 或真实产物 `artifact:阶段-v版本#完整sha256`。不得填写外部 URL、任意路径或虚构已经读过的来源。
- `scenes` 至少一项。每项 `id` 唯一且非空；`start/end` 为有限数值，首段 start=0，后段 start 等于上段 end，每段 end>start。时长是创作假设，不能以填数证明表演成立。
- 每段必须有非空 `visual`, `new_information`, `viewer_can_infer`, `delivery`, `action_end_state`；`product_fact_ids` 为引用已有 fact id 的数组，可为空。
- 每段 `speech_mode` 明确为 `on_camera`、`voiceover` 或 `none`。前两者必须填写非空 `spoken_text` 与 `speech_function`；`on_camera` 还要求 `face_visible=true`。`none` 不得有非空 spoken_text。

下例仅演示结构，产品/观众/动作均为假设。将所有占位符替换为当前产物的真实内容和指纹，不能原样提交或冒充业务结论：

```json
{
  "schema_version": 1,
  "market": "US",
  "id": "current-concept-v1",
  "product_family_id": "REPLACE_WITH_CURRENT_FAMILY",
  "purpose": "让观众理解已核实的一个使用理由",
  "audience_status": "hypothesis",
  "audience_situation": "待验证的当前使用处境",
  "current_alternative": "待核实的原有办法",
  "buying_hesitation": "当前产物提出的购买顾虑",
  "belief_change": "希望通过可见证据改变的一个判断",
  "speaker_relationship": "无口播；由使用动作建立关系",
  "form_reason": "这一处证据适合用实际动作展示",
  "opening_reason_to_watch": "开场直接呈现一个具体问题",
  "payoff": "动作完成后可看见结果",
  "failure_condition": "素材无法证明该结果时停止制作",
  "reference_mode": "original_product_facts",
  "reference_reason": "本稿未采用学习案例，仅依据当前产物核实的事实",
  "reference_decisions": [],
  "product_facts": [{
    "id": "f1",
    "statement": "REPLACE_WITH_OBSERVED_PRODUCT_FACT",
    "source": "artifact:intake-v1#REPLACE_WITH_FULL_SHA256",
    "limit": "只覆盖已观察的这项属性，不推出性能或市场效果"
  }],
  "scenes": [{
    "id": "s1", "start": 0, "end": 4,
    "visual": "用已登记素材呈现这一项事实",
    "new_information": "观众得到当前问题的一个具体答案",
    "viewer_can_infer": "仅推断画面实际支持的内容",
    "delivery": "自然动作，无口播；声音效果尚未验证",
    "action_end_state": "动作完成且结果可见",
    "speech_mode": "none", "spoken_text": "",
    "product_fact_ids": ["f1"]
  }]
}
```

视频包输出 `brief.json`, `library-snapshot.json`, `prompt.txt`, `manifest.json`, `review.md`。它是可复核生成输入，不直接执行。Studio 的「制作」入口另存准确 prompt、脚本与参考图版本，完成制作检查，并由用户分别确认图审和费用后提交；编译包的存在不授予执行权限。

## 图文 manifest

`schema_version=1`，`market="US"`，`placement="shoppable_photo"`，`music="publish-time-on-TikTok"`。当前只支持自然挂车图文，不套用付费轮播或商品详情页规则，也不合成音频或 MP4。

- `product` 必须有非空 `product_family_id`, `offer_id`, `variant`。必须来自当前商品记录，不得虚构供应身份。
- `product.sources` 至少一项，每项含唯一非空 `id`、非空 `role`、本 run 素材原始文件名 `path`、实际文件 `sha256`。Studio 不接受绝对路径、相对目录、URL 或其他 run 文件；内部会解析为受控路径再给编译器。
- `strategy` 必须有非空 `audience_situation`, `desired_belief`, `reason_to_buy`, `voice`；`method_references` 与 `limitations` 均为非空字符串数组。引用实际加载的方法与具体局限，不能用方法证明商品事实。
- `publish_caption` 必须非空，与图内文字分开表达购买理由与态度。
- `pages` 为有序数组，当前编译器接受 2–12 页，这是本地包约束，不是平台规则，也不表示创作一律应为固定页数。
- 每页 `id` 唯一，匹配 `[a-z0-9][a-z0-9-]{0,39}`。非空字段 `overlay_text`（最多 180 字符）、`alt_text`, `purchase_role`, `image_prompt`；`evidence_refs` 为非空数组且每项必须对应 product.sources 的 id。
- 没有最终图片时 `status="planned"` 且省略 `image`，产物会明确显示待生成，不能冒充已完成图片。
- 已有图片时 `status="bound"`，`image={path:"当前素材文件名",sha256:"实际完整指纹",kind:"existing"或"generated",provenance:"真实来源说明"}`。图片必须是可解码 PNG/JPEG/WebP。绑定不表示图片已审核、图内字已合成或获得生成授权。

下例也是结构示例；全部占位与假设必须换成当前 run 的真实记录：

```json
{
  "schema_version": 1, "market": "US",
  "placement": "shoppable_photo", "music": "publish-time-on-TikTok",
  "product": {
    "product_family_id": "REPLACE_WITH_CURRENT_FAMILY",
    "offer_id": "REPLACE_WITH_VERIFIED_OFFER",
    "variant": "REPLACE_WITH_VERIFIED_VARIANT",
    "sources": [{"id": "p1", "role": "当前商品外观证据", "path": "REPLACE_WITH_MATERIAL_FILENAME", "sha256": "REPLACE_WITH_FULL_SHA256"}]
  },
  "strategy": {
    "audience_situation": "待验证的当前使用处境",
    "desired_belief": "基于可见证据形成的一个判断",
    "reason_to_buy": "当前产物已说明的购买理由",
    "voice": "直接表达理由，不假装有未发生的使用经历",
    "method_references": ["us-photo-copy：逐页各回答一个购买问题"],
    "limitations": ["仅有静态外观证据，性能与效果未验证"]
  },
  "publish_caption": "REPLACE_WITH_CAPTION_GROUNDED_IN_CURRENT_BRIEF",
  "pages": [
    {"id": "page-1", "status": "planned", "overlay_text": "REPLACE_WITH_FIRST_PAGE_COPY", "alt_text": "第一页面向具体购买问题", "purchase_role": "建立与当前处境的关系", "image_prompt": "依据当前已观察商品外观策划首图，不增加未知属性", "evidence_refs": ["p1"]},
    {"id": "page-2", "status": "planned", "overlay_text": "REPLACE_WITH_SECOND_PAGE_COPY", "alt_text": "第二页呈现支持理由的细节", "purchase_role": "回答一个具体购买顾虑", "image_prompt": "依据相同商品来源展示已观察细节，不虚构性能", "evidence_refs": ["p1"]}
  ]
}
```

图文包输出 `pack.json`, `caption.txt`, `prompts.md`, `index.html`；bound 图片逐字节复制并校验，planned 图片保持待生成。编译错误会返回具体原因供修正；不要通过虚构来源、换 hash 或删除问题掩盖缺证据。
