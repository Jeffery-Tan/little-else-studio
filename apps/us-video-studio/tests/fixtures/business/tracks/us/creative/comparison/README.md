# US / Oumomo 与本方生成流程的可观察对照

2026-09-18。用户授权落实案例学习、工作流改进及此前记录的分支。本模块已实现本地收件、hash 核验、媒体参数检查、已有 ASR 关联和前后台提示词文本差异；已用真实旧素材运行[首个受限基线](baseline-001.md)。未调用 Oumomo、视频模型或付费分析，没有恢复 Gym 制作。

**目标是复现可观察的好效果，解释输入到输出的变化。不是声称识别了黑盒内部架构。** 给链接能解析视频，不足以区分下载器、浏览器、缓存、API 或其他方式。视频编码、分辨率和提示词相似也不能证明使用某个隐藏模型。

## 已有基线与运行位置

- T01 原参考、Oumomo 旧成片、本方 R15 remake v1 失败原片均复用原字节，无新下载或重编码。
- 两份既有 ASR 已与源媒体 hash 校验；本方实际最终提示词与历史结果也进入对照。
- 运行数据：`J:/E-Business-Video-Data/US/creative-comparison-20260918/`。原媒体保留原位置，不重复复制；完整提示词、运行 JSON、ASR 文本和报告均在 Git 外。
- 基线状态 `not_ready_for_same_input_comparison`：已经完成受限比较，但没有 Oumomo 的同一请求输入、前后台提示词和参数，不能当作同输入实验。
- 三份素材属于已有参考复刻链，不在[学习库](../learning/README.md)计算为三个新增独立成功案例。

## 下一条案例怎样接入

先创建收件表，再用 `hash` 命令登记本地文件。用户提供的整包材料由助手整理，不要求用户手工填 JSON。记录 `case_id`、各端 run id，并明确“这个输入确实属于这一次输出”的回执或用户陈述文件；不要只因两个视频相像就自动关联。

```powershell
python scripts/creative_comparison.py init next-oumomo-example --output J:/E-Business-Video-Data/US/creative-comparison-20260918/next-intake.json
python scripts/creative_comparison.py hash J:/path/to/reference.mp4
python scripts/creative_comparison.py compare J:/E-Business-Video-Data/US/creative-comparison-20260918/next-intake.json --output-dir J:/E-Business-Video-Data/US/creative-comparison-20260918/next-v1
python -m unittest discover -s tests -p test_creative_comparison.py -v
```

命令不联网、不生成、不安装依赖；比较真实媒体时使用本机现有 `ffprobe`。输出目录必须在 Git 工作区之外；文件只能新建，后续版本使用新目录。退出码 `0` 表示材料齐备可做可观察比较，`2` 表示报告已生成但同输入条件未齐，`1` 表示数据/完整性/工具错误。`0` 不等于内容通过或因果关系成立。

核心字段：

| 字段 | 登记方式与约束 |
|---|---|
| `reference_video` | 顶层为研究参考原片；每个 run 单独登记实际提交版本，裁切/重编码版本不能冒充原字节 |
| `reference_images` | 按实际提交顺序保存 hash；`null + unknown` 表示未知，`[] + confirmed_none` 才表示确认没有图片 |
| `user_prompt` / `backend_prompt` | 分别保存原文文件，不能用后台指令冒充用户原始需求；齐全时生成文本 diff |
| `output_video` | 对应此次请求的原成片；剪辑版另建 run，不覆盖原片 |
| `input_binding_evidence` | 该请求与全部输入/输出的关联证据文件；CLI 校验文件完整性，真实性仍须人工核对 |
| `model_parameters` | `unknown / partial / documented`，已知值必须绑定资料文件；隐藏参数继续未知，不靠视频猜。未知参数限制因果归因，不阻止材料齐备后的可观察比较 |
| `reference_asr` / `output_asr` | JSON 必须包含匹配的媒体 `sha256` 与 `segments`；仅支持语义和候选时段，不代替听评 |
| `observations` | 绑定来源 hash，区分历史报告、ASR 语义及用户反馈，说明适用范围；不自动生成审美评分 |

## 材料齐备后真正比较什么

1. **参考提取**：同一参考中的受众情境、承诺、可见证明、人物关系和声音组织，哪些进入了用户输入/后台指令，哪些丢失；逐条引用证据，未知开场不补写。
2. **策划转译**：后台新增/删掉了什么；区分购买理由、表演方式、分镜动作和工具约束，检查是否把“达人分享”改成“功能演示”。文本差异只是定位工具，不能直接给效果结论。
3. **输出兑现**：实际画面是否兑现利益，口播是否有具体理由，商品结构与动作是否成立；可靠原速声画审核缺位时保留未知。
4. **受控迁移**：本方先用相同原始素材与用户目标改策划，明确不可控模型、随机种子、参考预处理等差异。新增付费制作另获具体授权；一次好结果不叫稳定复现。
5. **回写学习库**：只纳入经过实际观察的条件式方法，同时带失败对照；不把品牌名或一个技巧固化成成功秘诀。

## 最少待补材料

已有旧视频不需要重给。若要研究**这个旧 Oumomo 请求**，只缺能对应到它的实际参考视频版本/参考图、原始用户提示词、后台最终提示词及请求关联信息；模型/参数只提供可见部分，后台没有显示就保持未知。新案例也可直接给同一请求的完整材料包，不必找回旧历史。当前先学习既有证据，不催用户补交材料。
