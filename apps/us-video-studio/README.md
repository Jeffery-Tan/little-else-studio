# US 视频制片室（us-video-studio）

US 视频与图文前期策划的本地独立工作台：用户在应用内与主 Agent 对话，主 Agent 调配角色 Agent，独立审核 Agent 给出评审，再由用户决定方向与脚本。支持策划、本地审阅包，以及由用户在同一工作台「分镜 / 成片」中明确提交的 Codex 图片与百炼 Wan 视频任务；不自动发布或判断市场效果。

功能代码、当前服务加载和真实能力验收分开记录，统一见 [实现与验收状态](docs/product-improvements.md)。本页描述工作树功能，不能单凭页面出现按钮判断对应后端已加载。
HTTP 入口在 [server.py](studio/server.py) 组合依赖和本机安全边界，学习交付、项目、素材、决定、连接与任务路由分别在 `studio/http_*.py`；测试客户端使用环回地址，与正式 Host 规则一致。
创意与声画验收按[分层任务集](docs/creative-quality-validation.md)开展；离线工程测试只覆盖状态与安全，不代表真实模型或成片质量已验证。

**直接在对话中说想做什么，附上视频或粘贴TK链接。** 主制片承接明确的分析、参考复刻或局部借鉴；自主创作无需参考，目标不清才问一句。通过[百炼北京 Omni](docs/bailian-omni-and-tiktok-import.md)复用现有Wan连接，只分析保存报告并停止；复刻绑定指定观察后接续主制片文字策划。链接失败可上传原视频续接，已完成分析会复用。当前代码已接通，真实链接成功率与模型识别质量待验收。

[Codex 驱动工作台](../video-workbench/README.md) 与本应用分别维护、互不写入运行状态；不自动迁移历史批准。业务采用状态仍以 [US 线路](../../tracks/us/README.md)、[午餐包当前状态](../../tracks/us/docs/lunch-video-current-state.md)及飞书 Tasks 为准。开发约束见 [CLAUDE.md](CLAUDE.md)，架构与运维见 [独立运行说明](docs/independent-runtime.md)。业务操作在本应用完成，Codex 桌面开发对话不作为后台执行器。

## 创作工作空间

首页项目卡片的「删除」和工作台顶部「删除项目」会先显示项目名与影响，确认后移入回收站。首页「回收站」可恢复全部对话、素材与成果；删除不清除磁盘文件或历史调用记录。正在执行的项目需先停止并等待结束，删除操作不会暗中取消任务。已删除项目从列表和切换器中隐藏，并拒绝新任务。

首页从一句话和可选参考图片/视频开始，自动建立创作记录；素材上传完成后才发送第一轮对话。Agent 在对话中用简短推荐选项和自由补充确认重要意图。桌面工作台左侧查看方案、分镜和成片，右侧保持连续对话；窄屏可切换画面与对话。创作板是准确版本的唯一决定入口，对话区可直接定位过去。确认方向后接续完整方案，确认完整方案后接续分镜输入准备；若决定已保存但任务未启动，项目会显示“继续准备”入口。已创建任务的重试只读取原任务，不自动重发模型调用；实际媒体生成仍需明确确认。版本、哈希与内部审核保留在后台和折叠记录中，旧项目与准确版本审批保持兼容。

任务状态卡展示当前角色、正在处理的操作与最近进展；「团队工作记录」按时间列出本轮分工、已完成操作和阶段内容入口，技术诊断默认折叠。角色处理开始事件在模型等待与工具执行前记录；只展示真实事件，不推算完成百分比或补造旧记录。

「灵感与方法」默认从参考画面进入，Skills、经验、反馈和本轮上下文仍可访问。版本哈希、调用记录、连接协议与费用参数默认收进详情；运行失败与审批要求仍明确显示，未知用量不成为创作待办。

项目列表、创作板、制片对话与主制片共用一份项目状态，区分“内容已保存”“审核未完成”和“等待采用”。新稿共用结构化内容生成正文；问题明确责任人与处理时机；审核重试绑定准确版本，不重新派单改稿。实现边界与旧稿兼容见 [制片交互架构](docs/creator-interaction.md)。

## 启动与检查

左侧「灵感与方法」已接入三项 Skill、原案例与方法库、研究来源和反馈迭代。Agent 可按角色读取[登记方法卡](studio/learning.py)，补救、视频输入与图文输入卡只提取稳定执行规则，原历史研究和反馈仍限人工审计或原资格过滤。实际角色输入保存快照；反馈绑定准确版本，先看差异再应用。视频输入与图文审阅包可在「本轮参考」打开，见 [学习迁移说明](docs/learning-migration.md)。

真实运行数据必须位于 Git 外，使用 `STUDIO_DATA_ROOT` 明确选择。未设置时代码沿用历史 J 盘默认目录，它不代表当前电脑实际使用的位置；素材、台账、配置与凭据不得提交 Git。一个数据根只允许一个 Studio 写入进程；离线导入或恢复前停止该数据根的服务。

以下在仓库根执行。使用完整运行依赖；测试另安装开发依赖。更新已有服务前先确认没有正在执行的任务，并正常停止该服务。此示例的数据根对应 2026-09-20 恢复目录，其他电脑须改为其实际恢复目录；不存在时先按跨电脑恢复说明操作，不直接启动空目录：

```powershell
$studioRuntime = Join-Path $env:LOCALAPPDATA 'LittleElse/Studio/runtime-venv'
$studioPython = Join-Path $studioRuntime 'Scripts/python.exe'
$studioData = Join-Path $env:LOCALAPPDATA 'LittleElse/Studio/restored-20260920'
if (!(Test-Path -LiteralPath $studioPython)) { py -3.12 -m venv $studioRuntime }
& $studioPython -m pip install -r apps/us-video-studio/requirements-runtime.txt
& $studioPython -m pip install -r apps/us-video-studio/requirements-local.txt

# 离线测试：不连接模型，不产生费用
& $studioPython -m pytest apps/us-video-studio/tests -q
node --test apps/us-video-studio/tests/js/*.mjs

# 同一入口先诊断，再启动；失效解释器、依赖、数据根、writer 锁和端口有对应提示
powershell -NoProfile -ExecutionPolicy Bypass -File apps/us-video-studio/scripts/start.ps1 -DataRoot $studioData -CheckOnly
powershell -NoProfile -ExecutionPolicy Bypass -File apps/us-video-studio/scripts/start.ps1 -DataRoot $studioData
```

已有项目必须选实际恢复的数据根；空目录会被诊断阻止。确实要建立全新空工作台时先建立 Git 外目录，再显式加 `-AllowNew`；首次启动会写入初始化标识，之后的正常重启无需再加。诊断不调用模型或媒体生成；FFmpeg/FFprobe 缺失显示为媒体能力警告。启动脚本可从当前用户已登记的 PATH 补上新安装的 FFmpeg 目录；正在运行的旧进程仍须正常重启才会加载。也可从应用目录运行 `python -m studio.doctor --data-root <DATA_ROOT> --json`。站内「模型设置 → 当前服务版本与环境」及 `/api/runtime` 显示本进程启动时间、后端代码与 US 方法指纹、数据根匿名标识和依赖，不返回原始路径或凭据；这些状态不能证明模型已实际调用成功。

默认仅绑定 `127.0.0.1`，访问 `http://127.0.0.1:4760/`。本应用没有公网身份验证，未配置开机启动或公网部署；不要直接暴露服务。

换电脑继续工作：代码通过 Git 拉取，项目与素材从私有 Release 备份恢复到新的 Git 外数据根。完整安装依赖见 `requirements-runtime.txt`，下载、恢复与后续接续步骤见 [跨电脑恢复](docs/portable-projects.md)。登录与 API Key 留在各自电脑，不随备份传输。

### Agent 与模型

执行架构与优化依据见 [Agent 架构审查](docs/agent-harness-review.md)。角色提交成果、审核记录结论后立即结束该角色本轮，主制片统一说明结果；不额外调用模型补写结束语。工具在执行前按本轮能力集合检查，派工无交付或审核无结论会明确返回失败。

主制片每条新消息重新确定工作范围，可找回本项目的旧对话；专业角色返工时拿到准确上一版与对应责任问题，并可补读登记方法。所有角色与模型设置页共用已声明的能力边界；主制片可按明确任务观察参考视频，素材区可显式做有限声画分析，但各专业角色仍只读来源标明的观察记录，不直接收到原视频像素或音频。模型理解质量仍需实测。每次调用的逻辑输入保存在 Git 外原 run 下的 `requests/<call_id>.json`，供本地指纹校验与诊断；记录失败不调用模型，不把快照用于跨角色或跨任务恢复对话。

本轮明确交付目标可选需求整理、方向、完整方案、制作输入、局部修订或只分析。任务结束时程序分别记录模型执行状态与交付状态：准确产物、相应独立检查或本轮修订计划缺失时列出具体缺项，不能把模型正常结束当作作品已交付。只读跟进不要求新的制作产物；生成仍需原有用户确认。

素材页现支持视频画面分析：本地提取带时间点的画面，单次交给所选视觉模型观察，经采用后进入创作依据；不发送音频、不宣称完整看听。参考解释与商品观察分别整理，导演构思前即可读取。输入限制、评估样本和运行依赖见 [素材理解与参考迁移](docs/material-understanding.md)。

对话中的局部修改现可绑定准确旧稿和字段，由程序保留其他内容；主制片按依赖和责任审核安排必要工作。新版仍需审核/采用，旧批准不迁移。支持范围与四点路线进度见 [按问题执行与局部修订](docs/problem-directed-revisions.md)。

点击左侧「模型设置」，先配置模型服务，再为主制片、创意导演、视觉策划、编剧和独立审片分配服务与模型。DeepSeek 提供模型选项；通用兼容连接仍填写提供方的准确模型 ID。新增服务不会自动替换主制片或其他角色。

- **Codex CLI**：使用本机 CLI 的现有 ChatGPT 登录。打开模型设置时只读加载本机 Codex 返回的可见模型，服务卡上的「读取本机可选模型」可刷新；随后可为五个角色分别从下拉列表选模型。留空时使用 CLI 默认模型；已保存但暂不在列表的型号保持原值，读取失败时仍可手填，不会静默改选。模型列表不是额度、登录或创作质量验证，也不发起模型调用。每次模型调用使用新的临时线程，制片室明确传入业务上下文，不复用当前开发对话。CLI 仍可能加载自身全局 AGENTS.md；适配器只允许该明确路径，拒绝仓库和其他项目指令来源，不承诺与 Windows 用户完全隔离。CLI 内置执行能力受限，模型只返回制片室工具请求，由后端执行与校验。优先使用服务进程 PATH 中的 CLI；Windows 普通终端未配置 PATH 时，自动从当前用户 `%LOCALAPPDATA%/OpenAI/Codex/bin/` 的已安装版本定位，优先最近更新的版本目录，其次旧版平铺目录。仍会校验实际协议与限制配置，不修改系统 PATH，也不要求桌面客户端保持打开。找不到时页面会明确提示安装与重启服务。模型目录协议依据：[OpenAI Docs · Codex App Server](https://learn.chatgpt.com/docs/app-server#list-models-modellist)。
- **DeepSeek 官方 API**：选择 DeepSeek 后填写 API Key，地址与兼容参数由应用设置，不需要手填。可先点击「验证密钥与模型」，再把可用模型分配给角色；也可先保存，未验证状态会明确显示。默认提供 `deepseek-flash` / `deepseek-v4-pro` 选项，验证后以提供方返回的模型列表为准，既有模型不会被静默替换。更换 Key 或服务后旧验证结果失效。
- **连接验证范围**：只向固定官方地址 `https://api.deepseek.com/v1/models` 发出一次只读 GET，验证认证并获取模型列表；不生成内容、不提交业务资料、不保存输入的 Key、不改变角色绑定。成功不代表余额、文本生成或完整 Agent 流程已经实测。Key 仅在点保存时按原本机 DPAPI 机制持久化；关闭窗口清空输入。验证结果仅用于当前编辑会话。
- **DeepSeek 任务运行**：专用适配器使用 thinking enabled/high；工作台不传 `max_tokens`，采用提供方默认输出额度，不继承通用兼容连接的 16K 上限（官方 2026-09-21 文档：当前思考模式默认 64K）。累计费用预算按已知记录执行，用量未知不阻断；同一角色的当前循环精确保留工具往返和续写所需的私有推理字段，不进入用户正文或其他角色。新 job 从已保存对话与产物开启新的推理请求，旧助手回复作为标记的历史资料，不伪造旧推理字段或建立第二套可恢复私有会话；逐次逻辑请求诊断快照可能含本轮必要私有字段，不向其他角色提供。调用继续通过原用量台账和任务恢复边界。接口依据：[思考与工具调用](https://api-docs.deepseek.com/guides/thinking_mode/)、[模型列表](https://api-docs.deepseek.com/api/list-models/)（2026-09-19 核对）。
- **Anthropic / OpenAI 兼容 API**：填写服务地址、API Key 与该提供方的准确模型 ID。OpenAI 连接默认使用 `max_completion_tokens`；旧兼容网关可选择 `max_tokens`，不自动重试替换参数。Anthropic 地址可含 `/v1`；OpenAI 地址可含 `/v1`，勿填写具体 messages/chat/completions 路径。
- **价格**：可按连接、模型填写输入/输出价格及来源；空白保持未知，Codex 订阅用量不套用 API 价格。更换已有连接的地址或协议会清空旧价格，核实后重新填写。
- **保存**：仅保存配置，不调用模型；正在执行的轮次保留配置快照，下一轮才应用变更。更换地址或协议需要重新输入 Key。

| 配置入口 | 含义 |
| --- | --- |
| `STUDIO_DATA_ROOT` | 数据根；未设时使用上述默认目录 |
| `<data-root>/connections.json` | 站内维护的连接、角色绑定与 Windows DPAPI 加密凭据；不提交 Git |
| `<data-root>/config.json` | 预算软阈值与循环限制；已有文件保持原样 |
| `STUDIO_BASE_URL`、`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` | 旧直接客户端的兼容配置；新的站内连接不自动导入环境凭据 |

没有 `connections.json` 的新服务默认各角色选择 Codex CLI。已有旧版 `config.json` 的模型/端点不会自动迁移为站内连接；第一次正式使用前在页面确认角色配置。预算和历史账目继续沿用原数据根。API Key 只在服务端加密存储，接口仅返回是否已保存，不写入浏览器存储、任务事件或模型上下文。

### Codex 登录 / 授权

在「模型设置」的 Codex 服务卡内查看登录状态；未登录时点击「登录 / 授权」，在系统浏览器完成官方 ChatGPT 登录，返回工作台后自动更新。流程使用本机 `codex login`，凭据仍由 CLI 自行保存，工作台不读取或返回密码、令牌及原始登录输出。依据：[官方身份验证说明](https://learn.chatgpt.com/docs/auth)。

打开设置和「刷新登录状态」只执行 `codex login status`，不调用模型、不查额度。已登录 ChatGPT 时不重复授权；已使用 API Key 登录时提示用户在终端自行确认切换，不自动登出或覆盖账号。未找到 CLI 时先安装，再刷新状态。

同一服务只允许一个登录流程，重复点击复用当前等待；模型任务运行时不能启动授权，授权等待期间不能新发起模型任务。登录最长等待10分钟，失败或超时后可手动重试；不自动启动第二次授权。关闭设置只停止页面查询，重新打开可继续查看；「取消本次登录」绑定显示的 attempt_id，只停止这次等待，不登出已经登录的账号。普通服务关闭会清理仍在等待的登录子进程。

接口：`GET /api/connections/codex/status` 返回受限状态、中文提示、登录方式和本次 attempt_id；`POST /api/connections/codex/login` 要求 `{"consent":true}`；`POST /api/connections/codex/login/cancel` 要求 `{"attempt_id":"界面实际显示的ID"}`。两个写接口要求本机同源 JSON，不提供 logout、令牌读取或账号切换接口。登录状态不证明实际生成或费用验收通过。

### 隔离界面预览

预览使用手写方案、审核和台账数字；它们不是实际创意结论、用量或批准。`scripts/seed_preview.py` 默认不复制商品素材，页面会明确显示缺失文件，适合检查决定、流程与降级状态；如需检查真实媒体排版，可显式传 `--media-root <商品素材目录>`，脚本先核对全部文件再复制。素材与演示文案未必是同一商品。脚本仅接受不存在的新数据根，拒绝覆盖已有目录。

在应用目录、专用 PowerShell 会话执行，下例目标目录必须尚不存在：

```powershell
$studioPython = Join-Path $env:LOCALAPPDATA 'LittleElse/Studio/runtime-venv/Scripts/python.exe'
$studioPreview = Join-Path $env:TEMP ('studio-preview-' + [guid]::NewGuid().ToString('N'))
& $studioPython scripts/seed_preview.py $studioPreview
$env:STUDIO_DATA_ROOT = $studioPreview
& $studioPython -m studio.server --port 4762
```

新版预览种子写入无凭据的 loopback 测试连接，全部角色都指向它，发送消息会在调用前阻断。旧预览目录不自动迁移；请使用新目录。不要在预览根添加真实端点配置或切换到 Codex。三个 fixture 分别展示方向待采用、脚本审核退回和全部完成；包含一个故意引用不存在文件的镜头，用于检查降级提示。停止服务后保留或按明确授权清理预览，不能据此删除其他运行数据。

## 界面与工作流

界面采用炭黑、灰阶与荧光黄绿，首页项目卡片、创作板和制片对话构成主要工作空间。主题在 [cinema.css](studio/web/cinema.css)，行为布局在 [style.css](studio/web/style.css)；使用本机字体回退，无外部字体、图片或前端框架依赖，支持窄屏和减少动态效果设置。

四个阶段为 `intake → direction → material_plan → writing`。需求整理呈现事实、约束和未知项；创意方向展示多个选项与推荐依据；素材计划说明可用素材与缺口；脚本呈现镜头时长、画面、台词和声音。`direction` 与 `writing` 必须经过独立审核及用户决定；本应用尚无 `layout_preview` / `ad_preview` 渲染阶段。

- 桌面左侧查看成果、右侧持续对话和就地决定；窄屏切换创作板与制片对话。原文阅读器支持版本切换，素材抽屉支持预览和上传。
- 方案、分镜、成片采用关联的标签页与面板；键盘左右方向键、Home、End 可切换并移动焦点，Tab 只进入当前标签。
- 推荐不等于采用。用户明确选中方向再提交，批准记录保存 `selected_option_id`；后续角色读取实际选择。
- 退回修改需要意见；覆盖审核退回需要明确理由，记录为 `approved_override`。批准仅覆盖该版策划，不授予生成、费用或发布权限。
- 同一版本只作一次决定：采用或退回之后，这一版不再出现决定入口，下一页由团队按你的意见产出；审核退回不在此列，它仍保留具名覆盖的入口。
- 决策草稿按运行、阶段和产物摘要保存在本机浏览器；旧对话区草稿可恢复到唯一的创作板决定入口。保存中阻止重复提交；冲突保留草稿，要求重新阅读最新版本。审核退回和上游失效不显示为完成。
- 费用入口展示真实记录的已知用量与待核实项；历史阶段用量是参考记录，不是下一次费用预测。

领域状态与业务措辞主要由 [vocabulary.py](studio/vocabulary.py) 提供，前端负责呈现与交互校验。审核通过时仍展示注意事项，不把 `pass` 当成内容可用保证。

## 架构与版本契约

```text
浏览器 studio/web (REST 任务提交 + 进度重连；保留 SSE 兼容入口)
  → server.py
      → jobs.py：持久任务、幂等提交、取消、重启中断与单 writer 锁
      → connections.py：加密凭据、每角色路由与配置快照
      → pipeline.py：前置条件、尝试、审核、审批与失效传播
      → store.py：不可变产物、事件历史、摘要与原子写入
      → ledger.py：调用意图、结算与待核实用量
      → agents/orchestrator.py：主 Agent 循环
          → tools/dispatch.py：上下文隔离的角色 Agent
          → agents/reviewer.py：独立审核上下文
```

每份产物由 `<stage>-vN.md` 正文及 JSON sidecar 组成。`artifact-v2` 摘要绑定阶段、版本、正文、展示结构、摘要、证据、限制和依赖。结构化展示不是第二套事实；格式不可用时保留正文并记录降级，不能据此伪造可选方向或审批。

上游产物、已选方向、素材字节、外部观察或导入来源变化会使依赖产物及审核失效，保留旧版本和批准历史。素材存在只证明文件可读取，不证明模型理解了画面。历史 `body-v1` 仅可回放，继续制作必须生成并审核 `artifact-v2`，不能复制旧批准。

审核只读取目标版本、必要事实与自身标准，不读取生产者的完整对话或自我背书。写作者直接获得 intake、已采用方向和素材证据。任何 Agent 工具都不能签发审批或写用户批准。

服务取得数据根 OS writer 锁，离线导入和恢复 CLI 复用同一锁，避免和服务同时写入。阶段锁仍覆盖同一 run 的版本操作；一个数据根同一时刻只运行一轮 Agent 任务，冲突请求明确拒绝，不隐藏排队。

### HTTP 集成

| 端点 | 用途 |
| --- | --- |
| `GET/POST /api/runs` | 列出或创建运行 |
| `POST /api/conversations` | 一句话与素材开始创作，幂等建立项目及首轮任务 |
| `DELETE /api/runs/{id}`；`GET /api/trash`；`POST /api/runs/{id}/restore` | 移入回收站、查看回收站、恢复原项目；不删除运行文件 |
| `GET /api/runs/{id}/state`、`/timeline` | 领域状态或界面投影；只读推导 |
| `GET /api/runs/{id}/chat`、`/events`、`/ledger` | 对话、历史事件、按调用合并后的台账 |
| `GET /api/runs/{id}/artifacts` | 产物列表，可按 `stage` 筛选 |
| `GET /api/runs/{id}/artifacts/{stage}/{version}` | 指定版本正文、展示结构与完整性校验 |
| `GET/PUT /api/connections` | 读取不含凭据的配置；携带 expected_revision 保存配置 |
| `GET /api/runtime`、`GET /api/capabilities` | 当前已加载进程标识与声明能力；不测试付费模型 |
| `POST /api/connections/deepseek/check` | 只读验证 DeepSeek 密钥并获取模型列表；不保存配置、不生成内容 |
| `GET /api/connections/codex/models` | 只读获取本机 Codex 的可见文字模型；不保存配置、不发起模型调用 |
| `POST /api/runs/{id}/jobs` | text + request_id 创建持久任务；同 ID 同文本幂等 |
| `GET /api/runs/{id}/jobs/latest` | 当前项目最后一次任务，无任务为 null |
| `GET /api/runs/{id}/jobs/{job_id}` | 读取任务和事件；after_seq 可读取增量 |
| `POST /api/runs/{id}/jobs/{job_id}/cancel` | 协作停止，不强杀在途模型请求 |
| `POST /api/runs/{id}/message` | 旧 SSE 兼容入口；观察同一持久任务 |
| `POST /api/runs/{id}/nonce` | 为页面实际展示版本签发审批 nonce |
| `POST /api/runs/{id}/decision` | 用户提交采用或退回决定 |
| `GET/POST /api/runs/{id}/materials` | 查看素材列表或上传素材 |
| `GET /api/runs/{id}/materials/{name}` | 读取素材文件 |
| `GET/POST /api/runs/{id}/material-observations` | 读取或追加准确素材观察；保存不调用模型 |
| `GET /api/runs/{id}/recovery`；`POST .../recovery/reconcile`、`.../recovery/abandon` | 读取恢复状态及保留的管理员兼容操作；未知用量不需要核账，也不阻断继续使用 |
| `GET /api/runs/{id}/history`、`/history/{document_id}` | 读取迁入历史目录或登记的精选文本；无在线导入入口 |
| `GET /api/learning`；`GET /api/runs/{id}/learning`；`POST .../learning/proposals`、`.../learning/proposals/{proposal_id}/apply` | 共用方法库、实际快照、候选差异与用户明确入库 |
| `GET /api/runs/{id}/delivery`、`/delivery/download`；`GET .../creative-packages`、`.../creative-packages/{package_id}/files/{filename}` | 当前交付、只读策划 ZIP 和本地创意包文件 |

nonce 请求的查询参数必须包含 `stage`、`expected_version` 和 `expected_artifact_sha256`。决定 JSON 必须携带相同三项，加上 `nonce`、`decision`（`approved` / `returned`）；采用含选项的方向时还必须有有效 `selected_option_id`，退回填写 `note`，覆盖退回审核填写 `override_reason`。

这些值取自用户实际阅读的产物，不得提交前偷偷读取最新版替换。服务端只落盘 nonce 摘要；相同 nonce 与相同决定重试幂等，更改选项、理由或决定则拒绝。模型轮次执行中、版本冲突或失效时返回 `409`；必填字段缺失返回 `422`，无效阶段返回 `400`。nonce 不是公网身份验证。

### 素材与观察

素材位于 `runs/<id>/materials/`，可从界面上传或在服务停止时本地放入。按文件名引用，缺文件时明确提示；视频由浏览器预览，不转码。上传检查扩展名、64 MiB 单文件上限和最终路径，拒绝同名覆盖；不直接创建阶段产物或批准，但素材摘要变化会使相关依赖失效。

外部观察可写入运行的 `evidence/observations.json`，须绑定文件名、hash、观察层级、方法、覆盖范围、观察者、日期、发现及限制；格式与校验见 [evidence.py](studio/evidence.py)。没有有效观察时仅记 `existence_only`。模型不能通过工具写入观察以冒充自己看过素材。

## 用量、费用与恢复

每次模型请求（API 或 Codex）先持久化 intent，再追加 settlement；台账按 `call_id` 合并有效记录。传输不自动重试，不完整结束、畸形工具参数或缺最终用量不能记作成功。内容是否成功与用量是否完整分别记录：完整非流式响应的合法输入/输出用量会自动记账，即使内容解析失败也不要求手工核账；断流计数或缺失用量仍保留未知，不补零、不要求用户核账。完整返回 `max_tokens` 时，角色循环可在配置限制内发起单独计费的续写；这与重发中断请求不同。

未知用量或价格保留为未知，美元金额为 `null`，不能补零。已知小计、未知调用和待核实状态分别显示；当前个人工作台不提供手工核账流程，未知用量不阻断任何项目后续调用。实际账单由用户在提供方平台查看，未来商业化积分/计费另行设计。金额是价目表估算，不是提供方账单。

默认总量软阈值为 2,000,000 token，美元软阈值为 10；仅确认有效价目表后美元阈值才可执行。上限跨该数据根所有运行累计，在调用前检查，最后一笔可能越过阈值，不保证绝不超额。

日常恢复只处理未结束的阶段尝试，不要求用户填写 token 或媒体计费用量。已结束调用即使用量未知，也允许用户明确发起下一轮；未完成的阶段仍可在「制片对话 → 找回创作进度」关闭，保留历史且不自动重试。

以下命令为保留的开发排错/历史兼容入口，不是用户继续创作的前置要求。在应用目录执行，显式指定数据根，不调用模型、不批准内容、不自动重跑。先停止对应服务；只有取得提供方用量或未受理证明才能对账，不得从部分输出猜 token。

```powershell
python -m studio.recovery --data-root <DATA_ROOT> pending
python -m studio.recovery --data-root <DATA_ROOT> reconcile <RUN_ID> <CALL_ID> --input-tokens <N> --output-tokens <N> --evidence-note "提供方用量记录或未受理证明的出处"

# 关闭崩溃遗留的指定开放尝试；不清除原稿、历史或未知费用
python -m studio.recovery --data-root <DATA_ROOT> attempts <RUN_ID>
python -m studio.recovery --data-root <DATA_ROOT> abandon <RUN_ID> <STAGE> --attempt-token <TOKEN_FROM_ATTEMPTS> --reason "中断核查说明" --server-stopped
```

浏览器刷新或关闭不会取消已接受的任务。发送结果不明时页面保留 request_id，点击「确认提交状态」会使用原 ID，不生成第二笔请求。服务重启将未完成任务标为 interrupted，用量未知不要求手工处理，不自动重发；打开原项目即可看到记录和恢复提示。停止任务会等待当前调用返回、记录真实用量，再停止后续调用和工具；它不撤回已完成产物，也不保证撤销当前调用费用。

恢复后由用户明确要求继续，系统依据当前版本与项目状态执行。仍开放的阶段尝试可在站内关闭，或使用上面的 abandon 命令；未知用量仅作排错记录，不阻断继续。「准备继续创作」只放入可编辑草稿，发送后才启动新一轮。确认模型请求尚未发出的本地配置/握手失败记录为失败、已知零 token，不制造未知账阻断。

### 创作入口、素材观察与交付

首页保留已有创作记录与下一步，默认入口为对话。工作台依据同一份项目状态呈现正在做什么、需要用户决定什么以及已保存的成果；确认卡片绑定实际展示内容，普通澄清选项不代替方案或媒体批准。桌面同时展示成果与对话，窄窗口只展示所选视图。

「项目素材」区分已上传与已记录观察。用户查看后可填写用途（商品/参考）、实际覆盖、发现与限制，保存到原有 `evidence/observations.json`，绑定素材 SHA 和观察清单版本；冲突拒绝覆盖，旧记录保留。保存会使依赖旧证据快照的阶段失效。角色上下文明确标注人工来源，参考不能被当作本商品事实。人工观察入口不调用模型。独立图片分析及逐条采用见下文；视频支持百炼声画分析与兼容视觉模型抽帧观察；统一参考任务见本文开头。

「查看交付」集中列出当前阶段版本、采用情况、缺项及既有制作文件。下载策划资料 ZIP 只读打包版本清单与策划原文，不包含媒体，不调用模型或改变批准；文件指纹异常时拒绝导出。编译制作文件仍沿用既有 `compile_creative_package` 工具，不能把策划 ZIP 当成已生成成片。

实现范围、案例迁移和已记录验证见 [实现与验收状态](docs/product-improvements.md)。

### 导入 ContentInput

先创建空 run，停止该数据根其他写入，再在应用目录执行：

```powershell
python -m studio.input_import --data-root <DATA_ROOT> --run-id <RUN_ID> --path <CONTENT_INPUT_JSON> --repository-root <REPOSITORY_ROOT> --allow-root <REPOSITORY_ROOT> --allow-root <MEDIA_ROOT>
```

导入器使用本应用独立的 [ContentInput v1 校验](studio/content_input_contract.py)，仅允许读取显式指定根目录中的来源；保留完整输入字节、事实层级、观察覆盖、未知项和来源指纹。拒绝覆盖已有 intake 或输入文件，来源改变会使依赖失效。导入是本地显式入口，不是任意文件读取 HTTP API；包中的授权字段不自动授予本应用生成、费用或发布权限。

## 验证范围与剩余工作

离线测试与隔离浏览器覆盖版本/方向绑定、任务和费用恢复、连接快照、图片分析采用、局部修订、制作流程及迁移边界。正式本机服务与多功能包接档已完成读取、播放及接口验收；最新复验、历史测试数量和布局证据统一见 [实现与验收状态](docs/product-improvements.md)。2026-09-17 的领域审查见 [方案核验](../../tracks/us/docs/studio-plan-review-2026-09-17.md)。

已完成一次明确授权的最小真实 Codex 文本连通测试，只使用合成文本，未走业务工具或素材生成。该授权已使用；本机协议可用、模型列表可读、离线测试通过都不代表真实生成或创意质量通过。

尚未完成真实图片/视频生成的连通、扣费与内容质量验收、素材理解的真实模型质量验收、修改后的完整实端点创作流程、独立搜索/参考发现及同模型生产与审核的独立质量评估。百炼声画和明确参考链接任务属于新增实现，服务加载状态与实测缺口见状态文档；它们不等于完整成片自动验收。人工观察和外部观察导入仍可使用。主控仅能准备制作方案，不能提交生成或批准图片；策划批准不自动扩展为制作及费用授权。

## 图片分析与采用

项目素材 → 分析图片 → 选择服务、视觉模型及用途 → 确认一次调用 → 核对结果 → 采用所选观察。上传文件本身不调用模型。已有 OpenAI/Anthropic 兼容连接可用于图片输入，但具体模型必须支持视觉；DeepSeek和Codex CLI尚未接入此图片入口；视频使用独立声画/抽帧观察入口。支持静态 PNG/JPEG/WebP，最多 4 MB、单边 8000 像素、2000 万总像素；先验证真实文件内容及非动画属性，再传输原图。

采用现有 `/api/runs/{run_id}/jobs/{job_id}` 查询与取消；创建入口为 `POST /api/runs/{run_id}/image-analysis`，包含 `request_id`、文件名和 `expected_sha256`、连接 `expected_revision`、`connection_id`、视觉 `model`、`purpose`（product/reference）及显式 `consent: true`。同请求标识与同参数只执行一次，不自动重试、不修改角色配置、不把图片字节或密钥记入事件日志。调用复用 Ledger，未知用量不阻止再次调用；无准确价格时保留金额 null。

`GET /api/runs/{run_id}/image-analysis` 返回可用连接与候选记录。模型结论先保存在原 run 事件日志，未采用前不会进入素材依据；`POST .../image-analysis/{analysis_id}/confirm` 绑定 `expected_candidate_sha256`、`expected_manifest_sha256` 与 `selected_findings` 索引，只将用户所选观察和全部限制追加至既有 `evidence/observations.json`。同一选择重复确认不追加，不同选择及旧图片拒绝；修正通过补充人工观察保留历史。观察不是性能验证或制作批准。

图片输入格式参照 [OpenAI 图片输入文档](https://developers.openai.com/api/docs/guides/images-vision) 与 [Anthropic 视觉文档](https://platform.claude.com/docs/en/build-with-claude/vision)，2026-09-19 核对。离线协议测试验证实际图片块和台账边界，不代表任一真实兼容端点已通过验收。

## 局部修改与版本比较

「创意方向 / 脚本 → 编辑方向或镜头 / 比较版本」可逐个编辑现有方向或镜头。文字暂存在当前浏览器窗口，点击「保存修改草稿」后写入项目既有事件日志；保存不调用模型、不改当前方案、不改变审核或采用。草稿绑定准确原稿版本与指纹，重复保存请求不重复写入，不能套用到已有新版本或依据已变化的方案。

「整理为新版本」会展示对应导演/编剧的当前模型，明确确认后进行一次模型调用，将修改同步至完整正文。卡片由用户修改确定；其他对象、素材绑定、证据和限制保留，完整正文仍需独立审核。新版本不继承旧审核与批准，下游依赖失效；改动时码或时长会将总时长标记为待复核，并增加节奏复核说明。模型调用使用既有任务与台账，不自动重试；取消时若已收到完整返回则保留未审核版本。未知用量不阻断继续。

当前支持修改已有对象字段，不支持增加、删除、重排镜头或改方向编号。历史格式、依据已变化、尚无结构卡片的方案需先由制片团队更新；旧版仍可阅读和比较。「版本比较」显示内容前后值，依据记录和正文差异按需展开，长正文差异有截断提示。

- `GET /api/runs/{run_id}/edits/{stage}?version=0`：读取当前或指定版本、编辑资格、草稿及可比较版本；stage 为 direction/writing。
- `POST .../edits/{stage}`：保存 `request_id`、`expected_version`、`expected_sha256` 与 `edits`（row、field、value）。
- `POST .../edits/{stage}/{draft_id}/generate`：绑定 `request_id`、`expected_draft_sha256`、连接 `expected_revision` 与显式 `consent: true`；查询、取消复用现有 jobs API。
- `GET .../artifacts/{stage}/compare?from_version=1&to_version=2`：只读比较两个完整性校验通过的版本。

以上已通过隔离假模型与界面测试；真实模型对正文与卡片的一致性、创意质量尚未验收，不能以状态机测试代替内容审核。
## 商品资料复用、项目参考与就地反馈

- 「需求整理 → 商品资料」可整理名称、商品信息、证据来源、未知与限制，另存不可变资料卡；「新建项目 → 复用商品资料」先核对再选用。资料卡保留准确来源版本、保存日期及来源限制；新的需求明确标注为未重新核验的历史快照，不复制原方案、素材或批准。仅 US 项目可复用，不替代商品事实源。
- 「灵感与方法 → 案例详情 → 应用到当前项目」可指定表达用途和本次想借鉴的内容；「本轮参考」可查看、移除。只有已具备用途资格的案例能应用，选择本身不调用模型。后续创作者上下文加入明确选择及表达范围，审核角色不接收这些偏好表达；来源变化需重新核对。参考调整会使方向及下游旧稿失效，需求整理不受影响。
- 稿件正文后的「留下具体反馈」绑定当前所读版本，可填写具体位置、范围与原话。默认预览可读的新增内容，完整变更记录按需展开；核对后保存到既有学习库，既不自动改稿也不继承批准。未提交文字暂存在本窗口，已保存反馈可刷新找回。

三项操作均不调用模型。商品资料卡和项目参考使用原 run 事件日志，反馈复用当前学习库及原有差异审核，不新增第二套业务事实源。资料保存、参考调整、新建项目具备重复请求保护和来源校验；前后端更新期间缺少新参考接口仅停用新工具，不影响既有灵感与方法阅读。

接口：`GET /api/product-cards`；`GET/POST /api/runs/{run_id}/product-cards`；新建项目可附 `product_card` 精确绑定；`GET/POST /api/runs/{run_id}/references`；`POST /api/runs/{run_id}/references/{case_id}/remove`。就地反馈复用现有 learning proposals 预览与 apply 接口。
## 模型选择、项目费用与作品表现

模型设置区分文字策划角色与素材区单独发起的图片分析。配置完整度只是当前字段是否齐全，不代表真实调用通过；更换服务提供方会清空受影响角色的旧型号，服务地址变化不沿用旧价格。价格按准确服务与模型显示，保存仅影响后续任务。现有 Codex CLI 登录和 API Key 机制保持不变。

「用量与费用」读取当前项目既有台账，分别显示调用次数、已记录 token、已知美元估算、媒体人民币估算及未知金额次数，可按角色或模型查看，并导出 CSV。金额未知不等于免费，用量未知与价格未知分开；未返回用量仅作提示，不提供手工核账入口。失败/取消仍可能产生费用。历史金额不套用当前价格，不显示提供方剩余额度，不自动获取账单。

「作品与表现」在项目顶部进入，关联已经发布的公开 HTTPS 作品链接与用户明确选择的脚本历史版本，记录实际作品相对稿件的差异。这里只保存本地关联，不访问链接、不发布、不读取平台账户，不代表成片已验收或获得发布授权。

每条表现观察须注明统计开始/结束时间、观察时间、流量来源、数据来源与口径、观察限制；播放、点赞、商品点击、归因订单、美元销售额与广告花费可留空，未知保持 null。单次修正追加新记录，原记录保留；不自动计算跨区间趋势、ROAS 或效果排名。每条记录标记用户报告、未经自动核验。可以将指定记录带到制片对话草稿，核对后再发送复盘；不会自动改写学习库或启动模型。

- `GET /api/runs/{run_id}/outcomes`：可核对的 writing 版本与作品观察历史。
- `POST /api/runs/{run_id}/outcomes`：绑定 `request_id`、`version`、`artifact_sha256`、作品名称、链接、带时区发布时间与实际差异。
- `POST /api/runs/{run_id}/outcomes/{publication_id}/snapshots`：绑定 `request_id`、`expected_latest_id`，保存统计区间、指标及来源。拒绝未来日期、非法数字、旧记录并发覆盖和无法核对的源稿；相同请求幂等。浏览器保存未确认时保留冻结请求，重开后先识别已保存结果。

作品和表现继续使用原 run 的不可变事件记录，不另起采集器或经营数据库；不会回填 Product Intel 或覆盖线路内业务事实源。该作品记录模块不自动采集平台数据或判断经营效果；媒体制作的独立入口见下文。
## 图片与视频制作

同一工作台的「分镜」「成片」承接媒体执行。主制片通过 `read_production` / `prepare_production` 准备实际生成描述，通过 `review_production` 完成内部文字制作检查；高级面板保留手动编辑。方案保存不调用模型，文字审核属于模型调用；图审和媒体生成提交仍由用户完成。

1. 主制片依据已经批准的准确脚本准备制作方案；内部保留不可变版本，修改不会覆盖原记录。
2. 团队完成独立文字制作检查。审核角色只获得当前 brief、准确脚本、实际 prompt、参数、素材证据与适用方法，不能读取生产者对话，也不会谎称看到图片像素。准备问题由团队修正；检查通过仍不是生成授权。
3. 视频参考图须在大图界面由用户确认。批准绑定原始文件名与 SHA256；换图后旧批准不能沿用，可明确撤回。
4. 核对实际 prompt、图、时长、清晰度和费用后，用户勾选同意并启动一个生成任务。保存、打开页面和轮询不触发生成。
5. 输出校验后写入本 run 的 `materials/`，保持原记录、任务与费用。生成成功仅表示文件已保存，实际画面、声音与创意效果仍待验收。

当前图片使用本机 Codex CLI 原生图片工具及其现有登录。独立临时线程只允许图片工具，参考图冻结后以像素输入；不复用开发对话或文字角色适配器。已核实本机协议可用，尚未完成真实出图验收。原生协议不提供图片计费用量，也不能硬性保证工具只调用一次；发现额外调用会停止本地等待，保留可验证的部分输出和未知用量。图片已保存即可继续图审；未返回计费用量不会阻止下一次调用。不会把文件张数当收费量、把订阅折算成免费或杜撰余额。CLI 自身可能保留 generated_images 缓存；工作台只读取返回的图片字节，不按回执任意路径读文件。

视频复用原 Windows 用户已有的百炼 DPAPI 凭据，仅在明确提交或查询原任务时解密，不修改实际配置、不复制凭据。当前接入北京 `wan3.0-video` 单首帧方式：2–30 秒、480P/720P/1080P，跟随参考图比例、原生声音开启、关闭自动 prompt 扩写；参数随方案冻结。发送原始参考图 data URL，视频参考需满足提供方至少 240 像素、最大边 8000、比例不超过 8、最多 20 MB 的条件。工作台图审允许静态 PNG/JPEG/WebP，最多 24 MB / 4000 万像素，不能静默压缩已批准图片以凑接口要求。

百炼公开原价估算按 ¥0.3 / ¥0.6 / ¥1.2 每秒分别计算；日期 2026-09-19，来源 [官方 Wan 3.0](https://help.aliyun.com/zh/model-studio/wan3-0-video)。实际优惠与账单未核验。视频人民币估算单独显示，不与文字美元金额相加，也不声称受文字模型美元预算覆盖。媒体调用写入同一 `ledger.jsonl`：`usage_unit=images|video_seconds`、`usage_quantity`、`estimated_cny`；文字 Tokens 不凭空补 0。未知媒体用量仅保留记录，不阻止新的模型调用；查询和取回原任务不重发生成。

恢复语义：request_id 保证原请求重试不重提交。百炼接受后立即保留 task_id 和固定 workspace endpoint；下载失败或服务重启后从原任务查询，不能自动换空间或再次提交。提交超时且没有 task_id 时，用户凭提供方证据补充任务编号，或明确登记未提交；已有任务/输出不能记作未提交。图片计量或视频秒数仅记录提供方返回值，缺失不要求用户手工核账。关闭制作对话框只停止页面轮询；停止制作执行仅结束本地等待，远端仍可能生成并计费。

制作基线：首份方案必须来自完整且批准的当前 writing。之后同一制作基线可接收本流程生成的派生媒体，不让每次输出迫使脚本重写；原素材、观察、脚本、上游版本、方向选择、参考选择或审批变化仍使该基线失效。创作板的原有素材依赖机制不被放宽。新基线仍需要更新稿件并明确批准。

接口位于 `/api/runs/{run_id}/production`：GET 读取能力/方案/素材与状态；POST `/drafts` 免费保存；`/material-approvals` 为浏览器图片决策；`/{draft_id}/review` 独立检查；`/{draft_id}/generate` 明确生成；`/attempts/{attempt_id}/refresh` 查询原视频任务；`/attempts/{attempt_id}/recover` 绑定原任务或依据提供方证据登记提交状态。未知计费用量不要求手工补齐。浏览器专属确认 nonce 不进入模型上下文。真实生成未在本轮离线验收中执行，不能以 mock 返回宣称提供方连通或质量通过。
## 显式接续旧案例

项目顶部「项目历史」读取迁入的准确原文、未采用草稿、用户反馈、历史批准及费用说明。主制片使用 `read_project_history` 按登记 ID 读取相关文本；独立审核角色不读取旧生产者对话。历史原件只作资料，不带执行权限。

本地管理员可用 `python -m studio.history_import --plan <PLAN_JSON> --data-root <DATA_ROOT>` 校验明确计划，加 `--apply` 才执行。计划要求 US 线路、全新 run_id、接档 brief/intake/summary、历史费用边界、明确来源目录（可附 files 精确白名单）、精选文本及已核对 hash 的图片/视频。没有任意文件导入的 HTTP 入口。

应用时必须停止同数据根的服务；单 writer 锁拒绝在线并写。完整原件复制到独立暂存、校验 hash，最后才将完成项目发布到 runs；失败暂存保留供核查，目标已存在时拒绝覆盖。原 Backlot 不改写，旧脚本不执行。只有精选 md/txt/json 通过只读接口返回，原始 HTML、脚本、日志和提供方签名 URL 不作为站内页面或模型历史提供。商家源素材与历史生成素材分别标注；未重做视觉观察。

迁移仅建立接档 intake 和 pause 路由，不迁入有效审批、当前生成任务或费用台账，不创建模型对话、不调用模型。用户在站内明确继续后再选择 continue/edit；新执行仍受本站图审、费用和版本限制。本站调用费用与历史费用说明分开保留，未知实际金额不补零。


已迁入的「小号多功能包 · Gym girl」对应 `bag-small-gym-studio-20260919`。在「项目历史」核对原片反馈、准确旧稿和 v14 未采用草稿，在「项目素材」阅读原媒体，再于制片对话明确继续改进。迁入时保持 pause；旧六图方式尚不能直接套用当前单首帧视频入口。归档规模和验收证据见 [实现与验收状态](docs/product-improvements.md)。
