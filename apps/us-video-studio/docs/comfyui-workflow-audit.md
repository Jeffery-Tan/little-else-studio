# ComfyUI 工作流审计：可借鉴的能力与 Studio 边界

核对日期：2026-09-27。范围：官方 ComfyUI 文档和本仓库当前实现的静态对照；没有安装 ComfyUI、下载模型、执行节点或做真实质量与成本测试。声画理解节点的单独核对见 [ComfyUI 声画理解调研](comfyui-audiovisual-research.md)。

## 结论

ComfyUI 适合成为**可选的媒体制作引擎**，不适合取代 Studio 的制片交互或项目事实源。它的 App Mode 已经采用“复杂工作流在内部，用户只看选定的输入与输出”的方式；这与当前 Studio 的中央作品画布、右侧对话和按需打开技术记录一致。[App Mode 官方说明](https://docs.comfy.org/interface/app-mode)

当前 Studio 已有准确产物版本与 SHA、素材来源校验、JobManager、审核、图审和费用授权。ComfyUI 的工作流、队列和历史记录只描述节点执行，不能自动代表商品事实成立、脚本获批、参考图通过或用户授权花费。接入时应由 Studio 保留这些决定权。[Studio 交互契约](creator-interaction.md)、[运行与存储](independent-runtime.md)

| ComfyUI 能力 | 对 Studio 的可用启示 | 当前判断 |
| --- | --- | --- |
| App Mode 为节点图挑选输入和输出，并在窄屏切换输入、结果、素材；运行时可看队列和结果 | 创作者只看目标、作品、素材、审核理由及下一步，制作参数另行展开 | **现在吸收交互原则**。不引入节点编辑器或第二个创作者界面。[官方说明](https://docs.comfy.org/interface/app-mode) |
| 子图与工作流模板将节点组合封装和复用 | 把经过验证的图片处理或成片检查步骤做成固定“制作配方”；每次运行保留配方版本和输入指纹 | **按实际重复需求再做**。先有可复现的窄任务，再设计配方库。[子图](https://docs.comfy.org/interface/features/subgraph)、[模板](https://docs.comfy.org/custom-nodes/workflow_templates) |
| 局部执行只跑选定输出节点所需的分支，缓存事件说明哪些节点被复用 | 某段或某张图修改后，只重做受影响的媒体步骤；向用户准确显示本次重做范围和预计费用 | **有条件吸收**。局部执行不能绕过新输入的图审、产物版本和费用门禁。[局部执行](https://docs.comfy.org/interface/features/partial-execution)、[执行事件](https://docs.comfy.org/development/comfyui-server/comms_messages) |
| `/prompt`、`/queue`、`/history` 与 WebSocket 进度事件 | 若接入，可把 ComfyUI prompt ID 映射到现有 Studio job/attempt，断线后查询实际结果 | **只做单向适配**。Studio 的 JobManager 仍是任务与恢复的事实源，不建立第二套项目状态。[HTTP 路由](https://docs.comfy.org/development/comfyui-server/comms_routes)、[事件](https://docs.comfy.org/development/comfyui-server/comms_messages) |
| 工作流 JSON 和节点版本管理 | 冻结执行配方、模型及节点版本、输入素材 SHA、参数、种子、输出 SHA，形成可核对的媒体来源记录 | **接入前提**。工作流文件本身不足以证明 Studio 的来源和审批。[Registry 版本说明](https://docs.comfy.org/registry/overview) |

## 如果以后接入，先做的最小验证

1. 只选一个真实瓶颈，例如**已批准参考图的局部修图或合成**。在隔离目录里用固定样本比较当前做法与 ComfyUI 配方的结果、耗时、显存和实际费用；若没有稳定优势，就保持现状。
2. Studio 先生成不可变执行清单：项目、目标产物及准确 SHA、已批准参考图 SHA、配方与节点版本、模型、参数、种子、预期输出和费用上限。执行前再次核对图审与授权；执行后由 Studio 将输出验哈希入库，作为**待审核候选**呈现。
3. 适配器只翻译 Studio job 与 ComfyUI prompt/queue/history，不把 ComfyUI 的“执行成功”翻译成“内容通过”。超时、断线、重复提交和未知结果沿用现有任务恢复语义；异常不能悄悄重跑产生新费用。
4. 第三方节点按固定版本审查其代码、依赖、许可和输入输出边界。官方 Registry 支持版本锁定和扫描，但这些措施不等于 Studio 可直接信任任意节点。[Registry 说明](https://docs.comfy.org/registry/overview)

## 真人实测前的优先级

先验证 Studio 自己的“首页输入 → 方向 → 素材 → 脚本逐段反馈 → 审核决定 → 结果”链路。ComfyUI 此时提供设计参照，不是首轮可用性的依赖。只有真人使用暴露出明确的媒体处理瓶颈，且隔离对照证明质量、速度或成本收益，才值得把它接为新的执行后端。此判断是基于当前仓库能力和官方文档的产品推论，尚未经过 ComfyUI 本机运行验证。
