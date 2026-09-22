# 在另一台电脑继续工作

代码与共用 Skills、学习文字库继续通过 Git 同步；项目记录与图片视频作为 ZIP 放在同一**私有 GitHub 仓库的 Release 附件**中。ZIP 是某一时刻的快照，不是双向同步。运行目录仍在 Git 仓库外。

## 2026-09-20 备份范围

Release：`studio-backup-20260920`，附件 `studio-projects-20260920.zip` 与 SHA256 文件。

- 两个现有项目：`bag-small-gym-studio-20260919` 与 `smoke-lunch-bag-01`。
- 包含对话、事件、方案及其版本、审阅、账目、项目素材和迁入的历史档案。原记录按字节保留，不重新解释批准或抹掉历史费用。
- 保留角色分配、模型名称、价格与预算；移除连接配置中的加密 Key。不包含 Codex 登录、百炼凭据、环境文件、Python 虚拟环境或服务日志。
- 多功能包的 20 份精选历史文本、15 份当前素材全部包含。唯一排除的是非精选旧接口响应 `history/archive/resume/renders/wan-19s-v1/latest-result.json`：其中含签名下载地址。原件仍在源电脑，对应已下载视频包含在备份中。原历史 manifest 保留原样，备份 manifest 明确记录此缺件，因此这不是原目录的完全逐字节镜像。
- 学习文字库在 Git 中；共享参考库位于原电脑 `US`、`workbench-discovery/projects` 目录的外部媒体不在此包中，笔记本可能无法播放那些灵感参考。这与当前多功能包项目内的素材和历史视频不同。

## Windows 笔记本首次恢复

需要 Python 3.12、Git、GitHub CLI（已登录且有私有仓库访问权限）。也可在 GitHub Release 页面手动下载附件。Codex CLI 或桌面客户端须在笔记本安装并登录；API 服务的 Key 在笔记本「模型设置」重新填写，原电脑的 DPAPI 凭据不能跨电脑使用。

先 clone/pull 私有仓库的最新代码，再在 PowerShell 执行。示例假设仓库位于 `C:/Projects/e-business`；将第一行改为你的实际位置：

```powershell
Set-Location 'C:/Projects/e-business/apps/us-video-studio'
$studioDownload = Join-Path $env:USERPROFILE 'Downloads/Little-Else'
gh release download studio-backup-20260920 --repo Jeffery-Tan/e-business --pattern 'studio-projects-20260920.zip*' --dir $studioDownload

$studioArchive = Join-Path $studioDownload 'studio-projects-20260920.zip'
$studioExpected = ((Get-Content "$studioArchive.sha256" -Raw).Trim() -split '\s+')[0]
if ((Get-FileHash -LiteralPath $studioArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $studioExpected) { throw '备份包校验失败，请重新下载' }

$studioRuntime = Join-Path $env:LOCALAPPDATA 'LittleElse/Studio/runtime-venv'
py -3.12 -m venv $studioRuntime
$studioPython = Join-Path $studioRuntime 'Scripts/python.exe'
& $studioPython -m pip install -r requirements-runtime.txt

# 必须是尚不存在的新目录，恢复工具不会覆盖已有工作。
$studioData = Join-Path $env:LOCALAPPDATA 'LittleElse/Studio/restored-20260920'
& $studioPython -m studio.backup restore $studioArchive --data-root $studioData
if ($LASTEXITCODE -ne 0) { throw '恢复失败，不启动服务' }
$env:STUDIO_DATA_ROOT = $studioData
& $studioPython -m studio.server --port 4760
```

打开 `http://127.0.0.1:4760/`。恢复不会调用模型。先查看多功能包历史、材料和对话，再确认模型设置。以后启动服务时也要设置相同的 `STUDIO_DATA_ROOT`；这是当前进程环境变量，不修改系统全局配置。端口若已有服务占用，先确认并关闭原服务，勿同时启动两个写入同一数据根的服务。

`requirements-runtime.txt` 列出完整直接运行依赖；旧 `requirements-local.txt` 只为开发机补依赖，不能用于空白笔记本环境。视频本地剪辑仍需另有 FFmpeg；恢复和文字策划不依赖它。

## 从当前电脑导出下一份备份

在 `apps/us-video-studio` 目录下，用已安装运行依赖的 Python：

```powershell
python -m studio.backup export --data-root '你的当前数据根' --output '数据根以外的新ZIP路径' --code-commit (git rev-parse HEAD) --omit-history-file 'runs/bag-small-gym-studio-20260919/history/archive/resume/renders/wan-19s-v1/latest-result.json'
python -m studio.backup verify '刚导出的ZIP路径'
```

默认要求对应服务已关闭，导出会获取原写入锁。服务仍开着但无任务时，可追加 `--live-url http://127.0.0.1:4760`：导出前后检查服务项目列表、任务恢复版本及全部源文件哈希，发现任务运行、未知费用或来源变化即失败并撤回本次未完成 ZIP。不要在导出过程中编辑项目。遗漏白名单只允许未精选的历史 JSON，不能排除当前项目记录、精选历史或素材来绕过检查；其他疑似凭据会阻止导出。

当前在线导出仍有兼容限制：它要求磁盘项目与 `/api/runs` 列表完全一致，并拒绝未决用量；因此有回收站项目或未知用量时不能在线导出。这不是创作需要核账的理由。此时正常停止服务后使用默认离线导出，保留包括回收站标记在内的全部记录；不要清空台账、恢复全部项目或删目录来绕过校验。新项目和后续对话不会自动写进 2026-09-20 的旧 Release 快照。

导出成功后将 ZIP 与 SHA256 上传到新的私有 Release，保留旧备份。不要把备份 ZIP 放进 Git，也不要把备份发布到公开仓库。

## 两台电脑接续工作的约定

先在电脑 A 停止创作，导出最新快照，再在电脑 B 下载恢复到**新目录**并切换 `STUDIO_DATA_ROOT`。返回 A 时同样操作，原目录保留作恢复点。

目前不支持自动合并两台电脑各自新增的聊天、方案、批准与账目；不要两边同时推进同一项目，也不要用文件同步软件覆盖运行中的目录。恢复工具会拒绝覆盖已有目标，避免用旧备份抹掉新进展。新的学习文字库改动仍需单独 Git 提交与推送。
