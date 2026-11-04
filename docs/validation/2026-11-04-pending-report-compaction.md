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

## 生产结果（北京时间2026-11-04 11:29回读）

3956个候选全部检查完毕，人工实际取消2569个，跳过1387个，无SQL超时遗留、无结果不明的写入。分批期间正常维护持锁时让路，没有强删Redis锁。

最终独立只读回查2569个不同旧ID：全部 CANCELLED/pending_superseded，2569条旧引用全部 SUPERSEDED；旧 claim_token/claimed_until 和有效 head 租约均为0，无旧ID缺失。对应542个不同承接任务全部存在（529 QUEUED、13已正常进入WAITING），没有被本工具取消。不把取消数写成平台同步成功数或真实需求缺口消失。

1387个当轮跳过项的最终状态为1365 WAITING、1 COMPLETE、21 CANCELLED（后两类由正常调度自行终结）；对应候选winner多数已有领取/执行历史。request_count=0不能单独证明从未发送，因此不放宽至任意WAITING、不改冻结代数、不丢弃续点；仍需完整承接证明和发送成本证据。跳过数是本轮操作统计，不是剩余全站积压数。

所有成功apply回执使用同一源码hash `0167703a6ddf4dce52ceac33cf4af52afc601920de21bf4580f2db93f80a54bf`，先预演后重新锁定证明。补充分页跨界用例后，本模块32项回归通过；此前37项关联回归和Ruff/mypy通过，配置完整时CLI --help通过。未配置环境的首次本地help因缺少必需配置失败，未接触生产写入。

工具实现提交bb614b57、跨页测试67f4b390，合并本地main用于留档；未推送、未发布新应用、未执行结构变更或队列清空。生产应用保持55e0/schema0099。私有回执位于 `.runtime/sync-production-20261031/compaction50/`；真实ID、manifest与原始日志不提交。
