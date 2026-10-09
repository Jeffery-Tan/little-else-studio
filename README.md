# Little Else Studio

独立维护的 US 创作工作台。代码仓库：[Jeffery-Tan/little-else-studio](https://github.com/Jeffery-Tan/little-else-studio)（私有）。源版本为电商仓库 `3021e39`，保留相关提交历史；后续软件维护在本仓库完成。正式运行服务尚未切换，切换与回退见迁移说明。应用目录保留 `apps/us-video-studio`，以减少搬迁对已有接口与测试的影响。

## 开发与启动

三个目录分别负责代码、业务资料和运行数据：

- 本仓库：工作台代码、执行脚本、方法卡、技术文档、测试与 CI。
- `BusinessRoot`：电商仓库的 US Skill、学习库、案例和研究资料；不在本仓库维护第二份当前库。
- `DataRoot`：原工作台运行目录，位于 Git 外。迁移不复制或改写历史、批准、凭据与生成记录。

Python 3.12；依赖沿用 `apps/us-video-studio/requirements-runtime.txt` 和 `requirements-local.txt`。前端测试使用 Node。

先检查，不启动或创建运行目录：

```powershell
./scripts/start.ps1 -BusinessRoot D:/projects/E-commerce -DataRoot <existing-data-root> -CheckOnly
```

正式切换时确认旧服务任务已经结束并正常停止，再去掉 `-CheckOnly`。不要对同一数据根启动两个服务。首次创建测试工作台才使用 `-AllowNew`。

直接运行 Python 模块需要在当前进程设置绝对路径 `STUDIO_BUSINESS_ROOT`；缺失或无效时明确拒绝，禁止悄悄创建空业务库。`scripts/start.ps1` 自动传入该配置。

## 离线检查

```powershell
python -m pytest apps/us-video-studio/tests -q
node --test apps/us-video-studio/tests/js/*.mjs
python scripts/validate_text_encoding.py --root .
```

pytest 使用 `tests/fixtures/business` 的固定资料，不读取操作员当前业务库。该快照仅用于兼容性测试，不能作为当前经营事实或生产方法库。现有测试覆盖程序行为和模拟模型边界，不证明真实模型或内容质量。GitHub CI 在 Windows 与 Ubuntu 各分四组执行全部顶层 Python 测试文件，每组最多 40 分钟；JavaScript 每个平台执行一次，另有提交历史密钥扫描。分组仅分配执行文件，不跳过测试或改变断言。

应用使用与接口详见 [应用说明](apps/us-video-studio/README.md)。技术文档中的外部业务与历史来源链接指向电商仓库；实际业务读取仍由 `BusinessRoot` 决定。本 README 和 [迁移说明](docs/migration.md) 是独立仓库的启动入口。
