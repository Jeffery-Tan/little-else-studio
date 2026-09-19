# Wan 视频提供方边界

2026-09-19 查阅阿里云官方 [Wan 3.0 API](https://help.aliyun.com/zh/model-studio/wan3-video-generation-api-reference) 与 [标准模型价格](https://help.aliyun.com/zh/model-studio/wan3-0-video)。本页描述协议核验及离线测试，不代表真实任务已经生成成功。

## 此次接入

`studio/agents/wan_video.py` 使用北京业务空间的 `wan3.0-video`。范围是单张首帧图片与提示词；图片直接作为 data URL 放进一次创建请求，无独立临时上传。支持 2–30 秒整数时长、480P/720P/1080P、支持的宽高比和音频开关；提示词改写默认关闭，避免隐式修改已经审过的输入。首帧需为静态、不透明图片，单边 240–8000 像素、宽高比不超过 8:1、20 MB 以内。工作台可以采用更严格的素材限制。

北京标准模型公开原价为每秒人民币 0.3 / 0.6 / 1.2 元，分别对应 480P / 720P / 1080P。`estimate_cost` 返回带地域、来源及核验日期的原价估算；不计算折扣，不冒充账单，不转换成美元。提供方返回的用量只保留明确的有限非负数字；缺失用量和实际金额保持未知。

## 调用接口与责任

- `WanVideoRequest` 固定提示词、图片原始字节及 SHA-256、时长、清晰度、比例、声音和提示词改写开关。
- `existing_endpoint()` 只读非密钥元数据，以便调用方在提交前将业务空间写入 intent。`WanVideoClient.submit(request, expected_endpoint=...)` 在加载凭据后再次比较空间，一致才发送一次 POST，返回 `WanVideoResult`，含 `task_id`、`status`、`endpoint`、`video_url`、`request_id`、`usage` 和未知的 `cost_usd`。不会轮询或重试。
- `poll(task_id, endpoint=...)` 只查询同一个已保存任务。旧任务的业务空间被固定；连接空间变化时拒绝查询到新空间。提供方任务与结果链接均有 24 小时期限，过期不能视为未调用。
- `download(url, max_bytes=...)` 限定 HTTPS 百炼结果 OSS 域名，禁用跳转，不加载、不发送 API Key；下载上限 150 MB，可由调用方进一步收紧。只返回字节，不写任何磁盘文件。

调用方必须先保存用户点击产生的准确图片审批、费用确认和本次 intent，再进入 `submit`。适配器只检查哈希一致性，不能赋予图片审批。原 Lunch trial 文件、旧批准和旧运行记录不会被读取或迁移。

网络异常、5xx、408、跳转或不完整的提交响应会抛 `WanVideoError(submission_unknown=True)`，必须核实提供方记录，不能重提生成。明确 HTTP 4xx 拒绝与查询/下载失败分别保留状态；查询和下载恢复只操作原任务，不新增付费生成。

下载校验包含 MP4 容器头和大小，**不证明视频可播放或内容成立**。集成层还必须实际探测/解码视频，并在项目素材目录以不可覆盖名称落盘，建立字节指纹与本次任务关联。链接、API Key 和原始提供方错误正文不得暴露到前端日志。

## 凭据和离线证据

`existing_credentials_available()` 仅检查原凭据文件是否存在，不读密钥内容。显式 `load_existing_credentials()` 复用 `%LOCALAPPDATA%/E-commerce/credentials/dashscope.dpapi` 的 Windows 用户加密凭据及同目录 `dashscope.json` 的地域/业务空间；仅真实提交或查询现有任务时调用。构造客户端、读取页面、预算估算、下载及离线测试均不解密凭据，不修改配置。

`tests/test_wan_video.py` 使用 `httpx.MockTransport` 和合成图片覆盖正确协议、精确字节绑定、拒绝无效输入、提交不重试、超时歧义、恢复空间固定、用量未知及下载大小/域名/跳转限制。MP4 头部测试夹具故意不是可播放视频，只证明传输层容器嗅探边界。
