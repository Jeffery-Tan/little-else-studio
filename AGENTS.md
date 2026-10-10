# Little Else Studio development

- 默认用中文沟通，代码、命令与文件名使用 English。解释有影响的选择及产品取舍，普通可逆工程决定自行处理。
- 修改应用前读 `apps/us-video-studio/CLAUDE.md`；保留其中的决定、费用、审批、停止、原任务核实、不可变版本及单 writer 约束。
- 当前能力仅 US。四个 US Skills、学习库、案例、创作方法和执行代码由本仓库维护，当前库位于 `tracks/us/creative/learning/library.json`。电商旧副本只供追溯，禁止形成第二个可写当前库。
- 商品、供应、价格、库存与经营研究仍归电商项目；可选 `STUDIO_BUSINESS_ROOT` 只连接经营事实，不改变学习读写路径。未连接时不得捏造事实，不得把固定历史证据、测试资料或 SEA 结论当当前 US 事实。
- 自然创作读取 `.agents/skills/us-commerce-strategy/SKILL.md`，再按交付读取视频或图文 Skill；恢复原任务按 `tracks/us/creative/learning/workflow-skills.md` 定位原 run。学习读取 `us-creative-learning`，保留原 diff 审核和应用门禁；库迁移不是新增学习结论或用户批准。
- 数据与凭据必须在软件仓库及业务仓库之外。不得复制实际运行数据、密钥、Cookie 或认证记录进 Git。
- 不调用真实模型或生成来验证代码。离线测试使用临时目录、固定测试资料与假模型；真实调用需要对应明确授权。
- 保留用户工作；不自行提交、推送、部署或切换现有服务。发布、修改运行记录、费用执行和外部副作用必须有明确授权。
- 先查 working tree，再编辑并验证最终差异。依赖使用已有 requirements；不要升级无关依赖。
- `.md`/`.csv` 使用 UTF-8 BOM；Skill frontmatter 文件使用 UTF-8 无 BOM。代码/配置使用 UTF-8 无 BOM；固定历史证据和测试快照保持原字节。改 Markdown 后运行 `scripts/validate_text_encoding.py --root .`。
- 行为变化运行相关测试。界面变化检查实际渲染，不能把构建成功或假模型测试当真实集成/内容通过。
