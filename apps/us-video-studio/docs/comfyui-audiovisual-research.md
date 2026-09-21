# ComfyUI 声画理解调研

核对日期：2026-09-21。用途：US 独立制片室的素材理解与成片验收设计。方法：读取官方及代表性第三方节点源码，沿输入处理到模型调用追踪；没有安装节点、下载模型权重或执行真实推理。下述兼容疑点属于静态审查，不能当作已复现故障。

## 结论

ComfyUI 提供媒体类型、节点组合和模型接入。具体理解来自所接模型，以及节点实际保留下来的画面、音频和时间信息。此次检查的实现包含云端联合理解、本地 Omni 模型，以及抽帧与语音转写组合，能力不能互相等同。

## 官方实现

官方 `VideoInput` 以画面、音频和帧率组成媒体对象，支持按起点与时长裁剪。`VideoFromFile` 用 PyAV 处理视频和音轨，并在片段解码时处理音频起始偏移与时长。对我们的启示：裁剪必须保持共同时间范围，同时保留原片时间映射。[媒体接口](https://github.com/Comfy-Org/ComfyUI/blob/b0f4b7b294ce482a2e071d9d762c133d38c7aa07/comfy_api/latest/_input/video_types.py)、[文件实现](https://github.com/Comfy-Org/ComfyUI/blob/b0f4b7b294ce482a2e071d9d762c133d38c7aa07/comfy_api/latest/_input_impl/video_types.py#L464)

`Google Gemini` 理解节点接受视频、音频、图片与提示，优先上传媒体取得 URL，部分场景使用内嵌编码，通过 Comfy Partner API 请求模型并返回文本。当前 V3 节点在 Gemini 3.8 Flash 选项开放 static/agentic：固定采样，或由提供方按问题探索时间线。它没有自动建立 Studio 的商品证据、版本绑定与成片审核结论。[官方节点源码](https://github.com/Comfy-Org/ComfyUI/blob/b0f4b7b294ce482a2e071d9d762c133d38c7aa07/comfy_api_nodes/nodes_gemini.py#L400)

提供方文档说明视频默认固定采样可能漏掉快速动作；动态探索会按需读取画面、音频或转写。需区分音频实际参与与只读转写，也不能把动态检索成功当作全片每段都被检查。[Gemini 视频处理](https://ai.google.dev/gemini-api/docs/video-understanding)

## 第三方代表节点

| 仓库与核对版本 | 实际路径 | 对我们意味着什么 |
| --- | --- | --- |
| SXQBW/ComfyUI-Qwen-Omni，67f27a3 | 本地 Qwen2.5-Omni 3B/7B；预处理最多 15 帧、最长边 384；配置视频内置声音或外部音频 | 模型具备联合能力，但必须核实节点是否完整传入声音和时间信息 |
| 1038lab/ComfyUI-QwenVL，1b67b44 | 该 Transformers 路径取视频帧并传给视觉处理器；默认 16 帧，输入端口为 IMAGE | 本路径提供画面理解，没有音轨输入，不能据此验收声音 |
| Qo-qiao/ComfyUI-omni-llm，c061219 | 已检查的统一推理路径把 ASR 结果拼入提示词，将视频帧作为图片传入；取帧限制为 15，部分模型分支再限制到 8 | 适合对白内容与画面摘要；原始语气、音效和精确声画对应不能由此得到保证 |

各行依据：[Qwen-Omni 节点](https://github.com/SXQBW/ComfyUI-Qwen-Omni/blob/67f27a333da0d1e764a893a9173b0413e643d2c2/QwenOmni.py#L594)、[QwenVL 节点](https://github.com/1038lab/ComfyUI-QwenVL/blob/1b67b443918801f571714bab636edc1845b7002a/py/AILab_QwenVL.py#L783)、[ASR 与视频帧组合](https://github.com/Qo-qiao/ComfyUI-omni-llm/blob/c0612199851b7e7e2995ad25adbf32b016cec079/nodes/unified_inference.py#L1726)。这是代表性路径审查，未声称覆盖这些仓库全部后端或整个 ComfyUI 生态。

Qwen-Omni 的联合能力由模型实现；Qwen 官方说明其时间位置编码用于对齐音频和视频时间。[模型说明](https://github.com/QwenLM/Qwen2.5-Omni#key-features)

**一处接线疑点**：SXQBW 节点把原视频变为 PIL 帧列表，再在内置音轨模式调用 `process_mm_info(...use_audio_in_video=True)`；所核对 Qwen 官方音频工具的该分支需要打开视频来源提取音轨。两者的数据类型存在明显不匹配，需固定依赖版本复现，不能把该插件视为已经通过验收的原样接入方案。[节点调用](https://github.com/SXQBW/ComfyUI-Qwen-Omni/blob/67f27a333da0d1e764a893a9173b0413e643d2c2/QwenOmni.py#L704)、[官方音频读取](https://github.com/QwenLM/Qwen2.5-Omni/blob/main/qwen-omni-utils/src/qwen_omni_utils/v2_5/audio_process.py)

## 对 Studio 的设计建议（尚未实施）

1. 沿用现有素材原件和 JobManager，把同步裁剪的视频与音轨交给专门的观察适配器，记录原片范围和实际送入范围。不得将只有图像帧的输入标成声画观察。
2. 声画模型负责联合观察；语音识别与文本对齐作为可选辅助，用来核对对白和字幕。两条路径保留独立来源，不把转写推断成语气与表演事实。
3. 先覆盖全片，再按具体疑点复查短片段。前三秒、快速商品动作、切点和结尾需要单独验证采样覆盖；时间戳合法只证明引用未越界，不证明内容判断正确。
4. 返回有来源的时间线观察，再由独立审核对照商品图和已采用脚本判断内容。参考理解与成片验收使用不同任务要求，沿用原版本、证据与费用边界。
5. 接入先做小规模真实模型对照，使用换音轨、静音、对白缺失、短暂动作缺陷等样本测漏报和误报。是否本地部署取决于实测质量、硬件占用与成本；本轮未评估本机运行这些模型的性能。

ComfyUI 值得借鉴的是媒体接口和可组合的观察步骤。其已查通用节点主要输出模型文本，我们的商品一致性、证据可信度、返工责任和验收停止条件仍需在 Studio 中实现。
