# Codex CLI 图片生成接入证据

核验日期：2026-09-19。对应模块：`studio/agents/codex_image.py`。

## 已确认

本机 `codex-cli 0.155.0-alpha.9.2` 的 `features list` 声明 `image_generation stable true`；由该二进制现场导出的 app-server JSON schema 包含：

- `modelProvider/capabilities/read` 与布尔值 `imageGeneration`。
- `imageGeneration` 事件，含 `result`、`status`、`savedPath`、`failure`。
- `turn/start` 的图片输入与隔离 thread 所需字段。

本轮无模型能力探测实际返回：`available=true`、`verified=false`、`model_invoked=false`。探测只生成本地 schema、读取受限会话配置与 provider 能力；没有创建 turn 或调用生成模型。22 项适配器测试使用合成图片和协议事件，未读取登录令牌，也未连接生成端点。

官方源码辅助解释原生协议：[image-generation/tool.rs](https://github.com/openai/codex/blob/main/codex-rs/ext/image-generation/src/tool.rs)。该实现的图片结果是 base64，参考可以来自当前输入图片；CLI 可另外保存自身缓存。源码 main 会变化，本机 schema 才是此次协议形状的核验依据；这些证据不代表账号额度或实际出图已经验收。

## 调用接口

`CodexImageClient().capability()` 返回本机能力；`generate(ImageRequest(...))` 仅由已获得本次生成授权、已写入费用 intent 的后端流程调用。

`ImageRequest` 接收 `prompt`、绝对路径且为空的 `output_dir`、最多五个 `reference_paths`、其允许目录 `reference_root` 和可选的主控 `model`。主控模型不是图片模型选择器；原生图片工具的模型由 Codex 决定。参考仅支持静态 PNG/JPEG/WebP，总字节不超过 24 MiB。

适配器建立全新 ephemeral thread，关闭 shell、浏览、MCP、插件、子 Agent 等能力，只开启原生图片工具；无执行环境，参考先在应用内校验，再作为 inline image 输入。模型不会收到原素材路径。输出只接收事件内图片字节，校验格式、尺寸与上限后用排他新建方式写到调用方给定的空目录；不读取服务器 `savedPath`。业务层仍负责把结果导入当前项目 materials、保存来源、图审与后续视频授权。

`ImageResult` 包含 `status`（completed/failed/unknown）、`images`（path/hash/MIME/尺寸）、`usage`（已观测主控文字总 token）、`observed_image_calls`、`usage_status`、实际主控模型及 thread/turn 标识。图片事件没有图生成费用或用量字段，因此 `usage_status` 保持 unknown，不能因文件成功或文字 token 已知而填零或声称已结清。

## 产品必须保留的限制

- app-server 没有可强制执行的“恰好一次图片工具调用”字段。一次按钮启动一个生成任务，不能保证模型仅调用一次。提示词只请求一次；观察到第二次立即结束，但不能声称阻止已经发生的第二次调用或费用。`single_image_limit_enforced=false` 必须保留。
- 图片失败、协议异常、取消、断连都不会自动重试。已保存的有效结果保留，用量待核实；重新生成需先恢复费用状态并获得对应授权。
- CLI 可能在自己的 `generated_images` 保存额外缓存。适配器不声称完全隔离 CLI 的底层文件写入，也不清理 Codex 自身缓存；业务输出只从验证过的内联字节导入。
- `available=true` 只表示本机协议和 provider 声明支持；`verified=false` 表示尚无真实出图验收。不能在界面标成“已测试成功”。