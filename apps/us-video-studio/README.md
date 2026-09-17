# US 视频制片室（us-video-studio）

US 视频前期策划的本地独立工作台：用户在应用内与主 Agent 对话，主 Agent 调配角色 Agent，独立审核 Agent 给出评审，再由用户决定方向与脚本。范围止于策划，不含图片/视频生成、发布或市场效果判断。

[Codex 驱动工作台](../video-workbench/README.md) 与本应用分别维护、互不写入运行状态；不自动迁移历史批准。业务采用状态仍以 [US 线路](../../tracks/us/README.md)、[午餐包当前状态](../../tracks/us/docs/lunch-video-current-state.md)及飞书 Tasks 为准。开发约束见 [CLAUDE.md](CLAUDE.md)。

## 启动与检查

真实运行数据默认位于仓库外 `J:/E-Business-Video-Data/us-video-studio/`，素材、台账、配置与凭据不得提交 Git。一个数据根只允许一个 Studio 写入进程；离线导入或恢复前停止该数据根的服务。

以下在仓库根执行。依赖文件仅补充本机已有 Python 环境，不能当作完整生产安装清单：

```powershell
python -m venv --system-site-packages 'J:/E-Business-Video-Data/us-video-studio/runtime-venv'
& 'J:/E-Business-Video-Data/us-video-studio/runtime-venv/Scripts/python.exe' -m pip install -r apps/us-video-studio/requirements-local.txt

# 离线测试：不连接模型，不产生费用
& 'J:/E-Business-Video-Data/us-video-studio/runtime-venv/Scripts/python.exe' -m pytest apps/us-video-studio/tests -q
node --test apps/us-video-studio/tests/js/*.mjs

# studio 是应用目录内的 Python 包，启动前切换目录
Set-Location apps/us-video-studio
& 'J:/E-Business-Video-Data/us-video-studio/runtime-venv/Scripts/python.exe' -m studio.server --port 4760
```

默认仅绑定 `127.0.0.1`，访问 `http://127.0.0.1:4760/`。本应用没有公网身份验证，未配置开机启动或公网部署；不要直接暴露服务。

| 配置入口 | 含义 |
| --- | --- |
| `STUDIO_DATA_ROOT` | 数据根；未设时使用上述默认目录 |
| `<data-root>/config.json` | 角色模型、端点、预算软阈值、价目表与循环限制；在 Git 外维护 |
| `STUDIO_BASE_URL` | 无 `config.json.base_url` 时使用的端点；再回退到代码默认值 |
| `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` | 模型凭据，仅在调用时从进程环境读取；前者优先 |

实际请求使用 `settings.base_url`。`ANTHROPIC_BASE_URL` 不控制 Studio 服务端请求。默认角色模型为 `deepseek-flash`，可配置值与循环参数以 [config.py](studio/config.py) 为准；修改真实凭据或运行配置需遵守仓库授权边界。

### 隔离界面预览

预览使用手写方案、审核和台账数字；它们不是实际创意结论、用量或批准。`scripts/seed_preview.py` 会从其明确指定的本地产品目录复制素材，素材与演示文案并非同一商品；执行前先检查来源和目标。脚本仅接受不存在的新数据根，拒绝覆盖已有目录。

在应用目录、专用 PowerShell 会话执行，下例目标目录必须尚不存在：

```powershell
& 'J:/E-Business-Video-Data/us-video-studio/runtime-venv/Scripts/python.exe' scripts/seed_preview.py 'J:/E-Business-Video-Data/us-video-studio-preview-new'
$env:STUDIO_DATA_ROOT = 'J:/E-Business-Video-Data/us-video-studio-preview-new'
$env:ANTHROPIC_AUTH_TOKEN = ''
$env:ANTHROPIC_API_KEY = ''
$env:STUDIO_BASE_URL = 'http://127.0.0.1:9/dead'
& 'J:/E-Business-Video-Data/us-video-studio/runtime-venv/Scripts/python.exe' -m studio.server --port 4762
```

该会话禁用模型凭据并使用不可用的本地端点；不要在预览根添加真实端点配置。三个 fixture 分别展示方向待采用、脚本审核退回和全部完成；包含一个故意引用不存在文件的镜头，用于检查降级提示。停止服务后保留或按明确授权清理预览，不能据此删除其他运行数据。

## 界面与工作流

界面采用炭黑、暖灰与香槟金，阶段目录、方案编号和镜头接触表构成独立制片室视觉。主题在 [cinema.css](studio/web/cinema.css)，行为布局在 [style.css](studio/web/style.css)；使用本机字体回退，无外部字体、图片或前端框架依赖，支持窄屏和减少动态效果设置。

四个阶段为 `intake → direction → material_plan → writing`。需求整理呈现事实、约束和未知项；创意方向展示多个选项与推荐依据；素材计划说明可用素材与缺口；脚本呈现镜头时长、画面、台词和声音。`direction` 与 `writing` 必须经过独立审核及用户决定；本应用尚无 `layout_preview` / `ad_preview` 渲染阶段。

- 桌面阅读与决策分栏，窄屏把决策放到正文之后；原文阅读器支持版本切换，素材抽屉支持预览和上传。
- 推荐不等于采用。用户明确选中方向再提交，批准记录保存 `selected_option_id`；后续角色读取实际选择。
- 退回修改需要意见；覆盖审核退回需要明确理由，记录为 `approved_override`。批准仅覆盖该版策划，不授予生成、费用或发布权限。
- 决策草稿按运行、阶段和产物摘要保存在本机浏览器。保存中阻止重复提交；冲突保留草稿，要求重新阅读最新版本。审核退回和上游失效不显示为完成。
- 费用入口展示真实记录的已知用量与待核实项；历史阶段用量是参考记录，不是下一次费用预测。

领域状态与业务措辞主要由 [vocabulary.py](studio/vocabulary.py) 提供，前端负责呈现与交互校验。审核通过时仍展示注意事项，不把 `pass` 当成内容可用保证。

## 架构与版本契约

```text
浏览器 studio/web (REST + SSE)
  → server.py
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

阶段写入锁覆盖同进程中同一数据根的多实例；模型调用与费用恢复另有 OS 锁。这不构成多服务写入支持：同一数据根仍须遵守单 writer 约束。

### HTTP 集成

| 端点 | 用途 |
| --- | --- |
| `GET/POST /api/runs` | 列出或创建运行 |
| `GET /api/runs/{id}/state`、`/timeline` | 领域状态或界面投影；只读推导 |
| `GET /api/runs/{id}/chat`、`/events`、`/ledger` | 对话、历史事件、按调用合并后的台账 |
| `GET /api/runs/{id}/artifacts` | 产物列表，可按 `stage` 筛选 |
| `GET /api/runs/{id}/artifacts/{stage}/{version}` | 指定版本正文、展示结构与完整性校验 |
| `POST /api/runs/{id}/message` | 启动主 Agent 轮次，通过 SSE 返回进度 |
| `POST /api/runs/{id}/nonce` | 为页面实际展示版本签发审批 nonce |
| `POST /api/runs/{id}/decision` | 用户提交采用或退回决定 |
| `GET/POST /api/runs/{id}/materials` | 查看素材列表或上传素材 |
| `GET /api/runs/{id}/materials/{name}` | 读取素材文件 |

nonce 请求的查询参数必须包含 `stage`、`expected_version` 和 `expected_artifact_sha256`。决定 JSON 必须携带相同三项，加上 `nonce`、`decision`（`approved` / `returned`）；采用含选项的方向时还必须有有效 `selected_option_id`，退回填写 `note`，覆盖退回审核填写 `override_reason`。

这些值取自用户实际阅读的产物，不得提交前偷偷读取最新版替换。服务端只落盘 nonce 摘要；相同 nonce 与相同决定重试幂等，更改选项、理由或决定则拒绝。模型轮次执行中、版本冲突或失效时返回 `409`；必填字段缺失返回 `422`，无效阶段返回 `400`。nonce 不是公网身份验证。

### 素材与观察

素材位于 `runs/<id>/materials/`，可从界面上传或在服务停止时本地放入。按文件名引用，缺文件时明确提示；视频由浏览器预览，不转码。上传检查扩展名、64 MiB 单文件上限和最终路径，拒绝同名覆盖；不直接创建阶段产物或批准，但素材摘要变化会使相关依赖失效。

外部观察可写入运行的 `evidence/observations.json`，须绑定文件名、hash、观察层级、方法、覆盖范围、观察者、日期、发现及限制；格式与校验见 [evidence.py](studio/evidence.py)。没有有效观察时仅记 `existence_only`。模型不能通过工具写入观察以冒充自己看过素材。

## 用量、费用与恢复

每次模型 HTTP 请求先持久化 intent，再追加 settlement；台账按 `call_id` 合并有效记录。传输不自动重试，不完整结束、畸形工具参数或缺最终用量不能记作成功。完整返回 `max_tokens` 时，角色循环可在配置限制内发起单独计费的续写；这与重发中断请求不同。

未知用量或价格保留为未知，美元金额为 `null`，不能补零。已知小计、未知调用和待核实状态分别显示；任一运行存在未核实用量都会阻止该数据根继续调用模型。金额是价目表估算，不是提供方账单。

默认总量软阈值为 2,000,000 token，美元软阈值为 10；仅确认有效价目表后美元阈值才可执行。上限跨该数据根所有运行累计，在调用前检查，最后一笔可能越过阈值，不保证绝不超额。

以下命令在应用目录执行，显式指定数据根，不调用模型、不批准内容、不自动重跑。先停止对应服务；只有取得提供方用量或未受理证明才能对账，不得从部分输出猜 token。

```powershell
python -m studio.recovery --data-root <DATA_ROOT> pending
python -m studio.recovery --data-root <DATA_ROOT> reconcile <RUN_ID> <CALL_ID> --input-tokens <N> --output-tokens <N> --evidence-note "提供方用量记录或未受理证明的出处"

# 关闭崩溃遗留的指定开放尝试；不清除原稿、历史或未知费用
python -m studio.recovery --data-root <DATA_ROOT> attempts <RUN_ID>
python -m studio.recovery --data-root <DATA_ROOT> abandon <RUN_ID> <STAGE> --attempt-token <TOKEN_FROM_ATTEMPTS> --reason "中断核查说明" --server-stopped
```

恢复后由用户明确要求继续，系统不自动重复已完成步骤。

### 导入 ContentInput

先创建空 run，停止该数据根其他写入，再在应用目录执行：

```powershell
python -m studio.input_import --data-root <DATA_ROOT> --run-id <RUN_ID> --path <CONTENT_INPUT_JSON> --repository-root <REPOSITORY_ROOT> --allow-root <REPOSITORY_ROOT> --allow-root <MEDIA_ROOT>
```

导入器复用仓库 [content_handoff.py](../video-workbench/openmontage/lib/content_handoff.py) 的校验，仅允许读取显式指定根目录中的来源；保留完整输入字节、事实层级、观察覆盖、未知项和来源指纹。拒绝覆盖已有 intake 或输入文件，来源改变会使依赖失效。导入是本地显式入口，不是任意文件读取 HTTP API；包中的授权字段不自动授予本应用生成、费用或发布权限。

## 验证范围与剩余工作

2026-09-17 修复与视觉改造使用离线模型 fixture 和隔离浏览器验证；覆盖版本冲突、真实方向选择、台账恢复、输入来源、原文切换及桌面/1024px/390px 布局。对应审查记录见 [方案核验](../../tracks/us/docs/studio-plan-review-2026-09-17.md)。测试命令见上文，以执行结果为准。

更早的兼容端点探索不能代替本轮传输与审批修改后的完整实端点验收。尚未完成：独立搜索/参考发现、直接视觉与声画理解、修改后的真实端点完整流程验收，以及真实运行数据切换验收。外部观察导入是过渡入口。

同模型生产与审核的创意质量仍需独立评估；审核通过、离线测试通过、渲染成功均不能证明内容可用或市场有效。本应用没有图片/视频生成工具，不把策划批准扩展为制作授权。
