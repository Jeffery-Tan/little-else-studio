# 从案例到实际制作输入

截至2026-09-20；US。日常策划与制作在[独立制片室](../../../../apps/us-video-studio/README.md)进行。本文说明共享编译器、旧标准Wan提交的输入绑定及2026-09-18历史校准记录；不替代内容自检、具体图审或费用授权。

## 助手怎样使用

1. 按[Skill分工](workflow-skills.md)读取本题策略/口播技能，使用 `creative_learning.py context` 检索有对应证据覆盖的正反案例，结合[策略指南](strategy-guide.md)处理受众处境、待改变判断及本款证据。
2. 在当前run保存一份 `brief.json`。字段见[编译演练](brief-example.json)：记录采用/拒绝的案例、人物关系、形式选择和逐段实际声画。由助手填写，不交给用户填表。演练不是新稿推荐，其商品演示适配与说服力均未通过。
3. 执行下面的compile。输出同版本方案、案例库快照、实际Prompt、审阅说明和hash清单。每镜 `on_camera / voiceover / none` 决定实际说话指令；台词作用、形式依据与观众应获得的理解也进入Prompt，不留在策划说明里。
4. 依据原有视频自检规则检查内容。编译器不能判断“有说服力”，不能检查自由文本全部矛盾，也不能保证模型按指令演出。尚无可信原速听评时，保持该缺口，不用结构通过代替。
5. 实际提交前verify对读最终Prompt；修改方案、案例快照或Prompt需要重新编译和复查。旧标准Wan入口强制绑定编译包与最终请求，具体见下文；其他历史执行器不能据此宣称已覆盖。Studio制作使用其独立的准确版本审核、图审和费用确认，编译包本身不授权生成。
6. 返回结果依原有补救规则处理，并将实际输入、结果、用户反馈及适用范围回到同一案例组。参考复刻、速度版和后期版不计算成多个独立成功。

```powershell
python scripts/creative_brief.py compile --brief tracks/us/creative/learning/brief-example.json --output-dir J:/E-Business-Video-Data/US/creative-learning-20260918/new-exercise
python scripts/creative_brief.py verify --compiled J:/E-Business-Video-Data/US/creative-learning-20260918/new-exercise --actual-prompt J:/E-Business-Video-Data/US/creative-learning-20260918/new-exercise/prompt.txt
```

输出目录必须新建，不覆盖旧记录。`verify`通过只表示最终文字与方案编译结果一致；自由文本中的伪事实、错误策略、文字冲突和不自然表达仍可能存在。示例10秒只是字段/编译演练，不是后续统一时长，也未做口播听评。

## 2026-09-18编译演练验证

- 本地演练已编译至 `J:/E-Business-Video-Data/US/creative-learning-20260918/brief-exercise-v2/`；实际Prompt一致性通过。v1保留为编译修正前历史，不用作当前输入。
- 测试覆盖混合说话方式、露脸前提、静音镜头不能含台词、未见题隔离、未知商品依据编号、时间窗口、输出保护、最终指令漂移和关键表达意图进入Prompt。
- 复核发现初版曾漏传台词作用和形式选择，已补入编译与回归测试；没有生成媒体来掩盖输入缺陷。
- 技术验证不证明后续创意已改善。完整原速听评、未见题表现、真实转化仍分别记录。

## 2026-09-18声轨校准准备（尚未调用）

已从既有25L原速和用户接受的1.5倍版提取16kHz单声道WAV，各约34.04/22.69秒；无新视频生成、无变速加工。拟发送的输入仅含A/B匿名音轨及固定问题，不含原路径、历史评价或正确答案；尚未向模型提交。来源映射、验收标准和request hash保存在外部目录 `J:/E-Business-Video-Data/US/creative-audio-calibration-20260918/`。

计划仅一次阿里云北京 `qwen3.5-omni-flash-2026-03-15` 音频理解，最多3000输出token，只返回文本，无自动重试。该模型以前未通过视觉缺陷审核；该计划仅隔离“能否分辨节奏、共同语义和可辨听感依据”，不是换名宣布能力升级。已知答案校准不是未见题，单次偏好一致也不能证明听评可靠，尤其没有独立听评者验证细节时不能自动加入performance学习集。

2026-09-18核对[官方价格](https://help.aliyun.com/zh/model-studio/qwen3-5-omni-flash)：音频输入18元/百万token，文字输入2.2元/百万token，文字输出13.3元/百万token。[官方音频计量](https://help.aliyun.com/zh/model-studio/qwen-omni)为3.5输入约7token/秒。当时两段与3000输出预算估计不足0.10元，已向用户申请1次、上限1元；这不是供应商硬性扣费封顶。未获该次批准不调用、不读取凭证。批准后仍核冻结请求hash，结果/未知调用状态均保留，不自动重试。

## 如何减少用户负担

用户之后只需要发链接、素材或反馈；收件、可访问性检查、媒体覆盖、案例拆解、与旧假设比较、策划检索由助手完成。不能获取或听懂时说明具体能力缺口，不能把补课变成让用户逐条评分。用户批准某一稿也不替助手证明声画自然或能带货。

先在实际正常需求中检验用户是否仍需补核心理由/人物关系、成片是否新增同类错误；沿用现有未见题观察门槛。项目Skill不作为全局方法或新增执行授权。稳定的本地整理/校验已工具化，创作判断继续作为可被反例修正的方法。

## 运行与验证

普通案例/brief工具只需Python标准库；图片相关工具及测试需Pillow，已在 `pyproject.toml` 的 `test` 可选依赖固定版本。新建开发环境安装 `python -m pip install -e ".[test]"`，再运行根目录 `scripts/quality-gate.ps1`；此说明不要求对Jeffery现有全局环境重复安装。历史配乐模块另需FFmpeg/FFprobe与字体；新图片文案模块不需要它们。

2026-09-18早期浏览已验证搜索nurse得到1条、完整声画筛选得到0条及明确空状态，并查看历史样片首帧和9秒媒体控件。当时浏览器随后断连，旧研究/图文页面只完成HTTP入口和17个本地链接可达检查；这些是旧静态页的验收范围，Studio迁移验收见[迁移说明](../../../../apps/us-video-studio/docs/learning-migration.md)。新增工具与Skill检查见[验证记录](workflow-skills.md)，不得用技术测试替代全程播放或听评。

旧静态视图由 scripts/creative_learning_hub.py 从案例库与实际产物新建，参数为 --output-dir、--photo-run（历史样片）、--comparison-run、--brief-run，以及可选 --photo-copy-run（当前图片文案编译包）；输出目录在Git外。研究tab按实际文件展示方法与Skill，未接图文包时明确未编译。页面是当次快照，后续材料更新需重新构建新版本并更新入口，不伪称自动监听。

## 旧标准Wan入口的编译绑定

实际路径由25L A-v7原始制作记录、外部回执和Gym production-review引用核对：`scripts/run_lunch_wan_reference_trial.py` 的标准Wan路径。2026-09-18加固只覆盖这条；独立`--first-last`及其他历史执行器不属于这次绑定。Studio的单首帧Wan适配器是另一路径，不能原样接续Gym旧六图请求；Gym/v14仍暂停。

- 标准`prepare`实际上传必须传 `--compiled <creative_brief编译目录>`；先离线检查编译Prompt、场景总时长、图片顺序、源文件hash及音视频来源，之后才进入原有图审/上传边界。合法无编译的旧`--dry-run`只可看预览，不能submit。
- 上传后仅允许以真实传输URL替换离线占位URL，其他参数须一致，输出 `creative-binding.json`。完整请求、编译manifest摘要、实际出站JSON字节及媒体顺序/身份均绑定。原run已有请求不覆盖。
- 标准`submit`强制要求binding及原有`execution-approval.json`中的 `creative_binding_sha256`（对binding做原有request_digest）；**不得由检查器自动写 approved**。保留原有一次请求、精确图审、价格和费用范围核验；删除binding不能退回旧路径。
- 最终HTTP序列化后，`http_json`的`before_send`再次核对真实bytes及独立授权，漂移在`urlopen`前拒绝。原有`poll`可读历史任务，不为校验修改历史请求或审批文件。

新brief允许 `reference_mode: original_product_facts` ＋非空 `reference_reason` ＋空 `reference_decisions`，仍必须有足够商品事实及限制。有参考时继续核真实合资格case/evidence。manifest v2只写calibration相关依赖子集，另留原全库SHA/schema；旧v1冻结包只读验证兼容，历史字节不改。研究HTML也使用同一投影，不只隐藏前端列表。

这些检查只证明确定性一致性。自由文字语义、自审结论、自然声画、用户选择、图审与付费授权继续独立。2026-09-18绑定验收使用mock传输与dry-run，没有上传或付费生成。
