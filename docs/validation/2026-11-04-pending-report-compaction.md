# 2026-11-04 相同补链旧执行清理

用户授权取消积压中重复的旧任务，只保留最新相同任务。维护入口为 `scripts/compact-pending-report-runs.py`，与应用部署分离，不改 schema、限流或业务开关。

## 安全边界

首轮仅处理 contract 2 的 material_links。租户、BC、actor、owner、报表类型、业务日期、账户/系列筛选、冻结路由全部一致；最新执行须真正未开始，每一条旧 PENDING 引用须由新 PENDING 引用以不低于旧 revision/requested_after 完整承接。不同日期不合并，已开始的新任务不作替代。完整引用超过每任务512则整组跳过。

两执行、全部 scope/ref/requirement/head 按固定顺序短锁复验；任何锁缺失整组保存点回滚。事务6秒、SQL3秒、锁等待100毫秒。只将旧执行标为 CANCELLED/pending_superseded、旧引用 SUPERSEDED，经正常 outcome 重排头；不推进 desired/satisfied/requested_after，不删除事实或分页。

发现走现有 active job_key 索引，256条分页，最多50000条/20秒，截断明确标记；它只生成候选。每批最多128对，先 rollback 预演，再以准确 manifest 和源码 hash 绑定 apply，apply 再锁定复验。网络返回不确定时先查已知ID状态，禁止自动重放写入。借用既有 reporting cleanup Redis 锁，不抢占正在执行的维护。

## 验证

- 原失败案例 RED 已复现，最终37项真实 PostgreSQL/Redis 回归通过（包含承接后正常执行发布、六类锁竞争、回滚、未覆盖/低版本/旧新鲜度/进度/日期/路由/代数/owner/租约/最新运行中/超限/其他租约保留）。
- Ruff 和定向 mypy 通过，独立复核 CLEAR。
- 生产只读扫描48页，12237活跃执行，其中10803补链执行、6847完整业务组、3956额外重复候选；候选不等于可取消数。
- 初次全局聚合在3秒保护下超时，未写入；已改索引分页。初次预演的超时/跳过不计清理成功。
- 生产精确版本55e0f93365671f5296c3bfcaba0ada63bd06bc89，schema0099。执行器核对既有 schedule/report_demand 源码 hash，仅在维护进程加载经审查模块，不修改容器文件、不发布应用。

生产执行回执与最终核验另行补录；真实ID和原始manifest保留在私有运行目录，不提交。
