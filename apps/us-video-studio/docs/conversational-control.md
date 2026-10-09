# 对话控制模型（F5）：决定与执行授权分离

设计日期：2026-09-29；工程核验更新：2026-10-06。历史工程审查基线为 `main@6176292`；本次收口包含 `6d8287c` 之后的参考与整体架构修复，当前实现及验收见[状态入口](product-improvements.md)。
上游依据：`Studio_Storyboard_Review_2026-09-29.md` 的 F5，以及本文件确立的原则：

> **唯一可解析的无费用、可逆 Creative Decision 可以由用户自然语言直接生效；有歧义就问；
> 收费/外部副作用只推进到准确的一次性确认，不由自然语言直接提交。**

本文件保留设计的八个问题，并按后续修复维护消息来源、展示绑定、接续回执、角色收尾和验收边界。

---

## 一、三条路径如何统一

三条入口共用决定与接续服务：

| 路径 | 入口 | 现在做什么 |
| --- | --- | --- |
| 按钮（对话及作品阅读页共用） | `POST /nonce` → `POST /decision` | `pipeline.record_approval(nonce=…)` 后 `continuation_job` |
| 对话 | `POST /jobs?enqueue=true` → 原 Job 队列与 Orchestrator 循环 | 保存真实消息凭据；模型提候选，服务端复核后写决定 |
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

`studio/decision.py::candidates` 把「当前状态下用户可以做哪些**决定类**动作」变成一个
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

`studio/decision.py::resolve(candidates, proposed, *, source, receipt)`：

- 纯函数，输入是候选清单、模型给的动作和服务端保存的用户消息凭据，没有 I/O，可单测。
- 返回 `{status: 'commit'|'clarify'|'confirmation'|'reject', action, reason, alternatives}`。
- **不碰** `pipeline`、不碰 `store`。写发生在 `commit_user_action`，并在 `store.run_lock`
  下**重新推导一次候选清单**，用当时的真实状态复核——防止验证与写之间状态漂移。

验证与写入分开便于测试；这只能证明被测工程边界，不能穷举自然语言含义或证明模型理解正确。

`GET /runs/{run_id}/timeline` 同时返回 `display_context_id`，在原事件日志保存这次向浏览器提供的待决卡、选项、版本与指纹、问题及相邻助手消息。同一展示内容可复用标识；该凭据表示服务端提供过内容，不证明用户实际阅读。浏览器发新消息时把标识随 `POST /runs/{run_id}/jobs` 发送，网络重试保留原标识。

入站先保存 `user_message_received`（原话、`message_id`、`request_id`、展示内容），任务接受后才写 `user_message_accepted` 与用户对话。被拒的请求不会伪装成已接受的用户轮次。没有展示凭据的旧客户端仍可讨论，但不能通过模型候选直接采用作品。模型不能提供或替换用户凭据；服务端从本轮入站取得它，在写入时重新核对当前待决项与当时展示内容。一次消息只能消费一次决定。

由程序派生的接续文字记录为 `internal_planning_instruction`，不新增用户消息或用户决定权限。

## 四、Creative Decision 与 Execution Authorization 如何区分

**由动作本身决定，不由文本决定。** `class` 是候选的属性：

- `creative`：无外部副作用、可逆、可通过新版本再改。允许对话直接 commit。
  当前包括：选择方向、采用阶段版本、退回阶段版本、简报修订。
- `execution`：会产生提供方请求或费用。对话只能走到 `confirmation`，**永不直接提交**。

`Studio` 的制作生成（`production_material_approval` / `prepare_generation`）**已经**是
Execution Authorization：它有自己的 nonce、准确的 draft_id + draft_sha256、逐项费用。
确认格式保留；提交前复核准确集合的每一项仍是当前修订。准备入口也可来自对话：自然语言说「按这六镜生成吧」→ 准备集合 →
对话里出现一张确认卡 → 用户点确认 → 走**现有**的生成路由。

准备不是授权；授权不是已提交。worker 在准备和紧邻提供方调用处重验整组，旧集合被拒；批量中途改了后续镜头，就停止尚未提交的镜头。

## 五、pending decision 如何表示

不新增数据库。两类 pending 仍从原项目事实推导出来：

1. **决定类**：`timeline._pending_action(state, …)` 已经产出那唯一一张决定卡的完整结构
   （`kind` / `stage` / `version` / `artifact_sha256` / `options` / `amend`）。
   `decision_service` 直接读它，不另存一份。
2. **执行授权类**：`Production.workspace` 已经产出每个 draft 的 `can_generate`、
   `reference_approved`、`estimate`，以及整组可提交集合。

展示与消息凭据也保存在原事件日志；核验读取它们，不依赖模型声称的对话上下文。决定过程记录：

```text
user_action_resolved   {action, target, source, status, request_id?}
user_action_rejected   {action, target, source, reason}
```

决定保存与后续执行分别记录。`commit` 只返回接续 `pending` 或 `paused`；任务结束后，`JobManager` 才按决定的固定 `request_id` 创建接续任务，记录 `decision_followup_consumed` 和真实任务 ID。按钮与对话共用同一派生入口，重复请求查回同一任务。启动服务不自动重放接续；创建失败明确留待恢复，不说成已经开始。中途停止或工作修改会抑制旧接续，进度询问不会。

「采用 B，暂时别继续」可以保存采用且暂停接续，两者独立。简报修订使用原有 `apply_brief_amendment` 路径，核对所展示的修订提案与简报指纹；不先批准已失效的旧方向。

## 六、短回复只在唯一上下文成立

1. 候选清单为空，或用户指向多个对象 → 澄清，不猜决定。
2. 「行」「好」「可以」等受控短答只允许回应已保存、仍紧邻且明确指定对象的单一是非问句。例如「是否采用方向 B？」「按 B 继续吗？」；普通 A/B/C 待选卡不能替模型补出 B。还有其它未决问题时也不放行。
3. 「采用 B」仍由模型提出动作；服务端核对原话中的选项、展示过的准确版本、当前候选与一次消息消费。用户写 C、模型报 B，或模型指向未展示、过期的版本，均拒绝。
4. 对否定、假设、引用、询问、修改请求和多选表达设置保守拒绝规则，防止已知误采用反例。它们不是通用语言理解器；较长自然语言仍依赖模型解释，离线预设模型不证明真实模型理解质量。
5. 用户明确暂停优先于模型建议的 `continue_after`；保存决定不隐含允许立即接续。

## 七、相关模块与责任

| 模块 | 改动 |
| --- | --- |
| `studio/decision.py` | **新增**：候选推导 + 纯验证器 + 接续派生 |
| `studio/conversation_input.py` | 服务端展示快照、真实用户消息收据与保守校验 |
| `studio/tools/registry.py` | **新增** `propose_user_action` 工具（仅 orchestrator） |
| `studio/agents/orchestrator.py` | 执行该工具；系统提示改为「你可以请服务端落实用户决定」 |
| `studio/models.py` | `Approval` 增加 `source`、`continuation_note` |
| `studio/pipeline.py` | `record_approval` 接受 `source`/`continuation_note`；采用时允许「决定 + 后续指示」并存 |
| `studio/http_approvals.py` | `/decision` 走 `decision_service`；按钮与对话同一写入与接续 |
| `studio/server.py` | `continuation_text` 读 `continuation_note`，按钮与对话共用 |
| `studio/jobs.py` | 实际接续任务与固定请求标识回执；内部派生指令不冒充用户 |
| `studio/timeline.py` | 决定卡 `kind` 增加 `class`，页面据此区分「直接生效」与「需要确认」 |
| `studio/web/app.js`、`journey.js` | 对话区出现待确认的执行授权卡；决定卡文案说明对话也能做同样的决定 |
| `studio/production.py` | 只读投影增加「这次可提交的生成集合」，供对话准备确认卡 |
| `CLAUDE.md`、`AGENTS.md`、`README.md` | 红线 2 重新表述；新增红线描述两类动作 |

## 八、保持不变的安全性质

- 产物不可变、`artifact-v2` 指纹、sha256 绑定、staleness、source fingerprint。
- 浏览器 nonce 仍单次使用、只落 `sha256`，不进入模型上下文。对话在核对真实消息与展示后由服务端内部签发并消费 nonce，以复用唯一的批准写入点；模型不能签发或提交它。
- 幂等：`request_id` 语义不变；同 ID 同负载查回原任务，同 ID 不同负载 409。
- 防重复提交：`prepare_generation` 对未结束 attempt 的拦截不变。
- 待核实提供方调用只查原任务，不自动重试。
- 未知用量照旧记录、不补零、不阻断。
- 上下文隔离：`propose_user_action` 只对 orchestrator 开放，专业角色拿不到。

---

## 九、事件模型变化

原事件日志中的相关记录：

```text
user_action_resolved  run_id, action_id, class, target, source, status, request_id?, at
user_action_rejected  run_id, action_id, class, target, source, reason, at
conversation_displayed  display_context_id, displayed, fingerprint
user_message_received / user_message_accepted / user_message_consumed
decision_followup_requested / decision_followup_consumed / decision_followup_blocked / decision_followup_suppressed
internal_planning_instruction
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
| C 明确邻接询问下回「行」 | 已保存、准确指明对象的单一是非问句 → commit；普通待选卡拒绝 |
| D 多个 pending 下回「行」 | 候选多于一个 → `clarify`，不产生批准记录 |
| E 生成六镜 | 对话准备 → `confirmation` → 不提交；确认路由提交恰好一次；重复 HTTP/reload 不重复 |
| F 确认卡出现后改第二镜 | 单镜/整组旧确认经真实 HTTP 返回 409；worker 准备与紧邻提供方提交边界再次核对，提供方调用为 0 |
| G 第二镜重做 | 唯一镜头修订 → 不取消其它已完成创作 |

---

## 十一、禁止事项自查

- 不把文本规则当作完整语言理解证明：明确区分受控短答、保守拒绝规则与模型解释；真实模型和真实成片须另行验收。
- 不让模型调用 `approve()`：模型只有 `propose_user_action`，写入在服务端。
- 不继承授权：采用脚本不产生生成授权；生成授权只来自 `Studio` 现有的一次性确认。
- 不把所有自然语言变成弹窗：`creative` 类直接生效。
- 不把旧 gate UI 原样搬进聊天区：决定卡保留为快捷方式，对话走同一 validator。
- 不退化成事事确认：安全来自准确绑定与动作风险等级。

## 十二、会话、角色任务与完整输入（2026-10-03）

本次采用 DeepSeek Harness 的持久收件、任务所有权与同源投影机制，在现有 Python 运行层实现；没有安装或切换到其 TypeScript runtime。主任务复用 Job ID，专业角色复用 Pipeline attempt ID，`AgentRuntime` 在原事件日志记录父子关系、原始用户消息、冻结上下文引用、工具权限和准确产物回执。`GET /api/runs/{run_id}/agent-runtime` 只读取公开元数据，不执行恢复、不返回模型输入正文。专业角色没有提交实际产物时不能标 completed。

- 中途消息和空闲输入共用不可变 receipt。多个调整逐条建立后继任务，不能合成为另一条用户决定；未执行的后续消息不能进入当前任务的模型历史、项目聊天摘要或历史工具读取。
- 进度问句直接读取现有制作状态并保存关联答复，才标 answered。明确停止和等待不接续；停止向所属角色传播，并撤销本轮待处理旧消息。停止原任务也会追踪同一接续链上的活动任务。
- 重启把未收尾任务记为 interrupted，保留尚未处理的消息并提供显式恢复入口，不重发模型或供应商请求。状态问答不会结清其他待处理输入。实际已经保存的产物不因取消而删除。
- 首次路由必须声明交付目标，并检查本轮明确的只读、格式、提示词和局部修改边界。第一份 local/rework 计划也要与原话核对，已绑定的具体目标不能降为 follow_up。误将真正修改标为 feedback 的只读路由允许纠正，不能覆盖真实停止。
- 决定接续和角色输入保留完整原话，即使模型遗漏或改写 `continuation_note`。内容条件通过 `propose_task_interpretation` 绑定逐字来源、适用角色/字段和 turn/project 生命周期；项目持续条件须另有明确持续范围原话。新增、替换、撤销、疑问、假设和转述分别记录，冲突仅阻止受影响的工作。旧场景/街拍条件继续兼容；语义解释由模型提出，服务端核验来源、作用域和权限，不声称完整理解任意自然语言。
- 草稿按项目保存文本、作品目标、直接修改模式、提示词模式及澄清身份。已上传附件保留文件引用和用途；未上传文件刷新后保留名称/用途并明确要求重新选择文件。接受结果未知时冻结原 request_id 与负载，查询原回执；只有确定拒绝才能恢复为可编辑草稿。
- queued/cancelling 状态中的不可用动作禁用或给出明确原因；直接修改不能在忙碌时变为普通 steer。版本过期拒绝后保留原目标，不自动改绑新版。

这层机制不新增任务数据库、审批写入点、镜头覆盖率或收费权限。原 Pipeline、production 与 Execution Authorization 仍分别负责产物、制作进度和一次性执行授权。离线回归覆盖程序边界；真实模型理解和实际成片质量须另行验收。

### 独立角色会话与剩余改造收尾（2026-10-03）

`AgentSessions` 为角色保存稳定 session_id，执行轮仍使用原 attempt ID。私有日志保存模型可见输入、完整工具结果、收件和上下文投影，不保存私有推理或认证对象；公开状态只提供身份、状态与产物回执。读取会话不启动模型，不重放工具。没有保存实际产物的中断/失败/等待会话由用户明确继续；新 attempt 重建当前领域状态，旧调用未知时核对原状态。已保存产物的会话只提供准确回执，新要求重新绑定稿件，不恢复旧任务。已停止的会话不能恢复，同一主任务不能重新派出该项工作；已保存产物保留。

主制片可用 `start_role` 非阻塞派出独立任务，`wait_role` 核对实际回执；每个会话只有一个执行器，最多 4 个角色工作线程、8 个正在执行或等待的角色任务、3 层委派深度。模型调用在同一数据根最多并行 4 个，全部结算前操作系统租约保持有效；对账仍互斥。仅白名单读取工具可并行，领域写入继续用原 run 锁并在提交时复核。依赖尚未采用时不能写下游，主任务不能在所属角色仍执行时宣称结束。

团队记录提供「补充要求」「停止此项」「继续此项」，绑定准确角色、项目与任务。定向补充不取消其他角色，不生成全局后继对话；原话进入该角色下一步骤，未处理前拒绝提交旧结果，多个已处理条件合并保留。停止仅影响所选角色，主制片不能自动再派同一项。角色已经结束或项目已切换时保留草稿与目标并提示，不降为普通对话。HTTP 恢复沿用原 request_id/回执幂等协议，不继承旧批准或收费授权。

长对话采用有来源的历史索引和准确原文检索；本轮原话、适用条件、当前领域状态与完整工具配对保留。压缩不拆开调用/结果，不把摘要当成采用或执行权限；不可再缩减的上下文超过预算时如实记录。角色恢复重新读取当前状态和条件，不以旧对话覆盖当前事实。

脚本的 shot_id、图文的 page_id 跨版本保持身份，序号仅表示当前顺序。已生成结果按逐镜画面、声音、时长、共享设计、商品/证据、模型参数及参考字节核对复用条件；用户明确复用时复制成新材料身份，未发生新的提供方生成。旧图审不继承，local_reuse 输入不能再次提交收费生成；未知结果只查询原任务。口播变化可保留未变画面，视频声音或时长变化须失效；场景变化只影响引用该场景的镜头。


### 再次复核后的执行边界修复（2026-10-03）

中途状态问句按整条消息逐句检查，包括自然进度表达、卡住的描述与否定停止；不会取消原任务或启动后继。明确停止按停止结算，不转成修改。明确要求继续运行但同时增加修改时记录 deferred，当前任务继续，结束后按原话单独处理；不能只看一个“停”字。无法识别的修改仍沿原处理规则，不声称完整理解任意自然语言。

创作决定提交时重读这条真实消息的可信 task.pause_after；语义暂停会阻止自动接续，模型给出的 continue_after 不能覆盖。scope.paths 仅表示内容条件适用于哪些字段，不把完整写稿变成局部修改任务。局部任务在准备、计划、角色输入与派工检查中使用同一份准确路径过滤冲突，无关镜头的问题不会阻断当前字段。

异常、取消与关闭在所属角色真实结算后释放主任务和写入租约；空闲检查包含独立角色执行器。从预约提交开始即保存稳定 Future，避免线程已开始但 pool.submit 尚未返回的窗口被当成空闲。等待在 jobs/run 锁之外进行，热重载与新任务均遵守这份占用状态。

定向角色补充若在收件时遇到会话关闭，原话保持可追溯，记录 job_steer_delivery_failed；重复 POST 仍拒绝，只读回执明确 received=false / rejected=true。前端恢复对应冻结草稿并继续观察原任务，不把失败投递改成已执行。角色补充与图片、视频、制作输入、脚本反馈统一互斥切换，保留草稿文字和冻结请求；镜头标签只读取制作域准确身份对应的序号，历史输入不按保存顺序冒充当前镜头。

视频本地复用由 production.reuse_candidates 提供当前脚本身份和同镜当前首帧的 reuse_reference；界面提交同一负载。首帧缺失、任务未知、不同字节或别镜引用均拒绝，不自动发起新生成。新材料身份不能继承旧图审或费用授权。

角色压缩历史后会看到当前会话的只读历史索引，可通过 read_role_history 分页取回逻辑回复和工具结果。服务端绑定 session，模型不能指定另一个会话、项目或读取 model_request/私有推理。索引和正文有界，原结果保持完整；取回只供背景核对，不能重放旧工具或获取采用、图审、收费权限。

### 整体复核后的交付与输入边界（2026-10-04）

新交付任务开始时冻结阶段基线，需求、方向与整稿须交付本轮准确新版本；明确沿用按真实原话、stage/version/hash 绑定。讨论、查询和准确沿用不强制重写，同一原输入的恢复使用首次基线。原角色的已保存产物不能冒充新要求的交付。

角色保存产物与关闭补充入口共用 run lock。晚到消息先保存真实 receipt，再记录投递失败；已保存版本和未处理补充保持可见，拒绝投递不取消父任务。历史崩溃缺少会话回执时，只核验原 attempt 的完整产物并投影，不补造事件或消费历史 pending；仍在收尾的 worker 保持可停止。

「继续此项」提交服务端固定 resume_command 和 resume_action=continue，以 expected_task_id 绑定当前 session_id/task_id；自定义文字先由普通对话解释，停止、查询与讨论不能借恢复接口开工。新整稿不能恢复旧局部任务；新修订须核对本轮真实输入的计划或反馈对象，版本、来源与完整字段集合相容才可继续旧角色。

入站冻结当前待答 question_id。准确选项按真实 receipt 结算，自由回答由 record_question_answer 引用当前问题与实际原话核验；查询、停止、讨论与无关反馈保留待答状态。合法「只分析参考」回答可结算问题，但不产生图片、视频或费用授权。
