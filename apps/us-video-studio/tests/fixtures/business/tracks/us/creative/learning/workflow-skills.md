# US 创意方法：Skills 与运行入口

截至2026-09-20，三项项目Skill与案例学习已接入[独立制片室](../../../../apps/us-video-studio/docs/learning-migration.md)。站内策划、反馈审阅及创作包编译共用本目录的案例库和仓库Skill；技术接入不代表创意质量或市场效果已通过。

## 研究到制作的接入点

| 任务 | 项目 Skill | 实际作用 |
| --- | --- | --- |
| 定位用户、选择创意、学习参考 | [us-commerce-strategy](../../../../.agents/skills/us-commerce-strategy/SKILL.md) | 从处境、当前办法和顾虑推出购买判断、商品证据与角色关系，交付具体开头 |
| 写口播、排动作和时间 | [us-creator-video](../../../../.agents/skills/us-creator-video/SKILL.md) | 保留理由和态度，明确对镜/旁白，按动作与句群推导时间，实际台词进入brief编译器 |
| 图片与发布文案 | [us-photo-copy](../../../../.agents/skills/us-photo-copy/SKILL.md) | 按购买问题排图，锁定同款外观与事实，分开图内文字和caption；音乐留在TikTok |

Skill只在仓库 `.agents/skills/` 维护，不安装到全局。Studio按角色组装Skill正文及合资格案例，保存准确输入快照；快照不证明模型正确遵循，须结合调用台账和结果判断。编译包使用共享编译器，不自行生成媒体或授予批准，详见[创作包契约](../../../../apps/us-video-studio/docs/creative-package-contract.md)。

Skill使用UTF-8无BOM，以适配[Codex frontmatter解析器](https://github.com/openai/codex/blob/3ed49879c8b138df83e923083b0308eec8c3a704/codex-rs/skills/src/parser.rs#L200)。`.editorconfig`和编码校验仅对 `.agents/skills/<name>/SKILL.md` 使用该例外，其余Markdown/CSV仍需BOM。2026-09-18三个文件通过官方结构校验，并出现在会话技能清单；此前 `skills/list` RPC失败不改写为成功。

## 方法依据与边界

- [带货方法研究](commerce-methods-research.md)及[8条来源记录](commerce-methods-sources.json)：购买处境、顾客语言、具体性、角色可信度、尺度和使用关系。
- [图片文案研究](photo-copy-methods-research.md)及[8条来源记录](photo-copy-methods-sources.json)：平台指南、商品页可用性、图像信息与文案实验。两份共16条记录，含重复来源，不是16个独立研究。
- [TikTok观察](tiktok-field-study.md)：2026-09-18查看2个US PDP、4张图片与可见买家评论；未完成新原生图文完整翻页和视频原速看听。
- [案例库](README.md)：8条基线案例、6条历史方法及正反例、用户认可范围继续保留；通用论文不冒充已看案例，来源不同的证据不合成“创意分”。

采用理由、实际最终输入、产物和用户核心修改记录在对应run。新反例用于修正判断，不机械叠加永久规则；发布后的曝光、点击和订单需要真实来源，未记录的效果保持未知。

## 图片文案与历史入口

[图片文案模块](../photo-copy/README.md)由 `scripts/photo_copy_pack.py` 输出逐页文字、提示词、caption、来源及审阅HTML；已有成图可 `bind` 为新版本。`planned` 表示待生成，不能把空白卡片导出为成品。交付成图保留原字节，图内文字须实际查看。

[旧图文音乐样片](../photo-music/README.md)和4752研究静态页只作历史材料；静态页由 `creative_learning_hub.py` 生成，库变动需重建。日常操作在4760独立制片室，已支持原生策划、版本修改、学习入库及创作包编译；这些能力不等于旧制作方式全部兼容。

Gym资料已迁入项目 `bag-small-gym-studio-20260919`，接档保持暂停，v14仍是草稿。旧图审和费用批准只供追溯，六参考图生成请求尚不能直接由Studio单首帧入口接续；用户可在站内继续改进方案，迁移不自动重稿或付费。

## 默认入口与反馈承接

开发会话由根AGENTS进行短路由；创意方法只维护在三个Skill。`creative_session.py context` 接受助手明确的线路、意图和交付形式，保存Skill hash及相关案例上下文到对应run的新目录；它不是关键词分类器，不读取聊天记录或自动执行制作。Studio日常对话使用自己的角色输入快照机制，不要求用户回开发会话运行CLI。

`continue`须读取并保留已选方向的准确版本；`edit/status/feedback/pause`不重开创意；SEA不读取US库。无案例命中时可基于足够商品事实原创。命令执行或快照存在，不证明自然路由或创作质量通过。

```powershell
python scripts/creative_session.py context --market US --intent new --format video --tags packing,family,reason,speech --output-dir J:/E-Business-Video-Data/US/LUN/new-run/context-v1
```

输出目录必须新建，实际任务应换成对应run。2026-09-18的[25L功能文本提案 proposal-v1](../lunch/R25-function-proposal-20260918/README.md)原run为 `J:/E-Business-Video-Data/US/LUN/R25-function-proposal-20260918/`；仍未采用、无用户评价，不替换已采用的A-v7，也不恢复Gym。它是独立案例指针，不能强套其他任务。

本机留存的候选Hook定义尚未启用，不随仓库提交；没有更改全局配置、项目信任或定义hash信任。2026-09-18本机CLI报告hooks功能可用，但未验证原生生命周期接通。`creative_session.py hook` 仅返回仓库路由提醒，Stop/Interrupt返回空对象；实际开发会话入口仍为AGENTS与显式context调用。`codex debug prompt-input`当时返回0并包含根路由和三Skill，但不含Hook提醒，只能证明输入组装。

反馈须关联原run、准确版本、原话、日期、范围、意图和观察；原因可为假设或null，未观察到的改善为null。Studio生成候选后由用户审阅差异并确认入库；CLI追加用法见[案例反馈](README.md#给已有案例追加反馈)。相同事件幂等，身份冲突不覆盖，历史快照保留；追加或选择不产生图审、费用或发布授权。CLI不参与Studio文件锁，不应并发覆盖共享库。

## 2026-09-18历史验证范围

- 两道构造题完成文本演练：午餐包两页图文、独立鞋仓约9秒口播/动作。图文包绑定实际灰色供应图与hash，两页仍待生成；不算自然流量、盲测或成片成功。
- 图文演练位于 `J:/E-Business-Video-Data/US/photo-copy-methods-exercise-20260918/review-v1/`。午休情绪理由及25L人群适配未经用户验证；鞋仓29词/9秒约193词每分钟仅为规划，未原速试读。
- 首阶段根质量门480项测试、25份数据集/354行证据及666份文本编码通过；Skill结构和diff检查通过。当时新增静态页未完成浏览器视觉验收，不据此否定后来Studio的隔离浏览器验收。
- 后续路由校准使用6条模拟输入，覆盖创作、图文、含糊版本、局部反馈、调速与暂停/SEA边界；未作为真实反馈入库，也不是六次独立冷启动。
- R25 `proposal-v1`读取商品图、供应视频约21秒抽帧及相关方法；context在稿件形成后补存，不能充作事前图审。公开web取得MEXS PDP缓存正文；没有新视频原速看听、音频API校准或媒体生成。
- 后续根门510项测试、25份数据集/354行证据及671份文本编码通过；幂等、反例依赖、无参考原创、未见题隔离和最终HTTP前阻断均为离线测试。原R25 run的 `quality-gate-final.log` 与 `context-v3/` 保留证据，旧v1/v2按原字节保留。

上述计数是各历史时点的验证，不是2026-09-20整仓验收结果。2026-09-18未新增付费生成或音频分析，助手耗时和额度成本未知；没有以合成偏好升级方法。Studio后续接入与验收见[迁移说明](../../../../apps/us-video-studio/docs/learning-migration.md)。真实R25反馈、下一次任务的表达改善和方法保留/回退仍待观察，不能宣布创意学习闭环已验证。
