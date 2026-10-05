# 参考视频功能与最新架构接入审计

审计日期：2026-10-03。代码基线：`6d8287ca88e81b0a95b38043b095b6f4c9f930cf`。开始与复现结束时工作树均干净。范围：独立 US Studio 的本地参考视频、公开 TikTok 链接、参考拆解、提示词适配、参考创作、忙时输入接续及新画布展示。Backlot 历史视频恢复不在本轮范围。

修复前结论：需要修改。初次已复现 8 项问题，核心是既有参考能力没有完整接上新的输入事务、用户任务契约、忙时入站与画布投影。下载、声画观察、原片复用和指定参考上下文可以继续沿用；建议围绕当前共享机制修复接入。初次审计只形成记录，没有修改应用行为；随后两轮实施结果见后文。

原审计和两轮实施记录均保留各自日期语境，当前修复状态与全量验收统一见[实现与验收状态](product-improvements.md)。本机 Temp 截图、反例与日志未随 Git 提交；跨电脑复跑使用仓库正式回归，不依赖这些临时文件。

## 证据与验证边界

- 相关既有 Python 离线测试：78 passed，覆盖 `test_natural_reference.py`、`test_reference_video.py`、`test_user_task_constraints.py`、`test_conversation_runtime_control.py`。
- 相关既有 JS 离线测试：75 passed，覆盖参考、附件用途、草稿事务、画布与导航。测试通过不能排除以下漏测场景。
- 前后端审计分别完成后，主审计重新运行了全部反例脚本并复核代码。后端模型与观察回调均为 fake/mock，只证明调用边界、状态与输入传递，不证明真实模型必然产生相同误路由。
- `/timeline` 真实路由结构通过临时数据根和 TestClient 核对，0 次模型调用；所有反例使用 Git 外临时目录。
- 本机运行中的 `http://127.0.0.1:4760/` 进行了只读界面检查。现有「https带货视频」项目仍可从素材区打开参考原片与 5 段声画报告。没有发送对话、上传文件、重试分析、改变业务记录或提交媒体生成。
- 没有新增真实模型、下载提供方或媒体生成调用；未验证新一次真实 TikTok 获取与收费声画分析的成功率。

## 需要修改的问题

### R1 · P1 · 裸链接缺少用途时，错误工具调用仍可触发参考观察

位置：[orchestrator.py](../studio/agents/orchestrator.py#L1024)、[runtime.js](../studio/web/runtime.js#L314)。

触发：用户仅发送 TikTok URL，模型错误调用 `observe_reference(action=analyze, quote=URL)`，没有用途澄清或已绑定的任务解释。后端校验 quote 出现在用户原话、来源来自用户后，即允许调用观察回调。普通项目的前端 `jobWirePayload()` 默认附加 `reference_consent=true`。

复现输出：`observer_calls=[URL]`、`delivery.goal=analysis`、`delivery.status=complete`、`stop=done`。

影响：用途尚未明确的商品链接可能被当成表达参考，并产生一次用户没有明确要求用途的观察费用。这是程序边界缺口；不能据此认定正常真实模型一定误判，也不涉及图片或视频生成授权。

最小改动：观察前核对这条真实输入的明确用途与观察准入。只有 URL、且无有效附件用途或用途回答时，先进行一次澄清。不能把模型自报 action、任意原话片段或普通项目默认布尔值独立当作用途依据。

验收：裸链接不调用观察；明确商品用途只走商品观察；明确参考用途走参考观察；回答用途后只执行本次允许的观察。

### R2 · P2 · 参考观察覆盖已冻结的最终交付

位置：[orchestrator.py](../studio/agents/orchestrator.py#L1050)；已有交付一致性校验在同文件 `prepare_creative_context` 的 918–919 行。

触发：真实用户要求「按这条参考做完整方案」，已经保存 `task_interpretation=produce/writing`。模型随后误调用 `observe_reference(action=analyze)`，分支直接把 `_deliverable` 改成 `analysis` 并结束本轮。prompt 分支同样直接重写交付目标。

复现输出：冻结任务仍为 `writing`，实际 `writing_exists=false`，任务却返回 `analysis/complete` 和 `done`。

影响：分析报告会替代用户要求的完整方案，完成状态与原任务不一致。

最小改动：参考动作与终止行为遵守真实输入绑定的任务契约；最终完成检查以该契约为准。参考分析如果是完整方案的依赖，不应修改最终目标或直接结束；只分析与只提示词仍保持各自的停止边界。

验收：同一参考分别请求分析、提示词、完整方案，交付与停止位置严格对应；错误工具动作不能把完整方案标记完成。

### R3 · P2 · 忙时参考请求在下一轮丢失观察准入

位置：[http_contracts.py](../studio/http_contracts.py#L91)、[http_execution.py](../studio/http_execution.py#L180)、[server.py](../studio/server.py#L112)。

触发：运行中发送「拆解这条参考 URL」。`SteerIn` 不接收 `reference_consent`；入站 receipt 也没有保存它；后继 `prepare_input()` 没有向 `prepare_job()` 传递本条输入的观察准入。即使请求额外带 `reference_consent=true`，该字段也会被忽略。

复现输出：`successor_status=succeeded`、`observer_calls=[]`、工具结果包含「参考观察连接未就绪」，后继模型配置没有 `reference_analysis`。

影响：相同用户要求在空闲时与执行中发送有不同结果；新架构的持久入站与接续没有保留参考流程所需信息。

最小改动：忙闲输入、receipt、恢复共用同一输入 envelope，持久保存本条输入的用途与观察准入，并据此冻结下一轮连接。不能无条件继承旧任务授权，也不能把所有后继任务统一设为 true。

验收：空闲、忙时接续与重启后显式恢复得到一致的用途/准入；停止后不恢复旧执行；未授权的新消息不继承前一任务的观察权限。

### R4 · P2 · 两个附件入口互相覆盖

位置：[journey.js](../studio/web/journey.js#L720) 的项目入口与 753 行的首页入口；实际替换在 [conversation-input.js](../studio/web/conversation-input.js#L46)。

触发：先用「＋商品图片/视频」添加 `product.jpg`，再用「＋参考视频」添加 `ref.mp4`。

复现输出：项目附件只剩 `["ref.mp4"]`；首页也只剩 1 个附件 chip。原商品素材没有进入待发送附件集合。

影响：与「商品素材和参考视频可以分开添加」的交互承诺不符；用户按正常顺序提供商品依据和参考时，会丢掉前一批附件。

最小改动：两个入口追加附件，逐项保留用途、上传回执与草稿事务身份。避免将已有视频统一重标为最后一次入口的用途。

验收：商品图片/商品视频与参考视频可按任意顺序添加、移除、改用途，并一起发送；重复点击不清空已有附件，失败接续不重复上传已保存字节。

### R5 · P2 · 「请主制片适配」覆盖草稿并继承旧修改目标

位置：[journey.js](../studio/web/journey.js#L666)。

触发：先选择一个脚本字段，保留 `feedbackTarget` 与 `composerRequestKind=writing_revision`；输入未发送意见，再从参考报告点击「请主制片适配」。该入口直接赋值 `chat-input`，绕过统一目标切换与草稿保存机制。

复现输出：未发送文字被覆盖，但持久草稿仍保留旧文字。随后发送负载仍为 `request_kind=writing_revision`、`target_kind=writing`、`path=["segments",0,"line"]`。

影响：应交给主制片的参考适配请求会被送成旧脚本字段的直接修改；用户草稿显示与恢复内容也不一致。

最小改动：通过现有统一输入事务切换到参考适配，解除旧作品/角色/直接修改身份，保留用户已有文字，保存新草稿，并携带准确参考身份。

验收：有普通草稿、局部脚本反馈、角色目标时点击适配，均不丢文字、不继承旧修改身份、不自动发送；刷新后的草稿一致。

### R6 · P2 · 只分析项目的新画布取错报告来源

位置：[creative-board.js](../studio/web/creative-board.js#L97) 与 131 行；任务数据已由 [journey.js](../studio/web/journey.js#L198) 传入。

触发：已完成参考分析的项目进入默认画布。组件只读 `timeline.reference.report` / `timeline.analysis.report`，真实 `/timeline` API 没有这两个字段，组件忽略已传入的 `referenceTasks`。

复现输出：真实路由 `Has reference/analysis: false false`；提供完成任务与真实 API shape 时，`projectBoard().analysis=""`。

影响：报告已经保存，默认画布仍显示分析占位，容易被理解为功能消失或任务未完成。

最小改动：画布消费已传入的参考任务投影，并使用当前准确参考选择与状态；沿用原事实源。

验收：完成、未完成、失败、过期及多条参考选择均显示对应报告/状态；没有报告时才显示占位。

### R7 · P2 · 从素材区打开分析详情，内容进入隐藏面板

位置：[material-workspace.js](../studio/web/material-workspace.js#L27)。

触发：只分析项目处于默认 `canvasTab=board`，从素材任务卡点击「查看逐段拆解」。分支只调用 `renderReferenceAnalysis()`，没有切换到显示该报告的面板。

复现输出：`canvasTab=board`、`stageHidden=true`、`boardHidden=false`、`reportRendered=true`。

影响：用户已点击查看，界面仍停在画布，刚打开的报告不可见。

最小改动：设置准确参考选择后，通过统一工作区导航显示分析详情。

验收：从默认画布、其他作品详情与多参考项目打开拆解，均显示选中的报告；返回画布正确，不改变任务状态。

### R8 · P2 · 参考任务的失败状态没有完整投影到新流程

位置：[reference-video.js](../studio/web/reference-video.js#L12)、[reference_video.py](../studio/reference_video.py#L72)。

前端触发：传统参考任务已有 `execution_status=failed`、`failure_reason=TikTok download timed out`。普通创作项目任务卡忽略这两个字段，只显示链接与「继续 / 上传原视频」。Learning 专用视图已经使用这些状态，普通项目没有接入。

后端触发：参考观察现在运行在当前对话 Job 内，`task_state()` 却只映射 `job_kind=reference_video`。隔离事件用例中，`job_kind=message` 已失败，关联参考仍是 `status=started`，没有 `execution_status` 与 `failure_reason`。这说明只补前端标签还不能覆盖新的对话路径。

影响：失败、取消或中断的参考任务可能一直看似未处理完成；用户看不到原因，也无法判断应继续原任务、上传原片还是等待。

最小改动：将参考处理绑定到实际所属的对话/参考 Job 或运行任务，在原投影中提供一致终态和原因；任务卡展示该投影，再据状态给出显式接续。保留已保存原片与分析，不新增自动重试。

验收：传统入口与对话入口分别覆盖下载失败、分析失败、取消、中断、成功及已保存原片后的接续；终态、原因和下一动作一致。

## 建议修复顺序与范围

1. 先处理 R1–R3：用途/准入、交付契约、忙时接续。它们决定是否调用、要交付什么及本条消息如何继续，属于共享后端边界。
2. 同轮处理 R4–R5：附件追加和适配入口接入现有输入事务，保证用户文字、商品依据与准确参考不会丢失或误绑定。
3. 接着处理 R6–R8：画布和详情消费同一参考投影，统一失败状态与接续入口。

保留现有的单 Job 参考观察、每轮一次尝试、按源字节/分析指纹复用、指定参考范围、商品事实与表达参考隔离。导演、素材、编剧的 `LearningService.context()` 和角色任务输入仍携带所选参考及改编方法；无需为本轮缺陷新增独立分析 Agent 或新阶段。

测试应补在真实接缝上：真实 API shape 与真正画布组件组合、两个文件入口连续操作、已有局部反馈后进入适配、忙时入站后的参考观察、错误 action 与冻结任务契约不一致。现有画布组件测试人为提供 `timeline.reference`，集成测试又 stub 组件，因而没有覆盖 R6。

## Git 外复现材料

- 前端反例：`C:/Users/tanju/AppData/Local/Temp/reference-ui-audit-20261003.mjs`。
- 真实 timeline 路由结构：`C:/Users/tanju/AppData/Local/Temp/reference-timeline-shape-audit-20261003.py`。
- 后端反例：`C:/Users/tanju/AppData/Local/Temp/studio-reference-audit-20261003-9ded1c618bbb4b33856f56707807af30/repro.py`；初次输出同目录 `repro-output.jsonl`。

临时材料用于本轮证据追溯；正式实现时应将必要场景转为仓库现有测试结构中的回归覆盖。本记录不代表以上问题已修复。

## 实施记录（2026-10-04）

本节记录后续修复；上方为修复前审计证据，保留原判断和复现结果。

- R1–R3：任务解释先绑定真实入站消息，视频用途按逐项附件、准确参考目标或本条原话中的语义解释核验。空闲、忙时和恢复读取各自消息的 `reference_consent`、`attachments` 与 `reference_target`；不继承上一轮准入。参考/商品观察不改写冻结交付目标，分析只在最终交付为分析时结束该轮。
- R4–R5：两个附件入口追加，保留各自用途和已确认上传回执；参考适配走统一草稿事务，保留原文字，解除旧作品/角色/直接修改目标，持久保存准确参考身份，并在发送时再次核验版本。
- R6–R8：画布读取实际 `referenceTasks`，素材区打开分析时切换可见面板；参考处理绑定实际所属 Job，保留步骤错误、原片和有效分析。任务卡区分未完成观察和分析后的后续失败，只有显式操作才恢复或准备制片对话草稿。

浏览器验收使用 Git 外临时 Store、真实应用与合成图片/视频，端口 `4763`，模型执行禁用。已实际操作首页及项目两入口连续添加、完成报告的默认画布、素材抽屉到逐段详情、适配已有草稿及刷新恢复、失败任务的原片/原因/显式恢复入口；浏览器无警告或脚本错误。该验收不代表真实 TikTok 获取成功率、百炼识别质量或创意内容质量已验证。

最终检查：完整 Python 用例按全部 `test_*.py` 模块分为三个隔离进程执行，结果为 `490 + 683 + 763 = 1936 passed`、`2 skipped`；完整 JavaScript 检查 `node --test --test-reporter=dot apps/us-video-studio/tests/js/*.mjs` 通过（615 passed、2 skipped）。跳过项为原有需显式开启的 live 检查，未绕过检查或访问真实业务数据。原有 Starlette/AnyIO 弃用提示保留。严格协议反例、真实 HTTP 精确参考复用、保存候选后的中断恢复及 UI 草稿/导航回归均通过；最终差异与 UTF-8 编码检查通过。

完整 Python 首轮发现测试辅助函数对只读路由缺少 `deliverable` 时抛出 `KeyError`，已修复辅助函数并复测全部模块。JavaScript 首轮发现旧待核实 steer 被新默认字段改变身份，已修复为原冻结载荷复核并补回归。精确参考复用统一走原参考服务的 `reuse_analysis`，绑定当前实际 Job；没有观察连接时仍可读取准确缓存，不调用观察模型。候选已保存但报告事件尚未写入的中断，也会在显式复用时补齐投影。

隔离截图保存在 `C:/Users/tanju/AppData/Local/Temp/studio-reference-ui-20261004-qv1fxj_0/analysis-board.jpg` 与同目录 `attachments.jpg`。没有执行真实模型调用、媒体生成、推送、部署或业务数据变更。

## 第二轮复核（2026-10-04）

结论：上轮修复覆盖了原反例，但“8 项已修完”不能作为完整流程验收结论。本轮确认 5 项仍需完善的问题，其中前两项影响参考正确性和最终交付。复核只追加证据记录，没有修改实现或正式测试。

### R9 · P1 · 准确参考只绑定观察入口，后续创作仍可换源

位置：`studio/agents/orchestrator.py:979–985`、`1078–1086`、`1148–1153`；`studio/reference_video.py:148–154`。

严格协议隔离反例：真实入站 `reference_target` 指定 A；观察准确复用 A。随后 `prepare_creative_context` 省略创作方式时沿用旧 B，显式指定 B 也被接受；甚至 `recreate` 已绑定 A 后仍可切到 B。实际模型请求包含 B 的完整 `timeline/audiovisual`，没有 A 的逐段画面或声音，保存提示词仍记在 A 名下，交付检查为 `complete`。

无旧参考的新项目同样有缺口：`prompt` 或作为方案依赖的 `analyze` 返回 A 的 findings/methods/limitations 简报；不含 `timeline/audiovisual`。默认上下文可成为 autonomous，模型没有收到 A 的逐段观察却能保存“基于 A”的提示词。

影响：UI 选对了参考和版本，最终提示词或方案仍可能借错视频、遗漏实际声画节奏，记录无法真实说明用了哪份依据。R5 的准确目标和 R2 的依赖延续尚未贯穿创作上下文。

修复边界：将本轮可信参考身份、分析版本和完整观察固定到实际创作输入；核验后续上下文及保存归属一致，拒绝模型换源。纯分析或提示词的输入绑定不应暗中改变整个项目的创作方式。验收须检查真正发出的主制片及角色输入，覆盖新项目、旧 B、显式换 B、`recreate` 后换 B 和分析依赖。

证据：`C:/Users/tanju/AppData/Local/Temp/reference_backend_reaudit_2834313eba5d478d86a117f9d87b114f.py`（4 场景）；主控独立重跑数据 `reference_reaudit_69o3ni_b`，另补 `C:/Users/tanju/AppData/Local/Temp/reference-second-review-20261004-477accb3/selected_context_repro.py`，数据 `reference_reaudit_s8lodu0v/recreate_then_override`。均使用 `legacy_task_contracts=False`、缓存分析和禁用观察器；没有真实模型生成质量结论。

### R10 · P1 · 失败恢复没有接续父任务的最终目标

位置：`studio/reference_video.py:367–370`、`studio/web/reference-video.js:165–170`、`studio/web/journey.js:685–686`。

真实自然对话接口隔离复现：原任务最终目标是 `writing`，参考子任务只存 `intent=analyze`，`instruction=只拆解开场的动作和声音`。下载失败后，UI 的“继续 / 上传原视频”提交到 `/reference-videos`，只带分析意图和该观察范围，没有父 Job、真实用户消息或原最终交付。恢复结果为 `succeeded`、参考 `completed`，但没有脚本，恢复 Job 的 `user_input_id=null`。

有效分析之后的“交给主制片继续”也没有准确原目标：它把 `task.instruction` 写成“原任务目标”。上述范围进入新原话后触发 `readonly=true/analysis_only=true`，尝试绑定原来的 `writing` 会被服务端拒绝。此处是模型的观察范围被重新包装成用户的新任务要求。

影响：R8 的状态/错误可见性已修，但恢复功能未实现完整的父任务接续；R2 在当前轮保留的最终目标在恢复入口又丢失。用户点击“继续”可能只得到报告，或无法继续脚本。

修复边界：从准确所属 Job 和原入站消息读取最终交付及观察范围，分别投影；恢复显式绑定原任务身份与本次用户继续动作。未完成观察先恢复依赖，再继续原目标；已保存分析直接复用。不能用参考 scope 冒充用户要求，不能依赖泛泛的“继续原任务”让模型猜目标。

证据：`C:/Users/tanju/AppData/Local/Temp/reference-ui-reaudit-20261004/test_continuation.py`、`ui_repro.mjs`、`continuation_guard.py`。主控独立重跑自然 API → 实际 UI 载荷 → API 恢复，以及新草稿的任务范围校验；结果一致。

### R11 · P2 · 首页确定拒绝后无法纠正首次输入

位置：`studio/web/journey.js:797–804`、`818–823`；附件重复和数量边界在 `studio/conversation_input.py:90–95`、`studio/http_contracts.py:95`。

真实浏览器复现：从首页参考入口连续添加同一个 `reference.mp4` 两次，首次请求因重复附件明确拒绝。原 frozen 身份和清单仍保留；移除一项后再次发送被旧 manifest 一致性检查挡住，显示“请重新选择上次发送的原附件，文件与顺序保持一致”。修改文字则显示“上次发送还未确认”。没有允许纠正确定失败或放弃原冻结草稿的入口。

数量上限为 24，前端追加未预检；25 项也会产生不可原样成功的确定拒绝。首页在上传前冻结，超限文件等可纠正的上传错误同样不能简单移除后再发。按钮状态还有不一致：失败 `finally` 会启用移除按钮，刷新恢复时却禁用；能点不代表修正能提交。

影响：R4 的追加与原样失败重试已覆盖，缺少确定失败后的修正事务。用户被要求纠正附件，但正常修正路径无法成功；这个冻结草稿还会影响下一次首页创作。

修复边界：区分“结果未知”与“已经明确拒绝”；前者保留准确身份核对，后者允许修正并建立新的提交身份，同时复用已确认上传字节及已有项目。冻结前检查重复文件、数量、大小和文件名，不能通过放宽后端校验解决。

证据：`C:/Users/tanju/AppData/Local/Temp/reference-second-review-20261004-477accb3/home_repro.mjs`、`media_repro.py`，以及同目录真实浏览器截图 `home-definite-rejection.jpg`。真实应用位于独立临时 Store、端口 4763，模型禁用；实际出现同一阻断提示，用量为 0。

### R12 · P2 · 旧空闲待确认提交重发时改变原载荷

位置：`studio/web/runtime.js:314–316`、`697–702`；冲突校验 `studio/conversation_input.py:53–60`。

隔离复现：旧 pending 保存 `{text, request_id}`，确认重试经 `submitJob` 会增加 `reference_consent=true`；同一 ID 在原入站回执中为 false，真实接口返回 `409 request_conflict`。旧 steer 已保留原载荷，此项只覆盖空闲 Job 恢复。该行为在本轮修改前已存在，属于遗漏的历史兼容问题。

覆盖边界：正常 `restoreJob` 若先查询到 received，会直接清除 pending，因此不是所有旧请求都失败；问题出现在该旧冻结载荷实际进入确认/重试时。即使原回执不存在，重发也不应凭当前默认值改变原本的观察准入。

修复边界：新消息才添加默认值；已保存待确认请求逐字段保持原状。确认入口优先查询准确回执，再依据结果恢复；不能靠服务端忽略内容差异解除冲突。

证据：`C:/Users/tanju/AppData/Local/Temp/reference-ui-reaudit-20261004/ui_repro.mjs` 的 `LEGACY_IDLE_RETRY` 与 `test_continuation.py` 的 `test_legacy_idle_pending_retry`；主控独立重跑确认。旧 steer 原载荷重试也复测通过。

### R13 · P2 · 忙时进度消息被草稿附件阻断，提示的移除动作不可用

位置：`studio/web/app.js:2135–2137`、`studio/web/conversation-input.js:160`。

隔离真实控制器复现：此前失败发送留下已上传的参考附件，项目通过其他入口开始任务；输入“现在进度怎么样”后尝试发送。控制器拒绝所有附件，包括已上传引用；文字及附件保留，没有新 POST，但提示“请先移除待上传附件”，而忙时移除按钮禁用，插入调整按钮仍可点击。

影响：没有附件无声丢失，但待发送附件阻断了本来应能送达的纯文字进度询问；界面指向一个无法执行的解决动作。用户只能等待或停止当前任务。

修复边界：忙时提供可用的草稿附件移除/暂存动作，或按现有契约支持已上传引用随中途消息保存；保持附件用途和原话，不为了进度询问取消在途工作。未上传的新字节仍须遵守运行时素材保护。

证据：`C:/Users/tanju/AppData/Local/Temp/reference-ui-reaudit-20261004/ui_repro.mjs` 的 `BUSY_UPLOADED`，主控独立重跑；`preserved=true`、`removeDisabled=true`、`adjustmentButtonDisabled=false`、`newPosts=0`。

### 其他边界与验证限制

- 裸 URL → 用途澄清 → 用户回答的商品/参考两条路径离线验证可用，各只调用对应观察器一次；没有发现该路径的新漏项。
- 本轮重跑现有相关检查：主控 28 个 Python、85 个 JavaScript 全部通过；另新增 Git 外反例与真实浏览器复现确认上述缺口。通过原用例不证明跨步骤输入及失败纠正成立。
- 未做真实 TikTok 下载、百炼声画识别或真实模型完整创作验收。这是仍未完成的验证，不是本轮凭空认定的供应商缺陷；没有花费、部署或修改真实业务数据。

## 第二轮实施记录（2026-10-04）

R9–R13 已落实到同一套既有任务、回执和草稿机制，未建立第二套任务状态。

- R9：准确参考及分析指纹绑定主制片上下文、素材摘要、方法输入和角色任务。新项目与旧 B 参考场景都传入 A 的完整逐段声画观察；后续换 B 或自主模式被拒绝，保存提示词再次核对实际上下文。任务已声明新参考依赖时，准备阶段不能跳过它而先用旧 B 写方案。提示词适配不改全项目方式；分析作为方案依赖时为最终创作绑定 A。历史缓存缺少逐段或声音数据时保留原文字及限制，不宣称完整声画观察。
- R10：恢复绑定当前准确父 Job、原真实用户回执和冻结任务解释；最终交付与观察范围分别投影。未完成观察先补依赖再继续原目标，已完成观察直接复用。本次新的继续原话及补充进入主制片和角色；原目标为分析也交给文字模型处理本次问题，因而有文字调用，但不重复原片观察。旧独立任务继续自己的分析/复刻目标。再次失败按新 owner 恢复；重启不自动续跑，停止/进度查询及改变交付的消息不能借恢复入口执行原制作。
- R11：冻结前检查重名、24 项数量、64 MB、文件名和格式。明确拒绝后可纠正并建立新身份，复用原项目及按文件名/字节确认的上传；未知创建或部分上传维持原身份核实，未知 Job 提交交回原回执恢复入口。
- R12：新消息才添加当前默认字段。旧空闲和忙时 pending 按原冻结载荷查询及重发，不补观察准入、附件或最新展示上下文。
- R13：实际任务在执行时允许移除浏览器中未发送的附件，已上传项目素材保留；上传、提交和未决回执仍保护原草稿。移除后纯文字消息使用既有中途消息分类，进度询问不停止在途任务。

回归覆盖位于 [tests/test_reference_context.py](../tests/test_reference_context.py)、[tests/test_reference_recovery.py](../tests/test_reference_recovery.py)、[tests/js/test_home_submission.mjs](../tests/js/test_home_submission.mjs) 及既有参考/草稿/runtime 用例。严格协议测试不注入旧任务解释，核对真正发出的模型请求；HTTP 和浏览器故障均为明确标识的离线模拟，不作为真实供应商成功证据。

浏览器使用端口 4763、独立 Git 外 `repair-data` 和合成参考视频，真实应用控制器配合一次 409 故障注入及可控离线长任务。实际验证了重复附件在创建前阻断；明确拒绝后修改文字、刷新、再次发送（同一项目、一个上传 POST、两个不同请求 ID）；忙时刷新后移除草稿并发送进度原话（分类为 status，无取消记录，原任务随后 succeeded，项目视频仍为一份）。浏览器无脚本错误或警告。预览服务及临时标签已关闭，原工作台未改动。

最终验证：完整 Python 模块三个隔离进程为 `601 + 706 + 657 = 1964 passed`、`2 skipped`；完整 JavaScript 为 `632 passed`、`2 skipped`。全量后最后复核新增了「新参考依赖不能在准备阶段跳过、转用旧 B」的严格反例，修复后重跑全部涉及 `video_use` 的参考/商品路径及上下文用例 `65 passed`，其中包含该新增回归。另一次跨参考/角色实际输入检查 `75 passed`、`1 skipped`。最终 diff 和 UTF-8 编码检查通过，原有需显式开启的 live 跳过项及 Starlette/AnyIO 弃用提示保留。

截图：`C:/Users/tanju/AppData/Local/Temp/studio-reference-ui-20261004-qv1fxj_0/repaired-home.jpg` 与 `repaired-progress.jpg`。本轮仍未调用真实模型/视频获取或媒体生成，没有费用、提交、推送或部署；真实下载成功率、声画识别质量及完整创意质量仍需另行验收。
