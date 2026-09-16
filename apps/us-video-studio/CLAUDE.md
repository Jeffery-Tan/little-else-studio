# us-video-studio 目录约定

本文件只约束本应用。跨项目规则见仓库根 [AGENTS.md](../../AGENTS.md) 与
[全局规则](#)，线路事实源见 [tracks/us](../../tracks/us/README.md)。

## 定位

US 视频前期策划的本地工作台。工作台内跟主 Agent 对话，主 Agent 调配角色 Agent
干活，审核 Agent 独立审核。**不依赖 Codex**，与
[apps/video-workbench](../video-workbench/README.md) 是两套东西，互不写入。

线路归属：**仅 US**。本应用不承载 SEA 任何结论，SEA 不得引用本应用的运行数据。

## 什么放哪

| 路径 | 放什么 | 不放什么 |
| --- | --- | --- |
| `studio/` | 应用代码。领域层（`models`/`store`/`pipeline`/`ledger`）与 Agent 层（`agents/`）、工具层（`tools/`）、服务层（`server.py`）分开 | 真实运行数据 |
| `studio/web/` | 原生 HTML/JS/CSS 前端，不引前端框架 | 构建产物 |
| `<app>/scripts/` | 本应用的开发工具。目前只有 `seed_preview.py`（造假数据预演界面）。**注意与仓库根的 `scripts/` 是两个目录**，那个放共用业务脚本 | 业务脚本 |
| `tests/` | 离线测试。**默认全部不联网**，用假模型覆盖循环与状态机 | 需要真实 API 的用例 |
| `README.md` | 定位、启动、边界、与旧工作台的关系 | 逐次运行记录 |
| `requirements-local.txt` | 本机额外依赖及版本 | 全量生产依赖 |

真实运行数据在仓库外：`J:/E-Business-Video-Data/us-video-studio/`。
素材（图片与视频）属于运行数据，同样不进 Git。
代码与配置进 Git，运行数据不进 Git。这与 `apps/video-workbench` 的既有做法一致。

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

# 本地服务
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

1. **不接任何付费生成 API。** v1 没有图片/视频生成工具，不是靠开关关掉。
2. **审批只能来自用户在界面的点击。** 任何 Agent 工具都不能写入 `approval`。
   审核 Agent 更不能写通过结论。
3. **不写回旧工作台。** 旧工作台的数据目录只读对照。Jeffery 已**不再使用**旧工作台
   （2026-09-17 明确：「只是我不想再用它」，不是它坏了）。因此旧工作台既不是安全网，
   也不是本应用的验收参照物——本应用要独立成立，功能缺口在这里补，不靠回退。
4. **不把技术成功当内容成立。** 渲染成功、字段非空、哈希校验通过都不等于内容可用。
5. **费用台账记全。** token 数是事实，美元数只标估算并注明价目来源与日期。
6. **改规范先改本文档，再改代码。**
7. **素材只往 `runs/<id>/materials/` 落，且只能是图片与视频。** 上传是**第三个写入口**
   （前两个是发消息、决定门禁，见红线 2），但它**不改状态**：不写事件日志、不产生
   artifact、不动任何产物。落地前必须做三件事——扩展名白名单、单文件大小上限、
   解析后的绝对路径确认在该目录之内（挡 `../../`）。文件名净化后保持原样入库，
   不做重命名，因为产物里的 `file` 字段按名引用它。
