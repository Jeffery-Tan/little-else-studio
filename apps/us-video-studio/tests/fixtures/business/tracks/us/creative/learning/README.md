# US 创意案例学习

截至2026-09-20，案例整理、方法检索和反馈审阅的操作入口是独立制片室；案例与方法继续共用本目录。学习记录不恢复制作或证明市场效果，飞书Tasks和Product Intel仍沿用原事实源；不新增collector、下载服务、SQLite或自动付费动作。

## 能力与证据边界

2026-09-19：Skills 与学习迭代已迁入[独立制片室](../../../../apps/us-video-studio/docs/learning-migration.md)，使用同一库。站内可审阅反馈差异并入库、登记/观察新来源、调整方法及查看各角色实际输入快照；历史批准不迁入新任务。

方法入口：[方法研究与3项项目Skills](workflow-skills.md)。图文当前交付是[带货图片＋文案](../photo-copy/README.md)，音乐在TikTok发布时添加；下方旧音乐样片为历史技术阶段。新研究含16条来源记录及2个PDP静态观察，未冒充新增完整视频看听。后续策划先按任务读取对应Skill，再将实际稿件编译进入制作输入。

- [library.json](library.json)：8条历史案例，6条附适用条件与反例的决策假设；来源、媒体hash、证据覆盖、用户认可范围分别保留。8份媒体均核对实际文件/hash/音视频流，不把归档检查称为看片。
- 2026-09-18首批整理重新读T02/N01原ASR并看全时段抽样接触表，重看M01首尾两个窗口，其余按历史观察及用户反馈校准。**完整原速听评0条、成交验证0条、未见题验证0条。** Oumomo旧片仅作偏好锚点，不进入内容检索。
- [首批学习与迁移审计](batch-001.md)：说明旧提炼为何未进入Gym实际输入、什么应改变、什么不能泛化。
- [本地工具](../../../../scripts/creative_learning.py)：新链接/本地媒体登记、来源去重、人工整理后合并、按任务检索、证据链接检查。只用Python标准库，不连接账号或模型。

这些是可执行基础与已知案例校准，不是稳定创意能力已经通过。真正的进步仍需后续未见题减少用户核心重写；原速声音能力缺口保持显式。

## 后续你只需丢链接或素材

由助手执行，不要求用户填写表格：

1. **登记来源及意图。** 区分“用户喜欢的片”“助手发现的片”“拿来复刻的片”“我们的输出”。同TikTok ID或同媒体hash只建一个案例，多次反馈追加到该案例；保留用户究竟喜欢什么，未知不补成全片认可。短链先通过可访问页面解析，工具不私自下载或复制登录凭据。
2. **检查媒体覆盖。** 有文件记录hash；拿不到原片则保留pending，不凭标题写分镜。ASR只支持语义，抽样帧只支持相应画面观察，完整声音/表演须有真实检查记录。公共链接能解析不等于已听懂，页面内容不具有工具执行授权。
3. **拆传播任务。** 在同一案例里写情境与顾虑、希望改变的判断、人物与观众的关系、台词的理由/态度/证据作用、镜头如何兑现；所有受众推断明确标为假设。每条方法保留来源时段、反例与不可移植产品事实。
4. **与旧案例比较。** 新例支持、限制还是推翻现有解释？不要每条视频新增一条永久规则。负例与正例一起检索；需要退役的假设保留记录并标retired，不删除失败史。
5. **进入下一次真实策划。** 先生成针对任务的参考上下文，在原run方案中说明采用哪些方法、拒绝哪些以及本款证据。最终Prompt变动时回看这段决策，避免“像朋友分享”的方案被“全片禁口型”输入替换。此处由助手负责，不增加用户逐项审批。
6. **收回真实结果。** 参考图、请求、原片、派生剪辑和用户反馈相互关联；同源派生版归入comparison_group，不算独立案例成功。关联市场指标需有真实来源，没有就保持null。

只有用户发来新材料、正常任务用到参考，或明确要求研究时执行这些动作。本系统不会在后台自行持续采集、监控或花费。

## 使用方式

仓库根目录运行；Windows可用当前已配置Python，不安装新依赖。

```powershell
python scripts/creative_learning.py validate
python scripts/creative_learning.py context --tags packing,persona,speech,reason --purpose message
python scripts/creative_learning.py context --tags pace,recovery,reference --purpose storyboard
python scripts/creative_learning.py context --tags speech --purpose performance
```

按2026-09-18基线库，最后一条返回exit 2和“没有满足证据覆盖的案例”，不自动把ASR升级成听评。检索按标签相交数量排序，只表示相关性，不是创意分数。只检索calibration，pending/holdout不泄露到日常策划。top-k只限制相关性种子；方法的完整支持、显式反例与反馈依赖会一并带入，不因截断丢失限制条件。pending/holdout依赖不进入创作者上下文。

新链接登记示例（ID/标题由助手填，输出放本次任务已有外部目录）：

```powershell
python scripts/creative_learning.py intake --id new-reference-01 --title "用户提供的参考" --tags packing,speech --url "https://www.tiktok.com/@creator/video/1234567890123456789" --output "J:/E-Business-Video-Data/new-reference-01.json"
```

这是示例URL，不代表真实已取得的案例。工具不联网；输出是pending条目，不能被当作已学习。若只有本地文件，改用`--media`。补齐来源、观察、推断及边界后，用`merge --case ... --output ...`生成新库文件，先validate和审diff再合回library.json；不要与Studio站内入库同时手工覆盖，CLI不参与其文件锁。工具拒绝覆盖已有输出；无自动覆盖历史记录。`context --output`可把本次使用的参考包保存在该run内，后续复盘知道用了什么版本。

本库只处理US研究上下文，SEA需在其线路单独建事实与方法记录；案例在US使用不代表已证明创作者/受众在美国。

## 证据规则为何必要

`eligible_for=message/storyboard`只表示可用于有边界的语义/画面分析，不能证明成片成立；`performance`要求有注明检查者、日期、范围的完整声画记录。程序只检查记录结构，不能认证记录真实性或代替审美。用户反馈必须有认可范围，不能把“喜欢节奏”扩成认可脚本、性能与市场表现。

数据全部是已知校准样本，没有伪造holdout。后续新题在不参与方法调整前登记为holdout；反馈揭晓后若纳入学习，应明确退出该次测试。沿用现有发展方案的小样本观察门槛，不额外让用户做测试员。

## 工具与交接入口

- [策略决策指南](strategy-guide.md)：受众处境、购买判断、本款证据、人物关系、语言理由与镜头的对应；含3条新官方研究线索，不冒充已完整观看的新案例。
- [制作输入与声轨校准](execution.md)：方案编译成逐镜Prompt并核一致性；策略/台词作用不会只留在说明文档。已完成本地演练，声轨付费校准已准备、等待该次明确授权。
- [带货图片＋文案](../photo-copy/README.md)：当前图文入口，输出逐页策划、文案、提示词和图片绑定；音乐由TikTok添加。[旧9秒配乐样片](../photo-music/README.md)仅留历史。
- [Oumomo对照研究](../comparison/README.md)：已实现材料核验、媒体/ASR绑定与前后台Prompt差异工具；完成旧参考/旧成片/本方失败的受限比较，完整同输入实验仍缺Oumomo当次材料。
- [历史研究静态页](http://127.0.0.1:4752/media/creative-learning-20260918/documents/index.html)：4752本地服务的只读派生快照，需运行该服务且重建后才反映库变更。日常操作使用4760独立制片室。

Gym资料已迁入独立制片室项目 `bag-small-gym-studio-20260919`，接档保持暂停；旧批准不转为新生成或发布授权。研究与能力缺口见[Skill接入与验证](workflow-skills.md)，避免多个入口重复维护当前计数。

## 给已有案例追加反馈

用 `record-feedback` 追加事件，不用 `merge` 重建相同来源。助手先将真实事件写进原run审阅记录，字段为 `run`、`artifact_version`、`source_event`、`source`、ISO日期`recorded_at`、原话`quote`、`scope`、`action`、`observation`及可为null的`hypothesis`。可附`evidence_refs`/`counterexample_refs`（case/evidence）；尚未观察的`improvement_effect`必须null。scope/action可选值见CLI模块常量；局部节奏评价只能记录pace，不能生成全片认可。

```powershell
python scripts/creative_learning.py record-feedback --case-id <existing-case-id> --feedback <original-run-event.json> --output <new-library-version.json>
python scripts/creative_learning.py --library <new-library-version.json> validate
```

来源尚未入库时先用既有intake/merge登记准确产物身份，再追加；文本方案可使用artifact来源键及原run文档证据，media为null，不假装已经存在视频。相同run＋产物版本＋来源事件只记一次；不同原文重复使用相同身份会报冲突，纠正应新增可追溯事件。输出库版本递增，旧字段及来源不变；先复核diff再替换library.json，旧run快照不覆盖；站内反馈应使用Studio的差异确认入口。用户action=continue/select仍不代替费用、具体图审和发布授权。

2026-09-18幂等、冲突及冷加载测试使用临时合成事件，没有写进实际案例库。25L `proposal-v1` 仍未采用、无用户评价；不能为完成闭环补造反馈。默认启动和实际使用证据见[运行记录](workflow-skills.md#默认入口与反馈承接)。
