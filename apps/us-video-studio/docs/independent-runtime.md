# 独立运行、架构与运维

## 目标与边界

独立制片室承接 US 视频与图文策划、用户明确提交的 Codex 图片和百炼单首帧视频制作。需求、角色执行、失败记录、版本及审批在站内完成；Codex 桌面开发对话只维护代码，不充当不可见业务 worker。Backlot 原记录独立保留；显式接档不会迁移有效批准、当前生成任务或费用授权。

范围以 [README](../README.md) 的用户流程与接口契约为准；实施与真实验收的区别见 [实现与验收状态](product-improvements.md)。本应用只绑定本机，没有公网身份验证，不适合多人共享部署。

## 模块与数据流

```text
浏览器 studio/web：明确请求 + request_id，读取进度与已保存版本
  → server.py：依赖组合、生命周期、本机请求边界与路由安装
      → http_*.py：学习交付、项目、素材、决定、连接与任务接口
      → jobs.py：任务持久化、单 writer、取消、重启中断、幂等
      → connections.py：DPAPI 凭据、角色路由、每轮配置快照
      → agents/orchestrator.py → tools/dispatch.py → 独立角色上下文
      → pipeline.py / store.py：尝试、不可变产物、审核、用户批准、依赖失效
      → ledger.py：调用 intent → settlement，未知用量留作诊断，不阻止新调用
      → image_analysis.py / revisions.py：分析候选及采用、局部修改新版本
      → learning.py / references.py：唯一学习库、实际输入快照、差异入库
      → production.py → codex_image.py / wan_video.py：显式制作、原任务回收
      → project_history.py：受控历史阅读；history_import.py 仅离线迁移
      → product_cards.py / outcomes.py：来源明确的资料复用、作品表现登记
```

业务与费用记录继续使用文件和追加事件，没有新建任务数据库或第二套学习库。每个数据根只有一个写入服务，同一时刻执行一轮 Agent 任务；冲突明确拒绝，不隐藏排队。单 run 的版本锁不能替代数据根的 OS writer 锁。服务、离线恢复和导入共同取得 writer 锁，因此离线操作前必须停止相应服务。

### 存储

数据根由 `STUDIO_DATA_ROOT` 指定，未设置时才回退到历史默认值 `J:/E-Business-Video-Data/us-video-studio/`。当前机器的操作入口见 README，不能把旧盘符当作已恢复目录。全部运行数据在 Git 外：

| 路径 | 含义 |
| --- | --- |
| `config.json` / `connections.json` | 预算与循环限制 / 站内角色路由、价格及 Windows DPAPI 加密凭据 |
| `.studio-root.json` | 显式新建空工作台时留下的初始化标识；不能用 writer 锁文件代替项目身份 |
| `runs/<id>/run.json`、`events.jsonl` | 初始项目及追加事件；任务、修订、制作、反馈等状态从事件推导 |
| `runs/<id>/artifacts/`、`reviews/`、`approvals.jsonl` | 不可变正文与 sidecar、独立审核、用户决定 |
| `runs/<id>/ledger.jsonl` | intent 与 settlement；按 call_id 合并，不能删历史清除未知用量 |
| `runs/<id>/materials/`、`evidence/observations.json` | 原字节素材与准确 hash 绑定的观察；生成来源单独标记 |
| `runs/<id>/learning/`、`creative-packages/` | 输入和提案快照 / 确定性编译的本地审阅包 |
| `runs/<id>/requests/` | 每次模型调用的不可变逻辑输入快照；可能包含业务资料，不是提供方网络请求已发送的证明 |
| `runs/<id>/history/manifest.json`、`history/archive/` | 显式迁入清单与只读原件；仅精选文本可站内读取 |
| `import-staging/` | 迁移暂存，全部复制校验后才发布新 run；失败保留供核查 |

唯一案例与方法库仍位于仓库的 `tracks/us/creative/learning/library.json`。用户明确应用差异会有意修改该文件，并在 run 中留下前后字节与审计；测试必须用临时库。业务采用状态仍由线路文档和飞书 Tasks 承载。

### 执行与恢复

1. **浏览器不拥有任务生命周期。** 服务先保存任务事件，再执行；相同 request_id 与相同参数重试不重复提交。刷新、关闭窗口或切换页面不取消已接受任务，页面用 jobs API 重连。
2. **中断不自动重发。** 服务启动将遗留任务标为 interrupted。用户查看已保存产物后明确继续，开放阶段尝试仍需处理；未知用量不要求手工核账。停止是协作取消，在途模型或远端视频仍可能完成和计费。媒体提交结果不明须找回原任务，不能将其与用量未知混为一谈。
3. **输入与批准固定版本。** artifact-v2 绑定正文、结构、证据、限制及依赖。审批针对页面实际展示的版本/hash/方向；body-v1 只回放，不继承旧批准。上游、图片、观察或参考变化使受影响产物失效。
4. **角色和凭据隔离。** 每轮冻结连接/模型/价格，审核不读生产者完整对话。Codex 每次使用独立临时线程，不注入开发对话；仍可能加载用户全局 AGENTS.md，不宣称 OS 级完全隔离。API Key 只在服务端按用途解密，不进模型、日志或浏览器存储；地址变化不转交旧 Key。
5. **费用不虚构。** 文字 token 与媒体 images/video_seconds 共享台账；美元与人民币估算分列，未知金额不补零。未知用量仅作诊断，个人工作台没有手工核账待办；实际账单在提供方平台查看。报价不等于账单，也不代表硬性支出上限。
6. **制作单独授权。** 主控准备方案并调用独立文字制作检查，用户在同一工作台核对输入、准确图审和费用后明确生成。百炼保存原 endpoint/task_id，恢复只查询原任务；Codex 图片可能没有可靠的原生计费量，保持未知也不阻止继续。协议和能力限制见 [Codex](codex-image-capability.md)、[Wan](wan-video-provider.md)。
7. **迁移不恢复执行。** history_import 只读来源，在隔离目录复制/校验后创建新 intake 与 pause；不复制有效审批或费用台账，不运行旧脚本。用户在站内明确继续，新一轮重新经过版本、审核与费用边界。
8. **删除可恢复。** 项目删除/恢复追加到原事件流，不移动或清除目录。删除项目从列表隐藏、禁止新任务，原台账仍计入汇总；活动任务未结束时不能删除。回收站恢复原身份和内容，不启动模型。

## 接口与集成入口

完整请求字段和用户交互见 [README](../README.md) 各功能节。接口均为本机 API，nonce 和 request_id 不构成公网身份验证。下表中 run 前缀为 `/api/runs/{run_id}`。

| 领域 | 入口与职责 |
| --- | --- |
| 项目/产物 | `/api/runs`；run 的 `/state`、`/timeline`、`/chat`、`/events`、`/ledger`、`/artifacts`；GET 指定版本，POST `/nonce` 与 `/decision` 执行用户决定；决定已保存但后续任务未创建时 POST `/decision-continuation` 幂等接续 |
| 对话入口/回收站 | POST `/api/conversations` 幂等建立项目；DELETE `/api/runs/{run_id}` 移入回收站，GET `/api/trash` 列表，POST run 的 `/restore` 恢复 |
| 连接/任务 | GET/PUT `/api/connections`；GET `/api/capabilities` 与 `/api/runtime` 分别给出声明能力和已加载进程信息；GET `/api/connections/codex/models` 从本机 CLI 只读列出可见文字模型，POST `/api/connections/deepseek/check` 只读验密钥；run 的 POST `/jobs`、GET `/jobs/latest` 或 `/jobs/{job_id}`、POST `/jobs/{job_id}/cancel` |
| Codex 登录 | GET `/api/connections/codex/status`；POST `/api/connections/codex/login` 显式启动官方浏览器登录，POST `/api/connections/codex/login/cancel` 取消对应 attempt_id；与模型任务互斥，不读取凭据或自动登出 |
| 恢复 | GET `/recovery`；POST `/recovery/abandon` 关闭准确遗留尝试。`/recovery/reconcile` 仅为保留的开发兼容入口，不是用户继续前置条件 |
| 素材/分析 | GET/POST `/materials`、`/material-observations`、`/image-analysis`；GET `/materials/{name}`；POST `/image-analysis/{analysis_id}/confirm` 逐条采用 |
| 修订 | GET/POST `/edits/{stage}`，POST `/edits/{stage}/{draft_id}/generate`；GET `/artifacts/{stage}/compare` |
| 学习/参考 | `/api/learning` 与 run 的 `/learning`；POST `/learning/proposals` 和 `/learning/proposals/{proposal_id}/apply`；GET/POST `/references`，POST `/references/{case_id}/remove` |
| 复用/作品 | GET `/api/product-cards`；run 的 GET/POST `/product-cards`、`/outcomes`；POST `/outcomes/{publication_id}/snapshots` |
| 交付 | GET `/delivery`、`/delivery/download`、`/creative-packages`、`/creative-packages/{package_id}/files/{filename}` |
| 制作 | GET `/production`；POST `/production/drafts`、`/production/material-approvals`、`/production/{draft_id}/review`、`/production/{draft_id}/generate`、`/production/attempts/{attempt_id}/refresh` 和 `/recover` |
| 历史 | GET `/history`、`/history/{document_id}`；没有 HTTP 导入或任意文件读取入口 |

变更接口要求准确版本或修订号，状态冲突通常返回 409，输入不合契约为 400/422，已删除项目返回 410；不要把冲突重试成新业务请求。审批与生成是页面的人类操作，不开放给 Agent 工具。每种接口的字段以对应模块 Pydantic 类型及 README 为准。新增参考视频任务的协议见 [声画与参考任务](bailian-omni-and-tiktok-import.md)，是否已加载须核对 [当前状态](product-improvements.md)。

## 本机运维

启动、依赖和离线测试命令见 [README](../README.md#启动与检查)。`start.ps1 -CheckOnly` 不写业务状态；首次显式 `-AllowNew` 启动空工作台会写 `.studio-root.json`，之后可正常重启。环境兼容项：`STUDIO_DATA_ROOT` 选择数据根；旧客户端的 `STUDIO_BASE_URL` / `ANTHROPIC_BASE_URL` 与 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` 不会自动导入站内连接。Codex 优先从服务进程 PATH 定位；Windows 未配置 PATH 时使用当前用户桌面应用安装目录内的 CLI（具体优先顺序见 README），无需打开桌面客户端；百炼通过 `%LOCALAPPDATA%/E-commerce/credentials/` 使用原用户 DPAPI 凭据，只在明确提交或查询原任务时解密。

服务更新前先确认目标端口、对应 studio.server 进程和数据根；查看各项目 recovery，确认活动任务、开放尝试、未知用量，保留原记录再正常停止对应服务。不要因更新而清空 ledger、覆盖运行目录或启动第二个 writer。若执行工具拒绝停止/启动，记录实际拒绝，不宣称已加载；再次操作前重新查端口与进程，不使用过期 PID。服务重新启动只恢复本地状态，不自动重发模型或视频请求。

以下 PowerShell 是只读冒烟，不调用模型；`<RUN_ID>` 替换为 `/api/runs` 返回的实际项目 ID。不要将响应中的业务内容或费用明细复制到公开日志。

```powershell
Invoke-RestMethod 'http://127.0.0.1:4760/api/runs'
$studioRunId = '<RUN_ID>'
Invoke-RestMethod "http://127.0.0.1:4760/api/runs/$studioRunId/recovery"
Invoke-RestMethod "http://127.0.0.1:4760/api/runs/$studioRunId/production"
Invoke-RestMethod "http://127.0.0.1:4760/api/runs/$studioRunId/history"
```

| 现象 | 核查与处理 |
| --- | --- |
| 页面新版但接口 404 | 核对端口和后端启动版本；静态资源刷新不等于 Python 进程加载新代码 |
| writer 锁冲突 | 确认是否已有服务/导入占用该数据根；不删除锁文件伪造解锁，不并行启动 |
| 页面仍要求核账后继续 | 核对是否加载旧服务/旧静态文件；当前未知用量不阻断。分别检查开放阶段尝试、正在执行的任务和媒体提交歧义，不填猜测用量解锁 |
| 提交结果不明 | 保留同一 request_id，确认原请求状态；视频按原 endpoint/task_id 查询，不新建任务重试 |
| 历史内容指纹冲突 | 检查归档完整性，保留失败现场；不改 hash 让校验通过，不读其他任意路径替代 |
| 模型服务不可用 | 核对当前角色连接和准确模型 ID；密钥列表验证通过不代表余额、生成或完整创作质量通过 |

恢复 CLI 和 ContentInput/history_import 命令均在 README。它们需要停止同数据根服务；history_import 不带 `--apply` 仅校验计划，目标存在时拒绝覆盖。迁移失败暂存留作证据，不能当作完成项目继续。

## 已有验收边界

2026-09-19 的离线及浏览器验证覆盖上述持久任务、状态/版本、恢复、连接、制作及迁移边界；最新历史检查数量见 [实现与验收状态](product-improvements.md)。另有一次已授权的真实最小 Codex 文本请求，9,730 输入 / 22 输出 token，美元费用未知。它不验证真实业务工具回合、图片/视频出图、第三方端点兼容性或内容与市场效果；也不构成后续调用授权。
