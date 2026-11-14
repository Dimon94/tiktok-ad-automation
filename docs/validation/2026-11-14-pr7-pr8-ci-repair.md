# PR #7 / #8 冲突与 CI 修复验证

## 范围与基准

- PR #7：原 head `83bbbc5b`，已有树 `fix-jb3-business-export-pr`；合入远端 main `456a2c86` 后修复基准 `ae923c8b`。
- PR #8：原 head `2995ade2`，已有树 `fix-jb6-historical-log-pr`；合入同一 main 后修复基准 `00adb938`。
- 两次合入仅冲突于 `docs/implementation-progress.md`，双方追加记录均保留。主工作树及其未提交内容未修改。
- 原最小目标是消除文档冲突并修实际 CI 红项。确认红项包含共同基线欠账后，范围扩展到 SQLModel/SDK 类型合同、链接恢复状态及前端回归夹具；不合并两份独立产品功能。情景收入保持既有四位格式。

## 修复

1. SQLModel 查询使用既有 `col()`，原始 SQL 复用 SQLAlchemy Session 类型视图；多列结果按 tuple 解构，边界模型复用 Pydantic 验证。固定三元 Decimal 返回保留原公式，SDK 严格成员形状保持。
2. datetime 字段显式表达等价 Column。两个脚本分别执行可重复机械修复和运行时边界验证；21 列的 SQL 类型、时区、nullable、default 表达式/形状、index、unique 均不变。0115 仅增加 typing.cast，去除类型视图后的迁移 AST 与基准一致，无 DDL 行为变更。
3. provider 可继续状态归 `link_steps` 所有。未知写入仍沿原任务只读找回；明确多候选转 `manual_review` 并保留 Unknown。执行器、直接领取、有界查询和 watchdog 共用终态/停止阶段定义，不再重复 POST 或空投人工任务。
4. 草稿仅等待未被手动链接替代的自动输入；跨页保留 pending。VIDEO/CAROUSEL 人工补链可结束，原未知 checkpoint 不改写。
5. 素材页跨 BC 保留租户级筛选及绑定原 BC 的传输；新上传使用新顶栏 BC。其他旧 BC 标签清除，当前非素材页重建，隐藏未保存表单仍参与切换守卫。完成页缩减冗余间距，保留首屏结果。
6. 修正旧布局/文案/守卫断言、准备页按钮、V2 创意单元查询、SOURCE outbox 精确身份及 VideoCreative 编译夹具。管理员 DSH 正向断言回到管理员用例。冗余 AI 源码拼写检查由既有真实 MANUAL/AUTO 服务回归覆盖。GitHub pytest 与本地门禁同样排除已明确退役的 mcp marker，不排除有效业务用例。

DRY record:
- scope：查询类型、provider 恢复、素材 BC 生命周期与 CI 夹具。
- searched：现有 SQLModel col/SQLAlchemy Session 用法，providers tasks/link_steps/Wangyan，drafts/manual_links，WorkspaceTabs/MaterialsPage，V2 编译与封面 outbox owner。
- reused：现有类型视图、Pydantic 边界、TERMINAL/STOPPED_STAGES、原任务和 outbox、页面本地上传守卫、VideoCreative。
- remaining duplication：Python 状态判断与 SQL 筛选共处同一 owner 并共享常量；测试保留独立行为预期。

## 动态证据

所有数据均为独立本地 PostgreSQL18/Redis7 合成数据；环境不加载生产配置，不调用真实广告或版权方服务。

- 当前 main 合入后类型检查：`337 diagnostics` → 两树 `All checks passed!`。两树 `ruff check app tests` 通过。
- `scripts/verify-sqlmodel-type-boundaries.py ae923c8`（PR7）及 `00adb938`（PR8）：`col identity preserved; 21 datetime fields unchanged; migration AST unchanged`。机械脚本再次运行零改动。
- 原前端完整 workspace：`750 passed / 2 failed`，两失败均为普通用户错误期待管理员入口；修后仅补跑两 viewport 及管理员正向用例：`3 passed (4.0s)`。复用其余750项结果，没有重复全量。此前18项定向及新增跨 BC 隐藏脏标签回归通过。
- 真实本地 API browser 原失败场景：`1 passed (2.5m)`，4 Campaign / 12 Ad Group / 24 Ad，failed/unknown 均0；原准备页错误按钮已越过并完成执行。
- 原后端两剧/王眼反例：`2 failed, 2 passed`；R2 最初六例均因 SOURCE/BUILD 同名事件查询歧义失败。修事件身份后暴露旧 dict 创意夹具，改用 VideoCreative 后六参数均通过（首参数在5项检查中通过，其余在后续13项检查中通过）。一次 ffmpeg 15秒 setup 超时在原配置重跑通过，没有扩大 timeout。
- 恢复/AI 聚焦轮：39项通过；SDK、日志 planning/daily、AI MANUAL/AUTO、invalid 恢复等 owner 回归：`137 passed, 36 deselected`（退役 MCP）。旧长 batch 轮因已加载过时 fixture/歧义逻辑中断，12通过与1失败保留，不算整轮通过。
- PR7 实际导出/查询/公式：`91 passed in 226.72s`。PR8 独立库历史日志/重建：`14 passed in 3.69s`。
- M1 有限验证：5000 实际 Decimal 输入、27快照状态及负控通过。M2：78732 转换及5个负控通过。模型不是全系统证明。

复现入口（从 backend/，沿用独立测试环境）：

```sh
python -m pytest tests/modules/business/test_export_integrity.py tests/modules/business/test_queries.py tests/modules/business/test_exports.py tests/modules/business/test_calculation.py -q --tb=short
python -m pytest tests/modules/operation_logs/test_historical_evidence.py tests/modules/business/test_historical_log_rebuild.py -q --tb=short
python -m pytest tests/modules/builds/test_manual_links.py::test_manual_override_does_not_wait_for_reused_unknown_provider tests/modules/providers/test_preparation_tasks.py::test_watchdog_does_not_republish_stopped_unknown -q --tb=short
```

## 独立审查与修复

R1：Standards PASS，发现 P2 人工状态仍被 watchdog 重投；Spec BLOCK，发现 P1 人工补链被旧未知任务阻塞。主线补真实 PG 回归，先得到：

```text
3 failed, 1 passed in 8.28s
```

修复后：

```text
5 passed, 1 warning in 22.16s
13 passed, 1 deselected, 5 warnings in 65.21s
```

前一轮包含两种媒体人工覆盖、两种停止阶段及 R2 首参数；后一轮包含正常恢复的正向对照、双媒体状态与 R2 其余参数。保留既有 SQLAlchemy 表循环警告，不将其当新产品失败。

R2：独立 Standards PASS、Spec PASS，无 P0/P1；旧 P1/P2 已关闭。跨页混合输入由静态控制流核对，未新增专门动态回放。108个公共修复文件在两树逐字节一致，原M1/M2独有产品差异保留。

## 交付边界

本记录证明本地实现及所列检查。没有重跑全仓后端、Linux镜像或远端新CI，没有部署、生产BC数据对账、真实平台投放或远程审批。两份PR的远端head在明确推送前保持原值；源分支使用普通合入，不重写已有历史，不强推。

## 推送后的 frontend CI 回调修复

- 首次推送 head 为 PR7 `5085d1be`、PR8 `b5ff9bb3`。两边 acceptance、campaign-recovery、other-recovery、browser、image 已成功；取证时 modules 仍运行，frontend 均因登录反馈用例失败。PR8 图片预览另有重试波动。
- 远端日志：[PR7 frontend](https://github.com/yaotingfeng/tiktok-ad-automation/actions/runs/43574892392/job/132386907608)、[PR8 frontend](https://github.com/yaotingfeng/tiktok-ad-automation/actions/runs/43574892353/job/132386907399)。本地相同生产预览入口复现 `1 failed / 1 passed`；不能把此前本地通过当作当前 CI 通过。
- 登录问题位于测试观察边界：真实跨站导航挂起后，新的文档读取或 JSHandle 取值会等待新上下文。提前观察/卸载快照虽然可过，却不能拒绝原来的 assign 返回后 finally 过早恢复按钮；该试验方案已撤回。
- 最终复用既有 `setAssignLocation` 导航运输边界，仅延迟真正离开。真实 hook、签发、URL校验、assign 返回后的 finally 均继续执行；先核对实际导航 href，再断言禁用、busy、完整反馈及单次签发，然后转发原生导航核对最终 URL。用例明确归入 `workspace-source`，GitHub 与本地门禁同样运行 `workspace` 和 `workspace-source`，不把场景漏出 CI。
- 图片用例在 HTTP 边界提供有效 PNG，增加 `naturalWidth=1`；原尺寸、src、无 video 断言保留，避免不存在的预览响应触发 onError 后移除图片造成波动。
- 正向：两个相关用例 `2 passed (7.1s)`；新增 CI 输入的整个源码项目 `40 passed (17.3s)`，无重试。复用此前普通 workspace 751项成功的远端结果，不重复全量。
- 真实负控：仅把 hook 中 assign 后的 `navigating=true` 改 false，让 finally 恢复按钮；最终测试在 `toBeDisabled` 处失败，收到 enabled。源码逐字节恢复，交付不包含组件修改。对应旧需求为 [素材登录加载反馈记录](2026-10-21-material-login-loading.md)。
- 测试专项 R3：Standards PASS、Spec PASS，零 finding。负控后仅将 route.fulfill 格式化多4行；最终40项运行使用格式化后的当前逻辑。未扩大超时、增加重试或放宽原行为断言。
- 本地日志：`/tmp/tkada-frontend-navigation-boundary-green.log`、`/tmp/tkada-navigation-boundary-negative.log`、`/tmp/tkada-workspace-source-ci-green.log`。本节只证明所列本地修复与已有远端结果；下一 head 的远端 CI 等待实际运行。


## 2026-11-14：modules 长任务诊断与分组接入

- 远程旧轮 pytest 从 08:16:02 UTC 到 10:38:59 UTC，约 143 分钟；7731 项普通用例串行执行。最新轮前端及其余后端组已通过，modules 仍运行，不能宣称整个 CI 已通过。
- 第一处失败可本地复现：`test_username_migration` 的 Alembic check 将 0116 明确保全的旧 `ad_material_reference.creative_ids` 与约束当作待删除结构。仅从自动比较排除该表的两个精确旧名称，不删除列、不改写历史迁移。原测试先失败（AutogenerateDiffsDetected），修正后通过。
- 旧轮 PostgreSQL 三次报 `out of shared memory` / `max_locks_per_transaction`，其中一次来自 4999 条账户权限写入。CI 临时实例将锁池设为 1024；5000 账户原测试在同配置的本地 PG18 通过。该错误只解释部分失败，不能解释全部后续错误。
- modules 接入现有 `deploy/ci/backend_tests.py` 四组执行器，各自独立数据库和 Redis 库号，保留全部普通测试及原有 retired MCP 排除；acceptance 仍由原来的三个独立组执行。未使用 `--fast` 或首错即停。新一轮实际耗时及是否全绿待 GitHub 结果。
- 原执行器仅依逐项结果，可能漏判会话结束阶段的异常；现检查子进程退出码，异常退出不能被全绿 call 记录覆盖，复跑进程非零也不能转绿。失败 report 立即写入异常详情，CI 始终上传分组日志、逐项结果和耗时。
- 原执行计划测试在两万行预热时记录约十六万条嵌套计划，导致服务日志下载超过 800 MB；现在仍执行原预热，只在原断言针对的单对象发布阶段开启记录。此项是日志体积修正，不将其声称为两小时执行的完整根因。

验证：

- `tests/api/routes/test_username_migration.py` + `tests/modules/accounts/capabilities/test_directory_revision.py`：3 passed，25.44 秒（PG18 / Redis7 隔离实例）。本地备份客户端用该 PG18 容器内同版工具，经临时 wrapper 调用。
- 真实执行器两组探针（目录缓存计划、迁移图）：2 passed，约 0.1 分钟，逐项结果确认两个用例均执行。缓存计划测试原有行数、扫描上界、准备语句和事务锁断言全部保留。
- 真实 pytest sessionfinish 故障探针：1 个用例通过后强制退出 1，执行器仍返回 1 并记录“进程失败”，验证不能假绿。
- `python3 deploy/ci/test_backend_tests.py`：12 tests OK；YAML 解析、全部 workflow shell 语法及修改 Python 的编译检查通过。
- 未重跑完整 modules；旧轮最终没有 pytest summary 的原因仍为 Unknown。共有数据库的大输入夹具可能影响后续测试，尚无具体失败归因，本轮不据此扩大改动。


## 2026-11-14：CI 按测试文件恢复干净数据库

- modules 的四组执行继续保留全部收集结果；`--isolate-files` 在文件 teardown 后从既有独立迁移模板重建本组测试数据库，避免已提交的跨租户队列被后续测试重复处理。保留 Redis 原逻辑。
- 证据：CI 的投影任务原期望 32 桶却处理 1157 桶；独立干净库中同组查询/投影/阶段用例 18 passed，仅归档密钥夹具一项失败（另行修复）。原测试断言不放宽。
- 黑盒：首文件提交标记表，次文件要求其不存在；无隔离时 1 failed / 1 passed，实际插件开启后 2 passed / 5.67s。13 项 runner 检查通过；独立 Standards / Spec 第1轮均 PASS。
- 逐项结果同时保留既有性能用例的 record_property 数据，以免仅剩总体耗时。完整远程 modules 尚未通过，产品名称与素材发布性能问题仍在独立修复，不把隔离视为全量修复。


## 2026-11-14：结构化 modules 失败的当前合同修复

- PR8 `2eb967d` 的分组产物可逐项定位失败：271 条身份目录错误集中在共用夹具缺分页完整性与真实凭据代次；保持产品校验，补齐真实目录事实。执行/重发/封面回归 27 项通过，preview 文件 49 项通过（旧批量 cap 用例由后续修复覆盖）。
- 历史迁移数据按当时实际列播种，不调用今日派发 ORM；保留不可逆退休合同，将旧往返与今日读取分开。V2 独立 SQL oracle、真实冻结源、成熟期固定日期、SDK 物理额度桶和归档加密夹具均对齐已批准合同。42 个不同针对性用例通过；Standards/Spec 第1轮均 PASS。
- 名称投影用真正多行 SQL，保留锁、版本、ORM 身份与事务回滚；23 项目录验证通过，6001 条两档约 15 秒，1600 条约 1.2 秒。提交 `c5ddf116`。
- 路由复用同一次核验后的 BC binding，避免弱身份映射导致重复 SELECT；直接提交仍满足原 25 SQL 上限。提交 `8c29a449`。后台池对已就绪公平赢家保留下一释放槽位，保留全空闲借用，提交 `8646c852`。
- 只读发布探针拒绝未知迁移版本；已知后继仍检查 V1 未完成事实，提交 `a28d40fa`。未发布。
- 7 个独立剩余失败已针对转绿：24 passed / 3 failed 的初轮由后续 3 passed（7.94 秒）覆盖；路由相关 25 passed（4.49 秒）。Carousel 使用完整场景/Minis 事实并按真实 due_at 驱动，报表跨请求测试实际消费 Redis 规划/刷新任务，子范围分别验证继承年龄与物理接口轮转。该 scope 两轴第1轮均 PASS。
- 慢账户测试仅第一轮使用紧片制造原 SQL 超时，后续恢复生产默认 3 秒，保持五分钟延期和其他账户推进的全部断言：1 passed。
- 基准 seed 原来误用普通分组库；现只新建原白名单专用库并在退出时删掉己创建的库，原 5432/localhost/库名 guard 不变。10 个安全/金额 oracle 检查通过；real-seed 本地未运行，因为本机 5432 已有用户 PostgreSQL，未触碰该实例，等待 GitHub 原生 5432 服务验证。
- 后端 Ruff、ty 均通过。完整远程 CI 仍未全绿；40k 素材发布的错误执行计划仍在独立验证，不将局部试验写成最终修复。


## 2026-11-14：40k 发布计划与后续回放修复

- 冷表计划使逐 VID 查询与外键检查反复扫描同账户历史。局部函数/GUC 实验未通过完整门槛，未交付。最终保留所有原触发器及迁移，以有界逻辑使用组发布素材、更新真实统计并重规划；大实体段更新 ad_object 统计，小目录不扫描共享统计。没有新增迁移、配置或业务开关。
- 同一 `(ad_remote_id, platform_material_id, material_type)` 的所有精确/缺省候选必须同批。Spec R1 的 P2（UUID 切段会让原样重复发布额外更换证据版本）已真实先红后绿修复：版本稳定、歧义与后二批回滚 3 项通过。批次按最多 2000 个使用组推进，单个组不拆开。
- 最终源码对应验证：三个目录/预算文件 55 passed /82.86 秒，原 12k 实体+40k 素材首次 29.895 秒、重复 28.360 秒，全部原 <45 秒和身份/数量断言保留；剩余 deadline 文件 3 passed /2.26 秒。后端 Ruff、ty 均通过。
- 实际新增 ShareUpdateExclusive 统计锁与普通业务 DML 兼容；多个大型发布及 VACUUM/ANALYZE 可能互相等待，20 秒 lock_timeout 仍通过现有可恢复重试处理。未验收多租户同时 40k，未部署。
- 327 个旧失败逐项回放得到 318 passed、1 skipped、3 failed、5 setup errors。已覆盖的旧广告总量 cap 用例按现行规格重写；专用端口 real-seed 单列待远程 CI。回放发现的 V2 10k 容量、IMAGE 冻结夹具、全进程历史 RSS 三组问题已修复，独立串联 7 passed /99.28 秒；实际活动窗口 0.188 秒、候选查询 0.106 秒、10 个活动组合，独立 profile 的 34,558,290 字节输入耗时 2.168 秒，RSS 674,119,680 字节，保留 18 秒/800 MiB。两轴审查第1轮均 PASS。
- 旧失败回放另定位目录候选在两轮之间重复扫描未获选的 22.5k 运行；保留 31k 访问上限继续修复，完整远程 CI 尚未通过。
