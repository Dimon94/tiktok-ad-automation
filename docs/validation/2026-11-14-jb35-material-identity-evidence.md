# JB35 素材身份与逐事实证据

状态：本地可审候选；待 root 独立阶段验收与完整 MMF review。无平台调用、tracker 写入、生产迁移或发布。
起点：`cf20640cf4fc2c50d67715d02990a535bd2e7a66`；唯一写树与分支 `feat/jb33-native-material-m4b`。保留原 `.venv`、`node_modules` 软链。

## 前置与实际范围

读取正式 JB33/JB35/JB36、execution-handoff、JB34 root acceptance、源码 notes、父 GLOSSARY 与 ADR0002。消费同一 contract3 / material-instance-result-v1、material_instances / parent_controls / observations / fact_keys / evidence_digest。未读取 ticket-sizing 作为预算。

JB34接口实际复跑：`test_material_result_is_frozen_before_export`，creative40、独立父100、同 snapshot/digest CSV；刷新40→90、删除目录后旧结果不变，新查询digest变化，跨tenant同ID700不混入。没有建立另一查询、同步或导出 owner。

历史前置：通过专用 M4b guard 对本地库运行 creative_report_day LEFT JOIN ad_creative 的 aggregate 探测：facts=0、legacy_without_evidence=0、missing_directory=0、deleted_directory=0（测试提交夹具清理之后）。没有代表性真实历史数据，历史可恢复比例 Unknown。

| 缺口 | 本地证据及处理 | 可恢复边界 |
|---|---|---|
| 已有 Numeric、无 evidence | 真实PG模拟旧行金额7；五态中 LEGACY_UNKNOWN=1，publisher=None | 原金额、主键、fetched_at/job可读；原请求、NULL/DASH/ABSENT与原观测不可重建 |
| 当前目录缺失 | unmapped creative17仍保留事实、creative/day | 不承诺找回从未同步或平台已删身份 |
| 只有 MID | historical-mid进入UNMAPPED_CREATIVE并保留MID | 不伪造VID、不改称精确实例 |
| 目录已删除/改名 | 旧冻结结果与CSV不变 | 只冻结已有证据；不能凭当前目录重建历史 |
| 未同步事实 | 无数据不补零 | 是否还能向平台恢复 Unknown；本票未调用平台 |

## 逐 AC

| AC | 实现与实际验证 |
|---|---|
| 首票接口 | 上述原 tracer 回归，浏览器/原导出链继续复用 |
| 历史探测 | 上表；无真实历史样本，不承诺补历史 |
| 每事实一次 | publisher合并完全相同重复页、冲突页报错；原投影PK维持tenant/account/creative/day；实例去重与多creative求和。五态测试重复输入c0，仍为17；源码double-count探针业务RED |
| 精确MID/冲突 | 同VID不同mid1/mid2保持2与8两个实例；CONFLICT不选候选；仅MID保留特殊身份 |
| 轮播/未映射 | COMPOSITE / UNMAPPED_CREATIVE进入原结果与CSV，保留assets及身份证据；普通实例合计10不含11/13/17/19。无父ID时不制造父控制 |
| 五态与观测 | 10/null/dash/absent/legacy7，同实例金额17且PARTIAL；五态各1。逐事实values、field_states、request、publisher scope/id/digest/version与原事实同事务；精确campaign发布version0，不借整账户水位或V+1 |
| 旧快照 | 原冻结owner保存身份、逐事实证据与特殊行；原40→90和目录删除回归仍绿，重查新digest |
| payload | SDK合成5creative、1普通实例、1轮播、2未映射。全范围payload231690字节，CSV219516字节，worker+HTTP110.28ms；筛选样本218715/156640字节、107.70ms。非规模基准，容量/平台支持Unknown |
| 形式化/原链 | bounded模型与源码PG拒错见formal skill；真实urllib3 SDKtransport→原sync Worker→PGRedis→Uvicorn/Vite/浏览器→原export HTTP→Celery→CSV。浏览器验证多creative与轮播特殊提示；全范围HTTP/CSV验证多creative/冲突/轮播/未映射，没有service/repository/browserresponse替身 |

碰撞矩阵：原另tenant同remoteID700隔离继承且复跑；新增同tenant双BC×双connection同ID：来源不明显示CONNECTION_AMBIGUOUS、connection=None且金额只一次；publisher精确来源选择对应连接；另一BC显示SOURCE_SCOPE_MISMATCH特殊桶。每个查询仍经真实授权；BC/connection不加入经济事实主键、不复制金额。不是任意授权配置的穷举证明。

## RED / 修复 / GREEN

- 初始业务RED：五creative实例被exact-single-creative条件整个跳过（StopIteration）。移除此限制，在原owner聚合同实例不同creative。
- 特殊桶扩展时真实PG暴露无父ID仍构造父过滤；修复为无可证明父ID不制造父控制。
- duplicate金额与NULL→0源码探针分别业务AssertionError RED；finally恢复仓外备份、cmp一致、同检查GREEN。SQL/import异常不计拒错。
- 夹具最初重复插入相同使用位置、SDK page_info计数未扩展等配置错误已修复，不计业务RED。

## 验证命令与边界

全部Python经 `python3 /tmp/tkada-native-series-20261112/m4b-implementation/run-test.py`；专用Python3.14、M4b PG/Redis，串行fixtures，无FLUSHDB、无共享依赖重装。

- `python -m pytest backend/tests/modules/reporting/test_material_instance_tracer.py backend/tests/modules/reporting/test_creative_projection.py backend/tests/modules/reporting/test_material_query.py -q --tb=short`：21 passed（含真实浏览器/worker）。随后新增轮播浏览器断言与逐事实values，tracer整文件6 passed；最终窄5 passed（仅跳过已绿browser，后续只类型/格式修正）。
- `python .agents/skills/formal-verify-reporting-data/scripts/material_identity_probes.py`：double-count、null-to-zero均业务RED→cmp→GREEN。
- material.py：原32+新增12 bounded PASS；broken-double-count、broken-null-to-zero均AssertionError退出1。
- `bun run build`：前端主/演示构建通过；后续UI计数调整后 `bunx tsc -p tsconfig.build.json`通过。
- 改动Python的Ruff check/format、git diff --check通过。
- 窄mypy9源文件：`--follow-imports=silent`，通过git show起点内容+外部临时shadow-file比较，不切checkout。原100/current90，新增诊断0；不称全仓mypy绿，未重跑旧全仓410。
- 扩展检查 creative_reporting 出现1条既有期望失配：test_hot_plan_pairs_ad_and_single_account_creative_facts只期望ad_day/creative_day，实际M4a还含campaign_day/adgroup_day；对应planner及该测试与起点无改动。该命令23 passed/1 failed；保留原测试并交root判断，不抹成全绿。

## JB36接口与 Unknown

原字段增量：MaterialInstanceResult.row_kind / identity_reason / field_state_counts；observations包含values、field_states、publisher、evidence、identity。coverage同样冻结field_state_counts及field_evidence；instance_count只含INSTANCE，special_row_count单列。CSV原writer增量携带同字段。父控制仍独立完整Ad范围，不受素材过滤、不进普通实例合计。

JB36继续完整金额grain/ratio/coverage分母、严格父范围可比性与差额、最终五种CSV行类；当前无实例的原summary/残差尚未变成最终五类，不提前声称完成JB36。CREATIVE_OBSERVED_CONTRACT唯一常量仅标本地观察，D0/NativeGrowth支持/自然付费/total成熟均UNCONFIRMED/Unknown。无真实平台或生产验收；SDKdouble不是平台证明。

## Root 独立阶段验收

Root 在候选 `6454a666baeff6a736813ea8d42b667c8cd58fea` 核对增量、正式 JB35/JB36 边界及发布/投影/冻结/导出调用。独立复跑 tracer、creative projection、material query：21 passed / 13.41s，包含真实浏览器与原 Worker/CSV 链；原7条弃用警告不影响行为。亲跑 material.py：原32与新增12个有界案例通过，不外推为全系统证明。源码拒错、构建与窄 mypy 证据继承候选的源码匹配结果，未重复无关全仓检查。

Root 复现 creative_reporting 旧调度断言：5 passed、1 failed，差异仅为 M4a 已有 campaign_day/adgroup_day。候选未改该调度和旧测试；本轮只将此断言更新为明确的四类日报，保留所有单账户断言，不改产品逻辑。更新后该文件6 passed / 0.67s。差异检查通过。

结论：JB35 本地阶段接受，可以在同一集成树推进 JB36；不是整 M4b 放行，不提前关闭 Linear。JB36 仍负责完整金额 grain/分母、可比父范围与最终五种 CSV 行；真实历史可恢复性、creative 平台口径及生产容量仍 Unknown。
