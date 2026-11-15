# JB-14 原生计划集与独立准备

起点 `4e89c407257b68f44923eb95176fbb71f40df6ae`，main 基线 `b185736753df3a0f6992d9494867ae877eb1b089`。仅指定 `feat/jb13-native-series-m2` 产品树；无 push、PR、部署、Linear 修改或真实平台调用。SDK pin、依赖及部署配置未改变。

## 实现与接口

- `scene_selection` 新写入唯一形状为 `NativeSeriesPlanSet`；`plans` 内 UUID 键唯一且非空。旧平铺输入/持久化 JSON 在读取边界成为固定键的单项，不回填数据库，不改历史冻结 payload。
- 工作区新增、复制、删除、选中计划批量修改地区；每项明确账户、资产、双身份、有序视频和可选封面。复制生成新键；显示批量编辑影响范围，未选项保持原值。新计划 HTTP 输入必须包含精确选择。
- 20 计划为当前任务扇出资源预算，每项沿用最多 1000 来源、每轮 20 素材的 owner；不是运营默认或平台商业上限。
- Workspace/Readiness 使用计划集，素材回执保存在既有 `DraftScenePreparation.native_materials.plans[plan_key]`；逐项复用 SceneJob、distribution、covers，未建立新的上传或资格 owner。
- `GET native-checkpoint?view=plans` 返回 `NativeSeriesBatchCheckpoint`；READY 项携 checkpoint，其他项携 selection 与 reason_codes。原 `native-checkpoint` 增加可选 `plan_key`，缺省仅允许单项。
- 同修订重试复用 preparation，递增 dispatch generation，保留有效证据/游标；输入修改仍走原 revision/obsolete 机制。先完成账户/场景检查，再全批按素材 ID 取锁。
- Preview/Execution 暂通过 `single_native_selection` 消费单项；多项返回 `native_batch_preview_pending`，页面说明批量冻结尚未开放。JB-15/16 迁移消费者，JB-18 移除此适配。没有 `is_batch`、`batch_ready` 或第二可变选择。
- 无 DDL 迁移：使用已有 JSONB 列做 expand 形状，保留旧材料回执读取。没有 downgrade 或历史冻结重写。

## 验证入口与证据

所有 Python 命令从本树 backend 经 `/tmp/tkada-native-series-20261112/m2-implementation/run-test.py`，使用私有 M2 环境、锁定 Python 3.14 依赖和隔离 PG/Redis。未打印私有环境。前端使用既有 Bun。

- 红：新增 `test_plan_set_preserves_explicit_mapping_and_normalizes_legacy` 在旧实现失败 `KeyError: plans`；随后同测试绿。早期一次错误 cwd 导致找不到测试，不计红。
- 初始回归：该测试 + `test_prepared_workspace_freezes_native_plan`：2 passed / 6.78s。
- 混合独立准备、同修订证据保留、旧 revision 与原 M1 浏览器提交：3 passed / 47 deselected / 55.99s。浏览器后继续真实 SDK transport 创建/回读 MATCH；不替换业务 owner。
- 真实计划编辑发现 PATCH 错发 `media_kind` 导致 422，已修复；测试仍通过真实 HTTP 保存。浏览器 BC fixture 改用 M1 的数字 BC；在途 route.fetch 清理属于测试收尾。
- 实际前端正式与 demo 构建曾通过；最终源码检查结果追加下方。
- 错用宿主 Python 3.11 的 compileall 因项目 Python 3.14 语法失败；改用固定 runner 的 compileall 通过。未改项目语法迎合宿主。

## 所有权与接续

本票实现 T1/T2/T5/T6、R1/R2，以及 R4 资格与 R12 准备范围/旧修订部分；T3/T4 的准备结果可观察，但多计划冻结准入由 JB-15 接续。占用冲突、完整冻结、执行、恢复、取消和形式模型分别归后续票。不得把本票当作完整 M2 MMF。

JB-15 应从真实保存的 canonical plans 与按键 readiness 开始；不得直接写 READY，不能静默取首项，必须继续显示不可用项。单项 M1 冻结历史与执行 payload 保持原格式。

## 最终针对性结果

- `pytest tests/modules/builds/test_native_plan_set.py <ready-case> <multiconnection-cover-case> -q --tb=short`：6 passed / 42.44s。包含真实页面三份计划、显式复制与选中两份批量编辑；两账户、两资产映射精确落库；未选第三项地区保持 CA。页面实际 PATCH 保存和 POST prepare，随后真实 continuation 与读取；第一项 READY、越权账户项 BLOCKED、第三项未就绪保持可见。
- 读取接口收敛到既有 `native-checkpoint?view=plans` 后，仅重跑受影响浏览器：1 passed / 3 deselected / 60.43s。
- 并发准备重试：两个独立 PG session 同时请求，复用同 preparation，revision 不变、dispatch generation 只推进一次、旧素材回执保持。旧平铺选择及旧素材 JSON 的只读回读返回单项且原 JSON/冻结 payload 不变。
- 真跑近似错误探针：关闭重复键拒绝 → schema 检查失败；逐项回执只返回第一项 → 混合准备检查失败。均 exit 1，逐文件从外部备份还原并逐字节核对；恢复后两项检查 exit 0。日志位于本机 `m2-implementation/jb14-probe-*.log`。
- 最终 `bun run build`：TypeScript、正式 Vite、demo Vite 全通过。`ruff check` 与固定 Python 3.14 `compileall` 通过。
- TDD 边界：先行红覆盖旧输入缺 canonical plans；PG/浏览器用例按纵切补齐，部分检查在接线后加入，不宣称所有用例均先红后写。上述探针证明关键检查拒绝近似错误。

独立验收仍应关注：两项均有不同账户正资格的组合、有效项与兄弟项仅 TTL 过期的完整异步刷新交错、跨来源/tenant/route 的完整失败矩阵及不同草稿素材锁竞争。本轮真实浏览器第二账户刻意无授权，用于证明 BLOCKED 不吞 READY；不把它当成双账户均可执行的证明。没有真实平台或生产验收，没有形式证明。

最终命令（backend 工作目录，均经固定 runner；前端命令从 frontend）：

```sh
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest tests/modules/builds/test_native_plan_set.py 'tests/modules/builds/scene_jobs/test_native_series.py::test_native_job_exact_asset_complete_streams_and_unverified_combination[ready]' tests/modules/builds/scene_jobs/test_native_series.py::test_native_multiconnection_cover_ambiguity_cannot_reuse_ready_mapping -q --tb=short
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest tests/modules/builds/test_native_plan_set.py -k browser -q --tb=short
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python /tmp/tkada-native-series-20261112/m2-implementation/jb14-probe.py
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest 'tests/modules/builds/scene_jobs/test_native_series.py::test_workspace_prepare_drives_native_job_without_provider[False]' -q --tb=short
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest tests/modules/builds/scene_jobs/test_native_series.py -k 'video_verification_resumes or selected_image_cover' -q --tb=short
bun run build
```

末次素材改动：任务键加 `plan_key`，避免同 preparation/source 的不同计划使用相同调用键；已有 distribution/covers 仍负责真正去重和上传。上述素材专项 2 passed / 26 deselected / 107.09s；空视频旧输入保持明确 BLOCKED，针对性 1 passed / 52.09s。时间为本机真实运行时间，不作为性能基准。

原生调用方回归实际命令：

```sh
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest tests/modules/builds/scene_jobs/test_native_series.py tests/modules/builds/test_native_series_selection.py tests/modules/builds/test_native_preview.py tests/modules/builds/test_native_execution.py -q --tb=short
```

该轮初始源码结果 **92 passed、2 failed / 1338.87s**，不是全绿：①旧断言把过期能力期待为 `account_build_unverified`，与已存在的 M1 TTL 分类 `account_build_evidence_expired` 不符，更新断言保持既有业务语义；②本次准备聚合遗漏 `material_issues`，导致封面多义时父回执 error_code 为空，已补回具体素材原因。两项均包含在上述 **6 passed** 的修复后针对性运行中，没有重新跑整套。其余 92 项包含单项直接 ENABLE、实际回读、UNKNOWN、取消、TTL 与最终发送围栏；配合末次素材任务键专项复验，不声称整仓或所有跨计划交错均已通过。

## 候选补充验收：双授权账户、单项 TTL 与共享素材竞争

本轮起点 `7e908d8edd575f055249faac3a797e264dbe6bee`，分支和唯一写树均按交接保持；原 `.venv`、`node_modules` 未跟踪软链保留。仅新增验收检查和记录，未复现需要修复的新产品缺陷，未修改页面、业务 owner、SDK pin、策略或冻结 connection 合同。Nowledge 定向检索未找到相关历史，采用本树 Spec、完整 JB-14 快照及已有报告作为合同来源。

### 实际路径和独立预期

新增 `test_http_two_authorized_plans_and_expired_sibling` 从实际 Workspace PATCH 保存两份计划、POST prepare、GET native-checkpoint?view=plans 读回。HTTP 使用 FastAPI TestClient/ASGI 运行正式 router、序列化、租户权限和数据库事务；仅认证身份绑定合成操作者，不替换资格/素材/草稿 owner。复用现有 M1 准备夹具作为种子，合成目录提供两个已授权账户和各自已有上传映射；没有写假 Scene、Capability 或 preparation READY。所有资格读取经官方 SDK transport double，实际 `process_preparation` dispatch generation、SceneJob、CapabilityJob、PG 和 Redis owner 生成结果；没有启动外部 Celery broker 消费进程。

独立预期来自 Spec R1/R2/T2/T5：第一账户 `actual-account` / `exact-series` / `owner` 使用素材原顺序；第二账户 `second-account` / `second-series` / `second-owner` 使用反序。同名/同源素材不推导账户映射。逐键比较选择、冻结路由、VID、封面和素材顺序，两个 Scene evidence 集不相交，双项最终 READY。

仅令第二项 Scene TTL 过期，第一项完整 checkpoint 保持相同；HTTP 同 revision prepare 复用 preparation，旧 dispatch generation 实际送入 `process_preparation` 无副作用，当前 generation 经异步处理入口刷新后第二项恢复 READY、Scene 证据更新、有序素材映射不变，第一项 checkpoint（包括 scope、证据和时间）逐字段不变。账户能力由 BC 级 CapabilityJob 共享；将共享能力 TTL 过期会同时失效两账户，因此不把该情形宣称为“单账户过期”。旧 revision 隔离复用并定向运行既有 `test_independent_preparation_retry_and_revision`。

`test_plan_scope_and_opposite_order_drafts` 将第二账户真实 VID/封面回执借入第一项，HTTP 读回明确 `native_video_mapping_unverified`，兄弟项不变；跨 tenant HTTP 读取 403；篡改 preparation 冻结 connection 被真实 PG `immutable scene route` 约束拒绝，原路由未变。恢复证据后检查完整回读等于原值。复用原多连接封面歧义已通过证据，不扩展完整 scope 矩阵。

同一测试通过 HTTP 新建两份草稿，计划顺序相反、共享相同素材，各计划自身素材顺序也相反。准备推进到 Scene 阶段后，以 Barrier 同时释放两个独立 PG session（断言不同 backend PID），设 5 秒 lock_timeout / 10 秒 statement_timeout，完成真实 continuation；两份草稿均两项 READY，逐键 VID/封面/顺序与输入一致。超时将直接失败，不吞掉锁错误。

### 锁顺序与边界

`drafts._scenes_page` 先遍历账户 Scene，然后对全批当前页素材按 ID 排序取锁。`scene._account_scope(lock=True)` 实际先独占锁冻结 `TikTokConnection`，随后 `ensure_scene_preparation` 锁 SceneJob；同一 connection 的不同草稿因此在进入素材阶段之前串行。`scene_jobs._lock_job` 同样连接先于 SceneJob，未发现反向锁序。素材侧 `distribution.ensure_target_asset` 与 `covers.ensure_cover` 均素材先于映射/任务；它们调用的 `require_material_route → verify_route` 为读取校验，不在已持有素材锁后另取 connection 行锁。全批 ID 排序后再调用逐项素材 owner 不反转已取得的素材锁序。

该证据只覆盖本票同冻结 connection、两个草稿反向计划/素材输入的竞争；不声称所有跨连接/崩溃/取消交错或形式模型已经通过。多计划冻结仍 `native_batch_preview_pending`，未实施 JB-15。

### 运行记录

从本树 backend 执行，所有 Python/ruff 均经指定 M2 runner；日志仅存外部本机目录 `m2-implementation`。旧 92 pass/2 fail 及修复后6 pass、页面与构建结果沿用前文，不重新跑全套。前端与产品源码未变化，无需重复浏览器/构建。

夹具调试曾出现 detached ORM、共享 Capability TTL 误影响兄弟、浅复制负例误改兄弟 position、不可变 route 约束及 create 缺显式 account_lines；这些均按真实输入/约束修正，不计产品 red 或产品修复。近似错误探针另行记录，不能用这些夹具失败冒充 TDD red。

精确命令（工作目录为本树 `backend`）：

```sh
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest tests/modules/builds/test_native_plan_set.py -k 'scope_and or http_two or independent_preparation' -q --tb=short
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest tests/modules/builds/test_native_plan_set.py -k scope_and -q --tb=short
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python /tmp/tkada-native-series-20261112/m2-implementation/jb14-acceptance-probes.py
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest tests/modules/builds/test_native_plan_set.py -k 'http_two or scope_and' -q --tb=short
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py ruff check tests/modules/builds/test_native_plan_set.py
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py ruff format --check tests/modules/builds/test_native_plan_set.py
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m compileall -q tests/modules/builds/test_native_plan_set.py
```

- 第一条：2 passed / 1 failed / 3 deselected，31.11s（`jb14-final.log`）。双账户/TTL/旧 dispatch 和既有旧 revision 检查通过；第三项当时未把数据库 immutable route 拒绝写成预期异常，不隐去该失败。
- 第二条：修正夹具后 1 passed / 5 deselected，11.49s（`jb14-scope-final.log`），包含真实双 session 并发和范围拒绝。
- 探针：`evaluate_native_plan_set` 临时只返回首项，`http_two` 在 `['READY'] != ['READY', 'READY']` 失败；`read_native_materials.scoped` 临时放过账户范围，`scope_and` 在越界回执 `READY != BLOCKED` 失败。分别 exit 1，详见 `jb14-acceptance-probe-plans.log` / `jb14-acceptance-probe-scope.log`。每次均 finally 恢复源码、字节比较并执行 `cmp`；产品文件最终无 diff。
- Ruff、格式检查和固定 Python 3.14 compileall 通过。

JB-15 可消费合同：同一 Draft canonical `plans`，UUID `plan_key` 稳定；真实保存选择与 `native-checkpoint?view=plans` 按键对应；READY 携带完整 checkpoint，其他项保留选择与原因；素材回执仍为既有 `DraftScenePreparation.native_materials.plans[key]`；共享策略和冻结 route，不增加第二资格 owner。JB-15 必须实现完整 INCLUDED/EXCLUDED 冻结，当前多项冻结仍明确 pending；本轮不解除该限制、不发布、不更新 Linear，正式 frontier 放行归 root。

恢复源码后第四条命令最终 **2 passed / 4 deselected，15.63s**（`jb14-restored-green.log`），双账户/TTL/dispatch 与范围/并发均通过。未解决的本票新产品缺陷：本轮未发现；外部平台、生产、完整矩阵和形式证明仍未验收。
