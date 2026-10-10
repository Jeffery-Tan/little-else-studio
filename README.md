# Little Else Studio

独立维护的 US 创作工作台，也是唯一开发与运行入口。代码仓库：[Jeffery-Tan/little-else-studio](https://github.com/Jeffery-Tan/little-else-studio)（私有）。源版本为电商仓库 `3021e39`，保留相关提交历史。电商旧工作台入口已退役，无须接续旧项目或维持双入口。应用目录保留 `apps/us-video-studio`，作为本仓库内部路径。

## 开发与启动

三个目录分别负责工作台、可选经营事实和运行数据：

- 本仓库：工作台代码、四个 US Skills、唯一当前学习库、案例、创作方法、执行脚本、技术文档、测试与 CI。
- 可选 `BusinessRoot`：电商商品、供应、价格与经营研究的只读来源；不提供或接收工作台学习库。
- `DataRoot`：本工作台的 Git 外运行目录。新项目使用新空目录，首次启动加 `-AllowNew`；不要求导入旧项目、批准或凭据。

Python 3.12；依赖沿用 `apps/us-video-studio/requirements-runtime.txt` 和 `requirements-local.txt`。前端测试使用 Node。

先检查，不启动或创建运行目录：

```powershell
./scripts/start.ps1 -DataRoot <new-empty-directory-outside-Git> -AllowNew -CheckOnly
```

诊断通过后去掉 `-CheckOnly` 启动新工作台；首次启动加 `-AllowNew`，后续重启同一目录不再需要。不要对同一数据根启动两个服务。旧数据保留原位，仅供历史追溯；当前没有旧服务迁移或回退前置条件。

Studio 无须连接电商目录即可启动、读取方法和维护学习库。需要引用经营事实时，在启动命令加 `-BusinessRoot D:/projects/E-commerce`，或直接运行 Python 时设置进程级绝对路径 `STUDIO_BUSINESS_ROOT`。配置目录必须存在；该配置不会改变知识归属，也不授权写入经营资料。

## 离线检查

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m pytest apps/us-video-studio/tests -q
node --test apps/us-video-studio/tests/js/*.mjs
python scripts/validate_text_encoding.py --root .
```

pytest 使用 `tests/fixtures/business` 的固定资料，不读取操作员当前业务库。该快照仅用于兼容性测试，不能作为当前经营事实或生产方法库。现有测试覆盖程序行为和模拟模型边界，不证明真实模型或内容质量。GitHub CI 在 Windows 与 Ubuntu 各分四组执行全部顶层 Python 测试文件，每组最多 40 分钟；JavaScript 每个平台执行一次，另有提交历史密钥扫描。分组仅分配执行文件，不跳过测试或改变断言。

应用使用与接口详见 [应用说明](apps/us-video-studio/README.md)。当前创作方法链接指向本仓库；未迁入的经营资料及历史来源链接固定指向电商来源版本。学习库引用的九份历史证据保留原字节，不能当当前商品事实。本 README 和 [迁移说明](docs/migration.md) 是独立仓库的启动入口。
