# us-video-studio 目录约定

本文件只约束本应用。跨项目规则见仓库根 [AGENTS.md](../../AGENTS.md) 与
适用的全局规则，线路事实源见 [tracks/us](../../tracks/us/README.md)。

## 定位

US 视频与图文策划、受控图片/视频制作的本地独立工作台。站内主 Agent 调配角色，审核 Agent 独立审核；Codex CLI 或用户配置的 API 作为模型连接。**业务运行不依赖当前 Codex 开发对话**，与 [Backlot](../video-workbench/README.md) 分别维护运行状态。

线路归属：**仅 US**。本应用不承载 SEA 任何结论，SEA 不得引用本应用的运行数据。

## 什么放哪

| 路径 | 放什么 | 不放什么 |
| --- | --- | --- |
| `studio/` | 应用代码。领域层（`models`/`store`/`pipeline`/`ledger`）与 Agent 层（`agents/`）、工具层（`tools/`）、服务层（`server.py`）分开 | 真实运行数据 |
| `studio/web/` | 原生 HTML/JS/CSS 前端，不引前端框架 | 构建产物 |
| `<app>/scripts/` | 本应用的开发工具。目前只有 `seed_preview.py`（造假数据预演界面）。**注意与仓库根的 `scripts/` 是两个目录**，那个放共用业务脚本 | 业务脚本 |
| `tests/` | 离线测试。**默认全部不联网**，用假模型覆盖循环与状态机 | 需要真实 API 的用例 |
| `README.md` | 定位、启动、运行契约、恢复命令与两套工作台的关系 | 逐次运行记录 |
| `requirements-local.txt` | 本机额外依赖及版本 | 全量生产依赖 |

真实运行数据在仓库外：`J:/E-Business-Video-Data/us-video-studio/`。
素材（图片与视频）属于运行数据，同样不进 Git。
代码与非敏感配置示例进 Git，实际连接配置、凭据与运行数据不进 Git。这与 `apps/video-workbench` 的既有做法一致。

## 命名

- 阶段名用 `snake_case`：`intake` / `direction` / `material_plan` / `writing`。
- 角色名用 `snake_case`：`orchestrator` / `director` / `material` / `writer` / `reviewer`。
- 运行目录 `runs/<run-id>/`，`run-id` 用 `[a-z0-9][a-z0-9-]*`。
- 素材放在 `runs/<run-id>/materials/`，**文件名保持磁盘上的原样**——产物里的
  `file` 字段就是靠这个文件名找到它的。中文名、空格、大小写都允许；
  路径分隔符不允许，见红线 7。
- 产物文件名由阶段决定：`<stage>-v<n>.md`，版本号只增不减。
- 测试文件 Python 用 `tests/test_<模块>.py`，前端渲染器用 `tests/js/test_<模块>.mjs`。

## 什么时候清理

- `runs/` 下的运行数据**只标记不删除**。过期靠状态与 `history/` 表达，不靠删文件。
- 产物不可变。修订产生新版本，旧版本留在原地。
- 本目录内不留临时文件；需要暂存写到系统临时目录。

## 命令

```powershell
# 离线测试（不联网、不花钱）。两条都要跑，前端渲染器在 Node 侧单独测。
& 'J:/E-Business-Video-Data/us-video-studio/runtime-venv/Scripts/python.exe' -m pytest apps/us-video-studio/tests -v
node --test apps/us-video-studio/tests/js/*.mjs

# 本地服务（先从仓库根切换到应用目录）
Set-Location apps/us-video-studio
& 'J:/E-Business-Video-Data/us-video-studio/runtime-venv/Scripts/python.exe' -m studio.server --port 4760
```

`studio/web/*.js` 是**字符串渲染器**，不是 DOM 构建器——所以它的转义能在 Node 里直接测，
不需要浏览器、不需要 jsdom。改 `blocks.js` / `md.js` 必须跑上面第二条。

## 编码

**仓库根**的 `scripts/quality-gate.ps1` 强制：**人类可读文件（`.md`/`.csv`）用 UTF-8 带
BOM**，**代码与配置（`.py`/`.json`/`.yaml`）用 UTF-8 不带 BOM**。加 BOM 是为了 Windows 下
中文不乱码。新建或改写 Markdown 后跑一次**仓库根**的 `scripts/validate_text_encoding.py`
确认。（本目录下也有一个 `scripts/`，放的是本应用的开发工具，不是这两个脚本。）

## 本应用红线

1. **生成仅由用户在制作界面明确提交。** 图片走 Codex CLI，视频走百炼单首帧链路；Agent 可准备方案但不得批准图片、授权费用或提交生成。准确输入须先完成制作检查；视频只接收本站按图片字节指纹获批的参考图。真实生成的验收费用另行确认，离线测试不联网、不花钱。
2. **审批只能来自用户在界面的点击。** 任何 Agent 工具都不能写入 `approval`。
   审核 Agent 可记录审核意见，但不能代替用户批准。
3. **两套工作台分别维护，互不写入运行状态。** Codex 驱动的 `apps/video-workbench`
   保留原记录，独立 Studio 需独立验收；显式迁移只读原件，不自动迁移历史批准。
4. **不把技术成功当内容成立。** 渲染成功、字段非空、哈希校验通过都不等于内容可用。
5. **费用台账记全。** 调用先记 intent，再记 settlement；未知用量或金额不得补零。
   待核实用量阻止继续调用，必须凭提供方证据恢复；预算是软阈值，金额只是估算。
6. **改规范先改本文档，再改代码。**
7. **素材只往 `runs/<id>/materials/` 落，且只能是图片与视频。** 上传不直接创建阶段
   产物或批准；素材字节与观察摘要变化必须使相关依赖失效。落地前检查扩展名白名单、
   单文件大小上限、解析后路径位于素材目录内；保留合法文件名，拒绝同名覆盖。
8. **审批绑定用户实际阅读的版本。** `expected_version` 与 `expected_artifact_sha256`
   必须对应展示内容，不得静默换成最新版；`selected_option_id` 是正式选择，推荐不是选择。
   `artifact-v2` 覆盖正文、结构、证据、限制及依赖；`body-v1` 只回放，不继承旧批准继续制作。
9. **同一数据根只运行一个写入进程。** 现有锁不代表多服务写入支持；本地输入导入与阶段
   恢复前停止对应服务。使用 README 中的显式恢复入口（CLI或站内运行控制），不删除历史或清零台账解锁。
10. **角色上下文保持隔离。** 编剧与审核读取必要事实、真实所选方向及素材证据；审核不得
    读取生产者完整对话或自我背书。文件存在不代表已观察，导入观察不代表模型亲自看过。
11. **输入不携带隐含权限。** ContentInput 仅从明确允许的本地根导入空 intake，拒绝覆盖；
    不增加任意文件 HTTP 读取入口，不把包内授权字段转成图片、视频、费用或发布授权。

## 运行与上下文约束

- 每轮冻结角色连接、价格与业务输入；保存配置只影响后续任务。新 API Key 仅保存到 Git 外的 Windows 用户 DPAPI 加密文件，不进入响应、日志、聊天、localStorage 或 Agent 上下文。开发助手不自动迁移环境凭据或读取 Codex 登录密钥。
- 任务复用原 run 事件、产物、台账，不另起业务任务数据库。浏览器断连不取消任务；重启中断不重放请求，未知用量先恢复。图片/视频也使用同一 Ledger，不能用文字 token 核账清除媒体未知用量；人民币与美元分列。
- Skills 读取 `.agents/skills/`，学习库复用 `tracks/us/creative/learning/library.json`。保存实际角色输入快照、Skill/hash、库版本和用途；快照不证明模型正确使用。案例只能按既有资格召回，不注入 holdout、pending、无用途资格或偏好锚点答案。
- 自然反馈绑定用户原话、run、准确产物版本/hash与范围；模型只提出候选，用户审阅差异后保存。应用前比较库 hash，保留前后版本；等待或局部认可不能转成全面通过。新链接仅登记，不自动抓取或宣称效果。
- 显式案例迁移只读原目录，单 writer 锁下隔离暂存、校验后发布；目标存在即拒绝覆盖。原脚本不执行，HTTP 只提供登记的精选文本，商家原素材与生成素材区分；主控可读历史，独立审核不读旧生产者档案。只建立 intake 与 pause，不继承旧审批/费用授权，不自动发模型请求。
- 离线测试使用临时 run、临时库与假模型，禁止污染真实案例库或借测试花费额度。真实生成、图片分析质量、视频声画理解和完整模型创作质量未验收，不能用 fixture 代替；既有一次 Codex 连通授权已使用。

## 配置与深入文档

`STUDIO_DATA_ROOT` 指定 Git 外数据根；`connections.json` 保存站内连接和角色路由，`config.json` 保存预算及循环限制。`STUDIO_BASE_URL`、`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` 仅用于旧直接客户端兼容路径，站内连接不自动导入环境凭据。服务默认 `127.0.0.1:4760`，无公网身份验证。

| 文档 | 内容 |
| --- | --- |
| [README](README.md) | 启动、用户流程、配置、HTTP 版本契约、恢复和导入 CLI |
| [独立运行说明](docs/independent-runtime.md) | 模块/存储关系、接口入口、只读冒烟、更新与排障 |
| [学习迁移](docs/learning-migration.md) | 共用 Skills/库、上下文隔离、反馈入库与存储 |
| [创意包契约](docs/creative-package-contract.md) | 视频/图文 manifest 和本地编译边界 |
| [Codex 图片](docs/codex-image-capability.md) / [Wan 视频](docs/wan-video-provider.md) | 提供方协议、能力证据、计费与结果限制 |
| [实现与验收状态](docs/product-improvements.md) | 已实现范围、案例接档、历史验证证据及剩余缺口 |
| [方案核验](../../tracks/us/docs/studio-plan-review-2026-09-17.md) | 2026-09-17 独立应用审查与修复证据 |
