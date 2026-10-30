# 同步运行摘要归档

对应 [同步/查询重构规格 §5.3](../superpowers/specs/2026-10-31-sync-query-redesign.md)。只处理 report / directory / balance 的终态运行摘要和所属执行、成本证据，不删除业务金额事实（包括独立小时事实）或历史素材身份。

## 保留与引用

- report `finished_at`、directory `completed_at`、balance `completed_at` 起算，满90天仍在线；严格早于90天边界才成为候选。迁移对缺失历史终态时间使用迁移观察时间，不从 `created_at` 推造更早完成时间。
- 报表私有页沿用7天；目录成功/作废页沿用1小时，终态FAILED页保留7天排查/恢复窗口，完成时间未知、活claim/lease或待发布子桶仍保留，二者与摘要90天分离。活跃运行、待发布子桶、未结算/未封存成本、未保留身份的投递、有私有页、调度头引用均阻止删除。
- 目录已发布运行须有同租户/账户/BC/actor/类型/冻结路由的更高序号充分覆盖证明：先按相同partition索引查询，再按包含删除项的完整快照索引查询，两者均复核 `superseding_directory_predicate` 和完整路由JSON；不扫描其他选择性superset历史推断覆盖；仍被 `material_child_v1.source_run_id` 引用的源运行保留。当前唯一覆盖证明不上传、不删除。创建子运行与归档删除共享源父行锁。

## 素材子运行的充分覆盖证明

`material_child_v1` 不使用普通目录覆盖谓词。新子运行在创建时保存窄标量 `material_owner_observed_through`，值为冻结上下文所有 owner 观察时间的最大值；精确 owner ID 继续使用冻结 `query.parent_ids`，不永久复制宽素材桥。

成功子运行归档须同时满足：源运行仍存在且冻结授权签名一致；源运行有同授权、同目录类型、更高序号的已发布完整快照（走完整覆盖索引）；每个冻结 owner 均存在、来源连接/通道一致，且当前观察时间严格超过该上界。每批最多500个 owner、总数最多100,000，最终复核对 owner 加共享锁，对源和新覆盖证明加键共享锁。仍被引用的源运行先保留；子归档后，源才可按普通充分覆盖规则归档。目录对象和素材事实不删除。

0097不全表JOIN回填。历史成功子缺少标量时，preview只读并返回保留原因；首次 `--apply` 或维护任务只登记基线并返回 `material_child_owner_baseline_registered`，本轮不上传或删除。上下文仍存在时流式取真实owner最大观察时间；上下文已清理时必须核实所有当前owner及源身份，登记单调观察值的保守上界，等待后续更高观察才可归档。owner缺失、来源不符或源授权不符则不登记、不删除。长期没有新完整刷新或owner证明不完整的运行继续保留，并暴露具体原因。

## 持久化和恢复

1. 分页冻结摘要、执行子桶、冻结scope/bucket、观测和发布版本、成本收据与审计定位身份。每批最多256行、单行8 MiB，单运行至多100,000条归档记录/100 MiB NDJSON；超过预算保留原数据并返回 `archive_budget_exceeded`。
2. 释放数据库事务后上传不可变blob，并完整GET回读检查SHA256和字节长度。临时spool仅是传输暂存，不能作为归档成功依据；没有本地文件或内存持久化兜底。
3. 新事务锁定父运行和子证据，重算完整摘要，复核终态、时间、引用和版本；在同一事务写临时 `sync_archive_finalization` intent并分批删除源记录，每条DELETE最多500行。
4. 事务外写不可变 committed marker，完整回读验证后删除intent。若进程在删源后退出，下一轮先恢复intent。归档审计先查intent，否则只读committed marker，不把未通过最终数据库复核的raw blob当正式版本。

专用命名空间为 `tenants/{tenant}/sync-runs/v1/{kind}/{execution_id}/`，正文为 `blobs/{sha256}.jsonl`，正式标记为 `committed/{sha256}.json`。对象存储复用显式配置的S3/R2客户端认证和endpoint，但必须明确设置独立私有 `SYNC_ARCHIVE_BUCKET`，不得为空或等于可能公开分发的素材 `S3_BUCKET`；未配置时保持在线数据，任务返回 `storage_unconfigured`。不得对该namespace套用会删除正式归档的临时素材生命周期规则。

上传成功但数据库复核失败的候选blob可能保留；它不会进入审计resolver。当前不自动删除这些候选，避免对象生命周期规则误删已提交正文。本文不把候选对象回收或存储保留策略配置描述为已完成。

## 维护入口

独立 `sync.archive_runs(max_runs=1,time_budget_seconds=20)` 的函数默认一次处理一个intent或运行，入口允许显式设置1–1000轮/最多90秒。当前Beat每5秒投递 `max_runs=5,time_budget_seconds=3`，control队列priority=9，唤醒消息4秒过期；显式expiry不会被通用Beat默认覆盖。每次网络返回后才检查预算，这不是3秒硬截止；单次存储调用仍受客户端和任务超时限制。

每完成或延期一项即推进游标，轮转租户；marker或正文PUT/GET单项故障保留intent/source，并按项60秒Redis退避。同一批可继续处理其他租户，最老poison intent不会固定占住所有归档机会。intent按 `(created_at,tenant_id,kind,execution_id)` 有界游标扫描，每次最多32条。Redis仅保存可丢弃的扫描/退避状态，丢失只影响重试时机。

返回结果中的 `archived` 表示经过最终数据库复核并有持久正文；`finalization_pending` 表示正文可通过intent定位、正式marker待补；`item_deferred` 明确单项失败但其他项可继续。

从 `backend/` 以已核验环境运行；以下命令不包含真实凭据：

```bash
uv run python -m scripts.archive_sync_runs --tenant-id TENANT_UUID --kind report --limit 25
uv run python -m scripts.archive_sync_runs --tenant-id TENANT_UUID --kind report --limit 25 --apply
uv run python -m scripts.archive_sync_runs --tenant-id TENANT_UUID --kind report --resolve-run-id RUN_UUID
```

首条只预览；第二条会写对象存储并在验证后删除在线摘要，需要对应环境的操作授权；第三条只读定位。分页继续使用输出中的 `after_time` / `after_id`。旧 `cleanup_ad_directory_history` 入口已改用同一显式租户归档命令，不再提供30天直接删除路径。

每日运行新增量、归档处理量和存储容量必须单独测量后调整维护频率。单轮有界不等于已经证明历史积压或每日净增长收敛。本地测试使用真实PG/Redis和对象存储传输替身，不构成真实S3/R2联调或生产验收。
