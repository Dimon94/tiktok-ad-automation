# M4a R1 P1：定向 Ad 事实观测

范围：唯一产品 writer，Codex gpt-6.1-sol/high；指定 feat/jb24-native-growth-m4a 工作树。
start `573ebc8fc2f50d212856c9d8ec23a29645ae3b59`。不切分支、不新建 checkout；保留原 `.venv`/`node_modules` 软链。
仅修 review-r1-spec.md 的 P1；不代表 M4a 放行。

## 初始真实 RED

独立预期来自 R1 反例和 JB24 R9/JB29 冻结实际来源合同。
专用真实 PG/Redis 守卫：
`python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py`（下文记作 guard）。

`guard python -m pytest backend/tests/modules/reporting/test_targeted_provenance.py -xq --tb=short`
初始 1 failed，0.40s：full t0 a1=20/a2=90，target C1 t1 a1=100；新查询金额100，但 BucketStamp.observed_at=t0，断言期望 t1。此为真实 publisher/query/PG 业务 RED，非模型推断。
无旧 full 分支也保留独立断言：native 行应有真实 t1/合同，不能借缺失的账户状态变成 LEGACY_UNKNOWN。

## 修复 owner 与合同

- 沿用 `publish_ad_day` 原账户 advisory lock、暂存校验、精确范围替换与事务，不增同步 owner、表或迁移。完整性 `ReportSyncState` 的 t0/version/digest 保持不变；定向任务仍只满足原范围。
- 在既有 AdReportDay.evidence 内同事务保存该行实际发布坐标：observed_at、published_at、合同、任务 publication_id、payload digest、租户/账户/请求日期/精确 campaign_ids 与范围类别。摘要绑定响应行摘要、币种/时区和精确 scope。原金额只在 Numeric 列，证据不复制金额。
- full 发布仍使用原整账户 publication_version；target 不拥有账户版本，version=0，publication_id + scope + digest 才是其身份，不伪造 V+1。publication_coordinates.scope 明确 CAMPAIGN_LOCAL_DAY（full 为 ACCOUNT_LOCAL_DAY）。
- 普通 Ad 和贡献查询从行证据选择合同/观测；真实无证据旧行不借当前账户时间或状态追认。full 空桶仍可使用账户发布坐标。
- Campaign/Adgroup 的 direct 选择和完整空桶权威不变。无 direct 的 CHILD_DERIVED 在同一 SQL 选择计划内按实际子事实/合同分桶、求和并保留各子观测，绝不把不同 t0/t1 合并成一次整账户刷新。有 native 子证据就保留 native 合同，source_kind 仍为 CHILD_DERIVED。
- SQL 成员、过滤、排序、summary、MetricVector 与 observation 共用选择计划；旧 formatter 的独立账户水位读取已改用同一 `_facts_cte`。
- 原 RR 构建继续冻结向量/provenance。publication_map 保留账户完整性记录，并纳入去重的 SELECTED_FACT 实际发布坐标；逐行 remote_id 留在行 provenance，避免把发布图扩成重复的每行记录。CSV 沿用原复制 owner，详情/翻页/CSV 不重算当前事实。

matched 字段：金额100；来源 PLATFORM_DIRECT 或 CHILD_DERIVED；native 合同；t1；精确 C1 scope；新 payload digest 与 publication_id；未更新 a2 的 t0；账户完整性 t0/V/H；冻结详情/CSV 与原向量及发布图一致。

## 当前验证

- targeted regression 14 cases：有/无 full；Campaign/Adgroup/account/drama × ordinary-derived/contribution；账户多 Campaign t0/t1；SQL阈值/排序/summary；冻结详情/CSV；full Campaign/Adgroup direct 及空发布覆盖 target child。
- `guard python -m pytest backend/tests/modules/reporting/test_targeted_provenance.py backend/tests/modules/reporting/test_native_publication.py backend/tests/modules/reporting/test_native_scope_roas.py backend/tests/modules/reporting/test_native_exports.py -k 'not representative' -xq --tb=short`：134 passed，1 deselected，44.75s。
- 更新旧 source-contract 测试唯一预期：有证据的派生子行不再一律 LEGACY_UNKNOWN；保留 direct/derived 分桶、40与20分离及各自ROAS断言。
- 中途 fixture 缺 `AdContributionScope.kind`、目录 `ad_type` 和误用不存在的阈值字段均已修正；不作为业务 RED。
- `guard ... test_native_levels.py ... -xq`：13 passed 后在已知 root P2 的 `platform_roas_total_reason` NOT_REQUESTED/NO_RETURNED_ROW 断言失败。此前两个 HTTP/SDK/Worker 参数场景通过；未将该套件报告为全绿，未修 P2。
- 源码消融：`python3 .agents/skills/formal-verify-reporting-data/scripts/targeted_probes.py`。分别强制 `to_jsonb(s)`、强制账户合同。两项均业务 AssertionError RED；仓外 TemporaryDirectory backup、finally 恢复、系统 cmp 一致；每次原检查 2 passed GREEN。与所有 PG fixtures 串行。
- 改动文件 Ruff 与 Python3.14 compileall 通过。窄 mypy（4个改动模块，follow-imports=silent）新增错误已修；剩余2条为原有 row_set `unsupported` 注解、publish creative `counts` 注解，未顺手修改。全仓旧410错误不重跑、不宣称全绿。
- 发布图去除重复 remote_id 后针对性重跑 targeted + native_exports，结果记于下方。

复用 source-matching 的 scope/权限、RR 冻结、SDK/browser/build 既有记录；因 SQL 选择和发布图有变化，本轮明确重跑上述窄相关回归。不做真实 TikTok/生产API/广告写、Linear写、push/PR/deploy/生产迁移/main 合并。

## 交回 root

三个 P2 刻意未修：creative 快照局部变量；legacy NULL 的 ABSENT；未请求 ratio 的 NO_RETURNED_ROW。R1 Spec 曾在 ratio P2 处阻断的 native_levels 全路径需 root 修 P2 后 fresh R2 执行。生产/平台语义确认仍 Unknown。最终形式验证同步见独立模型提交及 skill R1 段；其 atomic publication/RR 假设没有扩大为无界证明。

最终小范围复验：发布图去重后 targeted + native_exports **17 passed，5.28s**；compileall、Ruff、git diff --check 通过。新增同类范围断言已覆盖；不再重复全量验证。
