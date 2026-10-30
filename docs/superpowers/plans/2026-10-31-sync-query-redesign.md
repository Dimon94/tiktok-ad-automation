# TK-ADA 同步与报表查询整体优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 同时降低页面查询和同步成本，在保留金额、授权、素材身份及冻结操作正确性的前提下持续消化积压。

**Architecture:** 保留官方SDK、PG事实表、Redis/Celery、既有执行/分页/outbox，集中新增scope需求与到期调度合同；将HTTP移出业务事务。以窄当前投影和增量日模型支持稳定查询快照，单飞复用、页内补全，统一验收混合负载。

**Tech Stack:** 项目当前锁定的Python/FastAPI/SQLModel/PostgreSQL/Redis/Celery、React/TanStack Query；不新增平台HTTP客户端或分析数据库。

**Spec:** [总体方案](../specs/2026-10-31-sync-query-redesign.md)（必须与本计划一起读）；[巨量对照证据](../../validation/2026-10-31-oceanengine-comparison.md)；[连山 RDS 源表设计对照](../../validation/2026-10-31-lianshan-rds-schema-review.md)。

**状态：已获用户授权，在独立 worktree 实施与验收中，尚未具备上线结论。** 实施基线 `4656997e`，工作树 `.worktrees/sync-query-redesign`。用户要求先实现、压测、自检，完成后再提交上线确认；不得合并 main、推送或发布。本表的完整阶段验收仍按原标准执行，不把针对性单例通过等同整阶段完成。实际证据见[实施验收记录](../../validation/sync-query-redesign-acceptance.md)。

## 实施状态（以此区分代码完成与整阶段验收）

| 工作包 | 已实现 | 尚未关闭 |
|---|---|---|
| W0 | 固定夹具、查询/真实提交计时、旧版基线、参考生产只读核验 | 千万全量、源RDS实时DDL不可达、完整混合λ/μ |
| W1 | 统一scope/需求/执行围栏、迁移及旧任务映射 | 最终全部迁移与当前main集成复验 |
| W2–W3 | 窄投影、版本化dirty、单飞快照、分页与前端水位 | 关键混合查询仍有超过2s样本、真实HTTP与生产规模 |
| W4–W7 | 到期头、HTTP外事务、物理调用成本、公平共享池、请求/分页恢复 | 真实Celery及24小时服务率、完整最大桶包络 |
| W8 | V2消息寿命、90天摘要持久归档、原始小时金额保留 | 独立审查3项缺口修复与最终回归、归档服务率 |
| W9 | 迁移/回填/切换CLI、隔离PG/Redis正确性与局部压力 | 候选提交、合入main的既有修复、全规模与上线确认 |

下方复选项是完整验收条件，未勾选不代表该包代码尚未编写；只有其全部约束有证据才关闭。不得把本地针对性通过改写为生产积压已解决。

## Global Constraints

- tenant、BC、connection、binding generation、channel、actor授权语义不得在需求归并、缓存、发布、管理或导出中丢失。
- priority、lane、账户打包方式不进入稳定scope身份；新需求不能扩大已经开始的冻结查询。
- 唯一发送准入沿用 `platform_quotas.py → admission_policy → admission.lua → gateway`；readiness不扣平台次数。
- HTTP期间业务session/行锁/事务为0；发送前和回写分别验证授权/围栏，已发送请求不能假装撤回。
- 完整事实集合替换、分页/漂移、币种/时区、null/0/unsupported、轮播不要求VID、历史金额不丢不双算。
- 查询保留成员/顺序/refs冻结与序号cursor；首期summary截至T0、页面指标最新，显式标时点，不宣称同版。
- 所有表结构通过新Alembic迁移；不改历史迁移，编号从实施时实际head继续。禁止长期双调度/双事实写入。
- 不自动合并、推送或发布。用户已授权本地实施；生产选择全停旧消费者的整体切换，禁止新旧调度同时运行，最终上线仍须用户确认，遵守用户按需备份规则。
- 仅用真实隔离PG/Redis做并发和恢复测试，TikTok在transport边界替身；不能使用巨量默认本地环境跑测试（它直连测试业务库）。

## Review Focus

1. 同scope执行途中新增更高新鲜度需求：旧执行完成不能清掉后继，归W4/W5测试。
2. 跨账户、跨类型、历史帖子归属晚补：金额不重复、不丢失、不越授权，归W2/W3测试。
3. 租约失效、撤权、DB提交与broker ACK任一点崩溃：旧代不发布、有效需求仍可恢复，归W5/W6/W9测试。
4. 快照建立后更名、修订金额或撤权：冻结成员/顺序不变，授权即时复核，时点说明正确，归W3测试。
5. 巨大单日、10倍终态历史、平台长冷却和低流量租户：成本有界、无饥饿、清理不破坏恢复，归W0/W6/W8测试。

## 总依赖与交付方式

```text
W0 基线与最大桶决策
 └─W1 共享合同/结构/版本
    ├─W2 窄读模型与增量维护 → W3 查询/前端
    └─W4 合并需求与到期头 → W5 无跨HTTP事务/有界发布
                              └─W6 额度感知与公平派发 → W7 刷新与请求缩减
W2/W3/W5/W6/W7 → W8 生命周期治理 → W9 迁移演练和整体验收
```

W2与W4可独立开发，接口/迁移由集成人统一协调。每包聚焦提交、针对性验证；同一候选分支全部包集成后做整体门禁。可以先切查询读路径、后切同步owner，但没有全部达标不能宣布整体解决。

具体完成证据登记到 `docs/implementation-progress.md` 与新验收记录，记录真实SHA、测试命令、性能trace版本、未完成门槛；不可提前写“已提升”。

## W0：把基线和可实现容量算清

**Files**
- Create: `backend/app/modules/reporting/query_diagnostics.py`
- Modify: `backend/app/modules/reporting/queries.py`、`material_query.py`、`job_tasks.py`、`backend/app/core/diagnostics.py`
- Create: `backend/tests/performance/sync_query_fixture.py`、`test_sync_query_baseline.py`、`tests/modules/reporting/test_query_diagnostics.py`
- Create: `docs/validation/2026-10-31-sync-query-baseline.md`（执行时另注明实测日期）

**Interfaces**
- `QueryTrace.stage(name: str)`；`QueryTrace.count(name: str, value: int)`；输出匿名fingerprint、scope规模、阶段毫秒和成功/取消。
- fixture manifest固定seed、行数/宽度/分布、日期/币种/时区/授权、查询trace、HTTP响应trace和环境预算；后续所有包沿用。

- [ ] 写 `test_query_trace_redacts_scope_and_reports_cancel`：日志没有租户/账户原值、Token、报表正文；取消与成功分开；父子inclusive标注。
- [ ] 运行上述新测试，确认新计时合同未实现而失败；实现最小观测，重新通过，不改变业务查询/发送行为。
- [ ] 生成小规模校验集，再生成当前千万目录/引用规模；固定50页身份同时放大无关历史10倍，准备1/7/31/90日、六维度、include_empty和1/5/20会话矩阵；冻结账户数、事实数、匹配行、搜索选择性、冷热及硬件包络，20个不同冷查询与20个相同单飞请求分开验收，明确超界查询完成时间的单独口径。
- [ ] 隔离环境运行旧版纯读、纯同步、混合负载；记录P50/P95/P99、buffers/temp/WAL、RSS、pool wait、λ/μ和请求产出。生产仅做已有授权范围的限时只读采样。
- [ ] 交付平台维度/自然身份/数据库唯一键/展示身份矩阵，核实素材类型是否属于唯一身份及类型纠正语义；列出实际DDL、索引与分区大小。对照连山RDS源表而非只看巨量本地模型，不将源游标误作TikTok已有能力。
- [ ] 在同数据上比较月/日分区、账户/日期前导索引及页内素材反查，记录读写/规划/空间取舍；计算投影回填、索引与WAL峰值。若须改变默认月分区或事实唯一键，先修订spec再进入W1。
- [ ] 测最大账户日/目录分区的暂存→set-based发布：若无法满足spec的10秒最大桶预算，在W1前把该publisher改为dataset_generation+pointer设计并补规格；不得带“以后再说”的超大桶进入DDL。
- [ ] 核实真实app额度及例外、其他共享调用者、合法QPS/QPM/QPD与总连接容量；计算现有新鲜度所需最低请求率。需求超额度则在方案评审中明确申请额度或减少哪些冗余刷新，不默默缩覆盖。
- [ ] 提交 `perf: establish sync and query mixed-load baseline`。验收不是“有日志”，而是可重复运行的基线和最大桶/额度决策已经落地。

## W1：共享模型、版本与围栏

**Files**
- Create: `backend/app/modules/sync_control/{models.py,contracts.py,scopes.py,watermarks.py}`
- Modify: `backend/app/models.py`、`modules/reporting/store_models.py`、`modules/ads/sync_models.py`、`jobs/models.py`
- Create: 两组审查后的新Alembic迁移（控制合同；查询投影/索引），编号由当前head确定。
- Create: `backend/tests/modules/sync_control/test_contracts.py`、`test_migration.py`

**Interfaces**
- `ScopeKey`、`BucketKey`、`RequirementRevision`、`ExecutionFence`、`WaitReason`、`PublicationVersion`为不可变typed合同；日期使用账户当地date、观测时间使用UTC aware datetime。
- `scope_key(route, actor_id, advertiser_id, resource, normalized_query, metric_contract) -> ScopeKey`。
- `compare_publish_version(stored, candidate) -> bool`；generation围栏与观察水位分开，不能拿租约代数当源事实版本。
- `sync_execution_scope`保存execution类型/ID、scope/bucket、requirement ID与冻结revision、子桶发布状态/版本；数据库约束确保执行类型与既有对象引用有效，禁止无验证多态孤儿引用。

- [ ] 写身份测试：只改priority/batch产生相同scope；改tenant/BC/绑定代数/actor语义/时区/类型产生不同scope；缺必要字段失败关闭。
- [ ] 写migration测试：0069之前的真实旧合同样本、运行中页与outbox可读；唯一约束/索引/非空边界正确；空库upgrade不代替旧数据迁移。
- [ ] 将W0粒度/物理设计矩阵落实为审查后的DDL，分别定义业务日期、可缺失源更新时间、观测时间与发布版本。验证ID/类型纠正不产生重复金额，租户/账户约束完整，未验证的类型/精度缩减不进入迁移。
- [ ] 实现最小四张控制表及既有执行围栏字段，创建spec§5的投影表/dirty bucket与索引，保证模型一致；不启动新调度。
- [ ] 运行 `uv run pytest tests/modules/sync_control/test_contracts.py tests/modules/sync_control/test_migration.py -q`（backend目录、显式隔离环境）并执行 `alembic check`。
- [ ] 提交 `sync: define unified demand and publication contracts`；交付无默认生产行为变化的模型合同。

## W2：可重建的窄读模型与页内身份索引

**Files**
- Create: `backend/app/modules/reporting/read_models/{models.py,projector.py,dirty_buckets.py,material_identity.py}`
- Modify: `modules/reporting/publish.py`、`material_links.py`、`modules/ads/sync.py`、命名投影实际发布入口
- Create: `backend/tests/modules/reporting/test_read_model_projection.py`、`test_read_model_rebuild.py`

**Interfaces**
- `mark_dirty(session, keys: Sequence[ReadBucketKey], source_version: PublicationVersion) -> None`，与源变更同事务；幂等合并同桶。
- `project_bucket(session, key: ReadBucketKey, expected_version: PublicationVersion) -> ProjectionResult`，原子替换并推进派生水位；若源变了则保留后继dirty。
- `hydrate_material_identities(session, scope, row_keys, date_range) -> PageMaterialIdentityMap`：只取所选页，但包含授权范围内全部有消耗历史帖子。

- [ ] 写RED用例：迟到收入替换、帖子从未知到视频、同MID不同类型、轮播、视频跨账户多帖；断言每笔事实金额恰好一次，未归属仍有行。
- [ ] 写回填/发布交错测试：旧回填不能盖新发布；同bucket重建重复执行等价；新dirty不因旧重建完成消失；币种/时区/partial/null等价。
- [ ] 实现ad_entity_read、campaign_name_current、current_ad_material；复用campaign日汇总，新增adgroup/material展示身份日模型；宽configuration不进入列表投影。
- [ ] 先将所有仍运行的旧/新源事实、目录、link和命名入口接入dirty/同事务版本更新，再开始回填。重建冻结N只确认N，N+1不得清除；按当前业务latest和歧义规则投影，不删除历史证据。派生落后时读同一旧完整桶，不混新事实与旧归属。
- [ ] 运行新测试及既有 `test_dimension_query.py`、`test_material_query.py`、`test_typed_material_linkage.py`；执行小/大数据定向EXPLAIN ANALYZE，证明当前页无关历史放大后的工作量。
- [ ] 提交 `reporting: maintain narrow incremental query projections`；验收包含可重建、迟到修正和金额等价，不只验证插入成功。

## W3：查询单飞复用、页内补全和前端刷新合同

**Files**
- Create: `backend/app/modules/reporting/query_reuse.py`
- Modify: `queries.py`、`row_set.py`、`dimension_query.py`、`material_query.py`、`query_models.py`、`schemas.py`、`api.py`
- Modify: `frontend/src/features/ads/{useAdsQuery.ts,AdsWorkspace.tsx,AdsSummary.tsx,ReportQueryStatus.tsx}`，重新生成client
- Create: `backend/tests/modules/reporting/test_query_reuse.py`、`test_query_page_cost.py`；扩展 `test_queries.py` 与前端ads报表测试

**Interfaces**
- `QueryDependencyStamp(scope_digest, permission_revision, filter_digest, fact_buckets_digest, derived_buckets_digest, directory_revision, naming_revision, link_revision, metric_contract)`。
- `find_or_build_snapshot(session_factory, context, filters, dependency_stamp, cancellation) -> QuerySnapshot`；actor隔离，锁后再查，lease/token防旧构建者覆盖。
- 响应新增 `snapshot_at`、`metrics_observed_at`、`ordering_basis="snapshot"`；不修改旧cursor所冻结成员，新增字段通过统一schema/client生成。

- [ ] 写20并发同actor同stamp仅构建一次、持锁者死亡后接管、旧完成不能替换新结果测试；另一actor/授权scope不共享token；最终stamp来自构建RR内实际数据版本，不用事务外旧标签标记新结果。
- [ ] 写快照后发布/改名/解绑/撤权测试：冻结成员/refs和summary保持T0；页状态最新；撤权仍403；不按最新目录重新解释全选。
- [ ] 写50素材行成本测试，10倍无关事实不扩大页内映射集合；历史帖、类型及授权完整聚合。
- [ ] 使用W2读模型缩小构建与hydrate；保留SQL精确筛选/排序及稳定sequence，不用提前LIMIT漏掉匹配行。
- [ ] 实现同actor复用、60秒空闲首屏重建上限、交互不重建、新数据提示、隐藏页不轮询；联通取消但不能取消其他等待者的单飞任务。
- [ ] 运行后端查询/冻结选择合同及前端多标签页/取消/筛选/全选测试，构建并实测首屏/翻页SLO。若冷首屏未达标继续分析，不以202/加载中响应时间代替完成时间。
- [ ] 提交 `reporting: reuse stable snapshots and bound page hydration`。

## W4：统一需求归并与到期头，先影子运行

**Files**
- Create: `backend/app/modules/sync_control/{requirements.py,schedule.py,shadow.py}`
- Modify: `modules/reporting/planner.py`、`planning_steps.py`、`link_work.py`、`modules/ads/directory_candidates.py`及目录需求入口
- Create: `backend/tests/modules/sync_control/test_requirements.py`、`test_shadow_equivalence.py`

**Interfaces**
- `merge_requirement(session, scope: ScopeKey, bucket: BucketKey, requested_after, deadline, source) -> RequirementRevision`。
- `freeze_execution_scope(session, execution_ref, requirements: Sequence[RequirementRevision]) -> ExecutionFence`。
- `satisfy_requirements(session, fence, publication) -> PendingSuccessor`，只能满足该执行冻结且真实覆盖的revision。

- [ ] 写同义跨tier合并、批量拆分/重组不重复需求、不同授权不合并、执行中新增日期/更晚刷新保留后继测试。
- [ ] 写priority提升不重置checkpoint、覆盖缺口不重拉新鲜金额、轮播/视频分别归并测试。
- [ ] 实现三类需求来源写同一合同，既有任务仍执行；shadow只比较需求/到期/批次，不写业务覆盖、不发平台请求。
- [ ] 用原旧任务样本形成old→scope→requirement映射，逐项核对范围/冻结路由/页证据及未满足计数；不能按任务数量相等证明需求等价。
- [ ] 运行新测试与既有planner/目录公平性回归；提交 `sync: coalesce requirements into bounded schedule heads`。

## W5：HTTP移出事务，保存和发布有围栏

**Files**
- Create: `backend/app/modules/sync_control/{executions.py,recovery.py}`
- Modify: `modules/reporting/jobs.py`、`job_tasks.py`、`checkpoints.py`、`publish.py`、`balance_tasks.py`、`modules/ads/tasks.py`、`sync.py`
- Create: `backend/tests/modules/sync_control/test_execution_boundaries.py`、`test_publish_fencing.py`、`test_large_snapshot.py`

**Interfaces**
- `claim_execution(session, execution_ref, now) -> ExecutionEnvelope`；含fence、冻结query、checkpoint和下一物理endpoint。
- `fetch_page(envelope, gateway) -> ValidatedPage`，函数不接收Session；授权核验采用独立短读。
- `stage_page(session, fence, page) -> NextStep`；`publish_bucket(session, fence, bucket) -> PublicationResult`。

- [ ] transport替身阻塞HTTP时，用另一PG连接断言该执行不持业务事务/行锁；发送前后撤权、超时、失租晚回包都不能越权发布。
- [ ] 对领取提交、HTTP后、暂存提交、发布提交、outbox发送/ACK各边界注入崩溃，验证重放幂等和需求可恢复。
- [ ] 写旧快照晚发布、新快照已发布、两类报表对账版本、漂移epoch、完整集合删除旧行测试；多日发布一半崩溃，仅恢复未发布桶，已发布子桶不满足后继需求，父执行不提前COMPLETE。
- [ ] 将report、directory、balance adapter接到三段执行，去除跨HTTP的旧事务锁；由fence和发布CAS保护。实现规范化暂存/流式校验，不在末页加载所有JSON。
- [ ] 运行真实PG/Redis恢复和最大桶压力回归；确认发布原子性、RSS上界及spec时长门槛。
- [ ] 提交 `sync: fence page execution outside database transactions`。

## W6：接口就绪感知、公平调度与可靠唤醒

**Files**
- Create: `backend/app/modules/sync_control/{readiness.py,fairness.py,dispatcher.py,tasks.py}`
- Modify: `backend/app/jobs/admission.py`、`admission.lua`、`modules/reporting/job_tasks.py`、`modules/ads/tasks.py`、`jobs/celery_app.py`
- Create: `backend/tests/modules/sync_control/test_endpoint_dispatch.py`、`test_fairness.py`、`test_dispatch_recovery.py`

**Interfaces**
- `endpoint_readiness(redis, policy, endpoint_scope, now) -> EndpointReadiness(next_eligible_at, capacity_hint, reason)`，只观察；发送计数只在原admit Lua消费。
- `dispatch_due(session, redis, now, physical_slots) -> DispatchBatch`：有界领取，tenant→workclass公平；只发owner epoch允许的任务。原子本机dispatch occupancy覆盖queued/leased未发送工作，与actual inflight交接，不扣平台计数；多请求续步取得下一份预算后才能继续。
- `record_step_result(session, fence, outcome, actual_cost) -> None`，更新头/deficit，合并一次唤醒。

- [ ] 写quota满时10000个scope不产生10000个broker重试；Redis readiness过时仅少量发送前竞争拒绝，不双扣、不返还已发送次数；两个Control同时调度也不超发，queued占位取消/失租按generation回收。
- [ ] 写物理alias共桶、独立endpoint不互堵、QPM/QPD/cooldown、Redis恢复/冷启动、新旧key切换测试。
- [ ] 在单endpoint饱和下验证小租户、历史、目录、余额有进展，空闲借用和页边界归还；不因为endpoint冷却卡住别的endpoint。
- [ ] 替换旧逐步全局refill/活跃全集排序入口；保留DB失租/未发outbox恢复，Beat只做合并唤醒和兜底。
- [ ] 同trace比较本地拒绝/真实发送、每页SQL/outbox与μ−λ，满足80%拒绝下降且成功发送不降；提交 `sync: dispatch fairly when endpoint capacity is available`。

## W7：基于证据的刷新分层与响应复用

**Files**
- Create: `backend/app/modules/sync_control/{refresh_policy.py,response_reuse.py}`
- Modify: `modules/reporting/planner.py`、`link_queries.py`、`link_work.py`、`modules/ads`定向刷新入口
- Create: `backend/tests/modules/sync_control/test_refresh_policy.py`、`test_response_reuse.py`

**Interfaces**
- `next_refresh(scope, bucket, change_history, attribution_contract, now) -> RefreshDecision`。
- `ReusableResponseKey(scope, normalized_query, schema_version, observed_at, completeness_digest)`；复用不得跨授权或混页。

- [ ] 写跨日、31天修订、由不活跃变活跃、零金额但有效对象、周期探测和来源变更测试；先保持旧周期结果相同。
- [ ] 按W0实测输出具体频率矩阵与“预计省多少请求、会延迟哪种修正、兜底何时覆盖”，通过评审后才改变周期；不凭固定拍脑袋日龄降频。
- [ ] 合并同查询完整响应供相容需求，正/负缓存完整性与时效分别验证；一份响应不能满足开始观察之后才提出的新鲜度需求。
- [ ] 用同覆盖结果trace验证API请求节省、完整重建率、晚到修正无漏；目标由W0清单冻结，未达标不扩大周期。
- [ ] 提交 `sync: reuse complete reads and schedule evidence-based refreshes`。

## W8：暂存、流水和当前/历史增长治理

**Files**
- Create: `backend/app/modules/sync_control/retention.py`
- Modify: `backend/app/jobs/outbox.py`、`modules/reporting/job_tasks.py`和归档存储适配入口
- Create: `backend/tests/modules/sync_control/test_retention.py`

**Interfaces**
- `retention_candidates(session, policy, cursor, limit) -> RetentionBatch`；`prune_batch(session, batch, now) -> RetentionResult`，执行时再次检查引用/终态/generation。
- V2 dispatch带`valid_until`；幂等摘要、正文和审计归档有不同生命周期，legacy未定期消息不套新TTL。

- [ ] 写活跃超龄页不删、失租晚回包不复活、清理与新增引用竞争、payload清后重投仍幂等、过期消息由DB再派发测试。
- [ ] 扩展现有outbox压缩，不重写另一套；按spec保留期小批删除/归档，只清可证明无引用项。
- [ ] 当前素材投影不影响历史核算；不得自动删除旧素材引用/金额事实；归档失败保持原数据可用。
- [ ] 运行24小时加速时钟和真实并发试验，证明净增长符合保留模型、恢复不依赖已清正文；提交 `sync: bound control history with reference-safe retention`。

## W9：迁移演练、有限切换、混合验收及旧实现退场

**Files**
- Create: `backend/app/modules/sync_control/migration.py`、`backend/tests/modules/sync_control/test_cutover.py`
- Create: `scripts/sync-query-benchmark.py`、`docs/validation/sync-query-redesign-acceptance.md`
- Modify: `docs/runbooks/{deployment.md,sync-performance.md,production-junbo.md}`、`docs/implementation-progress.md`及实际部署并发配置

**Interfaces**
- `map_legacy_execution(session, legacy_ref) -> MigrationDecision`，输出可继承证据/需重建私有页/禁止转换原因。
- `transfer_scope_owner(session, scope, expected_epoch, target_contract) -> OwnerEpoch`，CAS确保单owner；转换后旧消息不能再派业务。

- [ ] 在隔离PG/Redis复制结构与生成旧状态：QUEUED/WAITING/活跃/失租RUNNING/未发outbox/部分页/旧授权，完成迁移及中断恢复。
- [ ] 验证最危险场景：双owner+新旧quota、混epoch页、清理过早、projection旧回填盖新事实、撤权缓存、需求静默丢失；逐项核对业务缺口而不是总任务行数。
- [ ] 跑spec§8完整矩阵，给出前后原始结果/查询计划/环境与容量；全部正确性门槛必须通过，已有未相关失败单独说明不能伪称全绿。
- [ ] 形成具体生产变更单：精确SHA/迁移/实际app级别与策略摘要、每角色pool/Worker预算、磁盘峰值、owner切换、停止/回滚点。提交评审，未获得本次迁移发布授权前不执行。
- [ ] 按批准方案先把owner围栏及源版本/dirty维护准备版本部署到全部旧planner/claim/续页/恢复/publisher并逐角色核验，再回填/投影校验和有限scope单owner切换；不具备准备条件则选择全停旧执行后整体切换，禁止与不识别epoch的0642ad3d混跑。观察3×15分钟后扩展，完成24小时及账户跨日/余额周期验收。任一数据正确性失败停止扩展。
- [ ] 删除旧scheduler/refill与临时迁移分支，保留审计映射与明确恢复工具；再次验证无旧consumer。提交 `sync: complete unified cutover and retire legacy scheduling`。
- [ ] 最终汇报同时列页面P95、有效数据/请求、各类μ−λ/最老年龄、平台拒绝、DB/RSS/增长、剩余限制；全部达标才将整体状态改成完成。

## 自检与方案评审结论

本计划覆盖源能力、同步调度、接口额度、事务/发布、读模型、页面、生命周期、部署与容量。包之间通过W1 typed合同、W0固定trace及单owner迁移连接，不允许各自写一套指标口径。表格里的数值均是待验目标，不是上线收益承诺。

暂不执行的选项：跨actor共享快照、全库MVCC历史读模型、直接换分析数据库、未验证的TikTok推送/RDS/异步报表能力、直接扩大上传并发。若W0证明它们是必须依赖，先更新规格及发布合同，再进入对应任务，不能在实施中隐式扩大架构。
