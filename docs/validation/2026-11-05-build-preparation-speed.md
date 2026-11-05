# 解析准备提速与投放身份手动刷新

状态：本地实现和验证完成，尚未推送或发布生产。基于本地 main 的 `1d352165`；本轮提交主题为 `builds: reduce preparation waits and refresh identity catalogs`。

用户要求为投放身份补齐手动刷新，并按已完成的耗时调查加快解析准备。此前线上只读样本为一剧一账户，草稿保存至 READY 约 54 秒，其中场景资料约 14.8 秒；网眼取链七轮包含会话过期、登录恢复及应用校验、两页搜剧、已有链接查询和回读。该样本是发布前观察，不能作为本轮提速后的效果。

## 改动

- 身份卡片正常状态下显示刷新图标；异常状态的“更新可用投放身份”也先使服务端缓存过期，再启动准备。刷新中禁止重复操作，失败原因显示在卡片。沿用现有 shadcn/ui 组件。
- 新增 `POST /build-drafts/{draft_id}/identities/refresh`，与小程序刷新共用场景缓存失效逻辑。仅限有 build 权限的本租户草稿、其账户、BC、当前连接和完整冻结路由；包括已得到身份但 Mini 仍在读取的快照。快照同时承载身份和小程序等资料，因此显式刷新会使该范围的场景资料整体过期，保留用户选择。普通准备仍复用有效缓存，24 小时有效期不变。
- 版权方会话恢复已有进展时立即续步，不再每个成功步骤固定等待五秒。其他领取者仍在执行时继续等待五秒；真实网络故障、持久化 due_at、冷却及未知写结果保护保持。
- 账户分页已有进展、账户解析结束、已有链接交接后立即续步；无新结果的远端链接/场景等待保留原退避，素材仍按既有五页/一秒预算保存断点。
- 场景 Worker 同次领取最多读取五页，累计八秒后不再启动下一页；单页不可中断，所有页共用原任务四十秒工作截止时间。每页沿用原权限、租户/BC、路由、准入、claim 和代数校验，并单独提交结果与持久续步。仅成功页面返回下一代，等待、失败、旧消息均结束本轮；旧代 outbox 消息自然幂等退出，不人工删除任务。

本轮选择缩短成功步骤之间的等待及有界连续读取，没有新增队列、Worker、定时预热、平台额度、配置开关、数据库迁移或持久任务正文。共用资源队列繁忙时首次领取仍可能等待；未声称彻底消除负载波动。

## 验证

先验证旧代码在立即续步断言、缺少身份刷新接口/按钮及缺少场景连续读取入口上失败，再实现修复。后端使用本轮独立 Docker PostgreSQL 18、Redis 8，数据库名包含 `_test`；只在外部传输边界使用替身，没有调用真实版权方或 TikTok 接口。

- 后端范围：`tests/modules/builds/test_draft_tasks.py`、`test_api.py`、`tests/modules/providers/test_auto_relogin.py`、`test_preparation_tasks.py`、`tests/modules/builds/scene_jobs/`，142 passed / 209.52 秒。
- 前端：`playwright test tests/build-preparation.spec.ts --project=workspace --workers=3 --reporter=line`，83 passed。覆盖正常身份刷新、两类目录失败后的更新、只读、未知结果恢复、缓存搜索与原选择行为。
- `bun run build`：TypeScript、正式版及体验版 Vite 构建通过；没有生产发布。
- 本轮 Python 文件 Ruff check 通过；前端三个手写改动文件 Biome check 通过。
- `scene_jobs.py`、`scene_tasks.py`、`mini_selection.py`、`providers/tasks.py` 严格 mypy 通过。`api.py` 与 `draft_tasks.py` 仍有四项既有类型问题：`fail_initial_candidate_snapshot` 两个 untyped 调用、`get_frozen_ads` 的 untyped 调用和 Any 返回。精确 HEAD shadow 对照确认原有这四项，另有一项旧 `CursorResult` 泛型问题已在本轮触及的函数中修正；未把整个模块类型检查称为通过。
- `demo:coverage` 未通过：既有 14 个 `business-*` 接口缺少演示处理器（dashboard、download、dramas、evidence、export、export_status、mapping、member、options、rates、read_evidence、rebuild、runs、set_rate）。客户端/演示 operation 注册表按实际 OpenAPI 重新生成，补入新身份刷新接口时也同步了这些已存在的业务接口元数据；本轮新增刷新接口已提供演示处理器，未扩展经营业务演示功能。

证据位于本地忽略目录 `.runtime/preparation-speed-20261105/`；无真实广告创建、激活、取链写入、生产配置变更或上线耗时对照。
