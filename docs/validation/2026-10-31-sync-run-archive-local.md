# W8 运行摘要归档本地验证

环境：隔离工作树 `sync-query-redesign`，真实本地 PostgreSQL `sync_redesign_test_archive`（最终短回归自动升级至0098），独立 Redis 59381；S3/R2只使用客户端传输边界替身。没有真实TikTok/S3写入、生产修改、提交、合并或发布。

## 已实现

- 三类运行终态摘要至少90天在线；旧7天jobs/balances删除入口停止删除摘要及执行/成本证据，私有页保留策略独立。
- 流式冻结和临时传输spool，100,000记录/100 MiB总预算，256行读取/500行删除；超过预算保留原数据并返回原因。
- 独立私有归档bucket，不能为空或复用素材桶。内容寻址候选blob完整GET校验；最终数据库复核、临时finalization intent和删源同事务；提交后持久marker校验再删除intent。
- 审计按tenant/kind/execution定位intent或正式marker，raw候选不可作为正式版本。金额事实及其裸UUID审计指针保留。
- 目录当前必要覆盖证明及JSON源引用保留，有更高同冻结范围覆盖且无引用的旧摘要可回收；source创建入口与归档共享父行锁。
- 独立维护任务支持显式轮数/时间预算，处理一轮后轮转租户；intent恢复优先，Redis游标不承担持久证据。
- 原页清理实测在合并回归里出现batch=2删除5页；物化页键CTE后固定单条删除上限，保留原终态和引用检查。

## 验证命令和结果

```bash
python3 .runtime/run.py archive -m pytest \
  tests/modules/sync_control/test_archive.py \
  tests/modules/sync_control/test_archive_tasks.py \
  tests/modules/sync_control/test_cleanup_contract.py \
  tests/modules/sync_control/test_retention.py \
  tests/modules/ads/test_material_directory_children.py \
  tests/modules/reporting/test_cleanup_batches.py \
  tests/modules/reporting/test_cleanup_page_scan.py -q -m 'not mcp' --tb=short
```

80 passed，5 deselected，15.44秒。之后补齐ContentLength异常和JSON引用查询与0092表达式索引的精确一致性，相关定向3 passed，0.86秒。

```bash
python3 .runtime/run.py archive -m pytest \
  tests/modules/reporting/test_cleanup_batches.py \
  tests/modules/reporting/test_cleanup_page_scan.py \
  tests/modules/reporting/test_job_tasks.py -k cleanup -q --tb=short
```

16 passed，68 deselected，1.88秒。验证旧7/8天父运行删除断言已迁移，短期页和快照行为保持。后续审查确认 CampaignReportHour 是独立小时事实，root 已停止其30天清理并由独立测试验证；不沿用早期“小时缓存”判断。

新增归档模块、存储适配、维护任务和CLI的Ruff、格式、ty、Python语法检查及CLI `--help`通过。真实双连接测试覆盖HTTP阶段无数据库父锁、上传中源变化不删、删除未提交时JSON子引用创建被锁阻塞；两阶段失败测试覆盖上传/校验异常、数据库提交异常、marker异常、marker成功但删intent提交异常。

## 未声称完成的验收

- 真实私有S3/R2桶权限、持久性/生命周期配置与联调尚未执行。
- root负责Settings/模板/任务调度接线及全部迁移总审查；本证据不代替最终全仓验收。
- 尚未证明生产每日新增率小于归档处理率，也未完成百万目录历史下覆盖证明查询计划验收；单轮有界不是增长收敛证明。
- 候选blob在上传后数据库复核失败时可能保留，当前不自动清理，且不进入审计resolver。
- 私有spool预算当前使用未压缩NDJSON；未以未经测量的gzip节省率或存储费用作承诺。

## 独立审查后的收尾

- 三个原归档模块 strict mypy 报错已消除；不弱化项目配置。新增子证明模块也纳入四模块 mypy 和 ty。
- 目录覆盖查询改成 exact partition / full snapshot 两条索引充分证明，包含 tenant/account/BC/actor/kind/ad_type/冻结route hash，复核完整route。真实PG小样本 `EXPLAIN` 强制索引路径验证命中0095对应索引及BC/hash Index Cond；这是路径正确性，不是百万行性能结果。
- poison marker / 正文 PUT/GET 按项60秒退避和游标推进；两租户持续故障回归证明A失败不阻B补marker或归档。Beat接线断言5秒、max_runs5、3秒预算、control priority9、expiry4。
- FAILED目录页增加7天窗口、claim/lease/pending/未知完成时间保护和有界页删除，随后90天可归档。
- 成功素材子新增窄owner观察上界、源完整覆盖证明和所有owner来源/观察复核；0097无历史大JOIN。真实主→子发布→新完整刷新→旧子/旧主依次归档通过；事实仍在。
- 历史缺标量preview不写，首次apply只登记；上下文仍在取真实max，已清取当前owner保守max并等待更新。缺owner拒登记、上传中owner来源改变保留正文且无正式marker。新增9例，1.86秒通过。

最终合并收尾回归 **97 passed，5 deselected，16.51秒**，结果见 `.runtime/archive-final-correctness.log`（本地日志，不提交）；四模块 strict mypy、ty、Ruff全部通过。此组包括归档、维护、公平退避、FAILED页、保留合同、素材子真实发布和报表有界页清理。没有性能独占压测或真实对象存储写入。
