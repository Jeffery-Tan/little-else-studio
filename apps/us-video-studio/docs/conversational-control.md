# 对话控制模型（F5）：决定与执行授权分离

设计日期：2026-09-29。基线 `main@4ceb3ab` 加 F1–F4/F6 修复。
上游依据：`Studio_Storyboard_Review_2026-09-29.md` 的 F5，以及本文件确立的原则：

> **唯一可解析的无费用、可逆 Creative Decision 可以由用户自然语言直接生效；有歧义就问；
> 收费/外部副作用只推进到准确的一次性确认，不由自然语言直接提交。**

本文件先回答实施顺序里要求回答的八个问题，再给事件模型与验收映射。

---

## 一、三条路径如何统一

现状是三条各写一次的路径：

| 路径 | 入口 | 现在做什么 |
| --- | --- | --- |
| 按钮 | `POST /nonce` → `POST /decision` | `pipeline.record_approval(nonce=…)` 后 `continuation_job` |
| 对话 | `POST /jobs` → Orchestrator 循环 | 模型说「请用户去界面决定」，没有写决定的路径 |
| 接续 | `POST /decision-continuation`、`/resume-steer` | 按已保存的批准派生固定 request_id 的任务 |

统一后的形状：

```text
对话（propose_user_action → 服务端核验 → commit_user_action）
按钮（/nonce + /decision         → 服务端核验 → commit_user_action）
                                        │
                                        ▼
                          pipeline.record_approval(...)   ← 唯一写批准的地方
                                        │
                                        ▼
                          decision_service.continuation(...) ← 唯一派生接续的地方
```

- **写入点只有一个**：`Pipeline.record_approval`。对话不新增第二条写批准的路径，只是让
  Orchestrator 有机会请求它。按钮路径的 nonce 保留，作为「这个输入确实来自浏览器」的凭据。
- **接续派生只有一个**：`decision_service.continuation`，按钮和对话共用；`/decision-continuation`
  与 `resume-steer` 继续按原 request_id 幂等查回原任务。
- 两条路径的差别只剩 `source`（`ui` / `conversation`），记入事件，不改语义。

---

## 二、Action Candidate 放在哪里

新增 `studio/decision.py::CANDS`：把「当前状态下用户可以做哪些**决定类**动作」变成一个
**从状态纯函数式推导出来的清单**。每个候选带 `id`、`class`、`target`、`label`、`options`。

候选不是模型自由生成的。模型只能**指向**其中一个 `id`：

```python
# 合法的候选（当前方向待选、选项 A/B/C 均可采用）
{'id': 'select_direction', 'class': 'creative',
 'target': {'stage': 'direction', 'version': 7, 'sha256': '…', 'option_id': 'B'}}
```

关键约束：`option_id` **只来自用户原话的指认**。模型不得用 `recommended` 顶替用户的选择——
候选清单里每个选项都列出来，模型必须选一个明确的，选不出来就 `ask_user`。

## 三、Trusted validator 放在哪里

`studio/decision.py::resolve(candidates, proposed, *, source)`：

- 纯函数，输入是候选清单和模型给的动作，没有 I/O，可单测。
- 返回 `{status: 'commit'|'clarify'|'confirmation'|'reject', action, reason, alternatives}`。
- **不碰** `pipeline`、不碰 `store`。写发生在 `commit_user_action`，并在 `store.run_lock`
  下**重新推导一次候选清单**，用当时的真实状态复核——防止验证与写之间状态漂移。

这个划分是有意的：验证是纯的，所以可以穷举测试；写只有一处，所以不可能绕过。

## 四、Creative Decision 与 Execution Authorization 如何区分

**由动作本身决定，不由文本决定。** `class` 是候选的属性：

- `creative`：无外部副作用、可逆、可通过新版本再改。允许对话直接 commit。
  当前包括：选择方向、采用阶段版本、退回阶段版本、简报修订。
- `execution`：会产生提供方请求或费用。对话只能走到 `confirmation`，**永不直接提交**。

`Studio` 的制作生成（`production_material_approval` / `prepare_generation`）**已经**是
Execution Authorization：它有自己的 nonce、准确的 draft_id + draft_sha256、逐项费用。
本次不重做它，只把「准备这次生成」提前到对话：自然语言说「按这六镜生成吧」→ 准备集合 →
对话里出现一张确认卡 → 用户点确认 → 走**现有**的生成路由。

即：Execution Authorization 的存储和提交路径保持不变，改变的是**它从哪里被发起**。

## 五、pending decision 如何表示

不新增存储。两类 pending 都已经从状态推导出来：

1. **决定类**：`timeline._pending_action(state, …)` 已经产出那唯一一张决定卡的完整结构
   （`kind` / `stage` / `version` / `artifact_sha256` / `options` / `amend`）。
   `decision_service` 直接读它，不另存一份。
2. **执行授权类**：`Production.workspace` 已经产出每个 draft 的 `can_generate`、
   `reference_approved`、`estimate`，以及整组可提交集合。

新增的只有**事件**，用于追溯来源，不用于推导状态：

```text
user_action_resolved   {action, target, source, status, request_id?}
user_action_rejected   {action, target, source, reason}
```

`decision.py` 不保存待决队列，因此不存在「队列与状态不一致」的可能；服务重启后候选清单
自然重新推导，不需要恢复。

## 六、短回复只在唯一上下文成立

这是最容易做错的地方，规则如下（纯函数，可穷举）：

1. 候选清单为空 → `clarify`，并说明当前没有等你决定的事。
2. 候选里 `creative` 且 target 唯一的动作**恰好一个** → 允许 commit。
   例：「行」对「用 B 继续吗？」= `approve_stage(direction-v7)`。
3. 同一 target 上有**多个互斥动作**（采用 / 退回）→ **永远 `clarify`**，因为「行」分辨不了
   「采用」和「退回并修改」。
4. 存在多个 target（方向待选 + 脚本待决 + 一次生成待确认）→ `clarify`，
   并在问题里列出实际的两三项。
5. 「采用 B」这类**指定了选项**的消息走 `select_direction`，它可以在一组选项里唯一解析，
   不受规则 3 限制，因为用户指名了对象。
6. 模型给的动作不在候选里（指向旧版本、指向不存在的选项）→ `reject`，把真实原因交回模型，
   由它向用户说明，不让用户看内部状态。

规则 3 是新增的、明确的取舍：宁可多问一句，也不把「行」猜成「退回」。

## 七、会修改的模块

| 模块 | 改动 |
| --- | --- |
| `studio/decision.py` | **新增**：候选推导 + 纯验证器 + 接续派生 |
| `studio/tools/registry.py` | **新增** `propose_user_action` 工具（仅 orchestrator） |
| `studio/agents/orchestrator.py` | 执行该工具；系统提示改为「你可以请服务端落实用户决定」 |
| `studio/models.py` | `Approval` 增加 `source`、`continuation_note` |
| `studio/pipeline.py` | `record_approval` 接受 `source`/`continuation_note`；采用时允许「决定 + 后续指示」并存 |
| `studio/http_approvals.py` | `/decision` 走 `decision_service`；按钮与对话同一写入与接续 |
| `studio/server.py` | `continuation_text` 读 `continuation_note`，按钮与对话共用 |
| `studio/timeline.py` | 决定卡 `kind` 增加 `class`，页面据此区分「直接生效」与「需要确认」 |
| `studio/web/app.js`、`journey.js` | 对话区出现待确认的执行授权卡；决定卡文案说明对话也能做同样的决定 |
| `studio/production.py` | 只读投影增加「这次可提交的生成集合」，供对话准备确认卡 |
| `CLAUDE.md`、`AGENTS.md`、`README.md` | 红线 2 重新表述；新增红线描述两类动作 |

## 八、保持不变的安全性质

- 产物不可变、`artifact-v2` 指纹、sha256 绑定、staleness、source fingerprint。
- `nonce` 机制不变：仍然只发给浏览器，仍然单次使用，仍然只落 `sha256`；对话路径**不使用也不签发**
  nonce，它走的是服务端对自己派生的候选做核验，不是伪造点击。
- 幂等：`request_id` 语义不变；同 ID 同负载查回原任务，同 ID 不同负载 409。
- 防重复提交：`prepare_generation` 对未结束 attempt 的拦截不变。
- 待核实提供方调用只查原任务，不自动重试。
- 未知用量照旧记录、不补零、不阻断。
- 上下文隔离：`propose_user_action` 只对 orchestrator 开放，专业角色拿不到。

---

## 九、事件模型变化

新增（都不参与状态推导，只做追溯）：

```text
user_action_resolved  run_id, action_id, class, target, source, status, request_id?, at
user_action_rejected  run_id, action_id, class, target, source, reason, at
```

`Approval` 增加两个字段，旧记录缺省即 `source='ui'`、`continuation_note=''`：
`source`、`continuation_note`。`source_snapshot` 已排除 `continue_after`；本次也排除
`continuation_note` 与 `source`，避免一次对话决定改变制作基线指纹。

---

## 十、A–G 验收如何测

| 场景 | 测法 |
| --- | --- |
| A 采用 B 并继续 | 对话 → `propose_user_action(select_direction)` → 批准记录 `source='conversation'`、`selected_option_id='B'` → 接续任务存在 → 无媒体调用 |
| B 采用 B + 开头更生活化 | 同一提交 + `continuation_note` 进入接续任务文本；被采用版本字节与指纹不变 |
| C 唯一询问下回「行」 | 候选唯一 → commit |
| D 多个 pending 下回「行」 | 候选多于一个 → `clarify`，不产生批准记录 |
| E 生成六镜 | 对话准备 → `confirmation` → 不提交；确认路由提交恰好一次；重复 HTTP/reload 不重复 |
| F 确认卡出现后改第二镜 | 快照变化 → 原授权失效，提交被拒 |
| G 第二镜重做 | 唯一镜头修订 → 不取消其它已完成创作 |

---

## 十一、禁止事项自查

- 不写关键词白名单：唯一的文本判断在 `resolve` 的「唯一候选」规则里，且只用于**放行**，
  不用于识别语义。
- 不让模型调用 `approve()`：模型只有 `propose_user_action`，写入在服务端。
- 不继承授权：采用脚本不产生生成授权；生成授权只来自 `Studio` 现有的一次性确认。
- 不把所有自然语言变成弹窗：`creative` 类直接生效。
- 不把旧 gate UI 原样搬进聊天区：决定卡保留为快捷方式，对话走同一 validator。
- 不退化成事事确认：安全来自准确绑定与动作风险等级。
