# 实时目录状态与身份补录份额

本轮完成 `.worktrees/sync-request-reduction` 原有未提交的 `directory_service_v1` 修改，基于已有32槽及测试明确的状态/身份各16保底、空闲互借。没有增加业务范围、数据库结构、队列、Worker并发、平台配额或功能开关；没有发布或调用真实平台。本轮聚焦提交主题为 `ads: preserve live directory service shares`。

## 行为与边界

- 用户定向回读、今日正消耗状态刷新标记 `status`；素材身份补录和素材子运行标记 `identity`，缺失父链继承来源目的。未标记历史运行保持未知来源，调度时属于非状态类；不凭年龄或kind猜测它是状态需求。
- live总窗口保持32，状态/非状态各16保底。当前另一类没有可执行候选时互借；已有借槽不被抢占，后续释放槽优先补足另一类。后台8槽及原依赖/全量份额不变。
- 生产者首次投递、恢复扫描、完成后直接补位共用原事务窗口锁。候选快照一次提取分类及在途数，各服务跳过锁定队首后保底；最终有界池前缀重新按租户轮转，避免小批次让同租户实时/后台一起越过其他租户。
- 实际热报/父链需求证明覆盖时，可批量CAS晋升未开始旧请求。保留tenant、BC、actor、冻结route、partition、request身份、年龄、代数；已投递任一代、已有页/发布/租约、素材子运行及后台运行均不晋升。行锁冲突跳过，后续正常恢复。
- 服务键只是本地调度元数据，不参与平台查询分区摘要；调用平台前剥离。素材子运行即使错误携带status，也仍按身份类计算。

## 验证

使用新专用本地PostgreSQL库 `test_directory_service_20261030`、Redis非零库13/14及传输边界替身。环境从 `/dev/null` 加载测试专用值，没有读取生产配置。

- 原有服务份额10项先复现5失败：身份流占满32、两方向单槽补位错误、生产者越过另一类保底；修复后通过。
- 派发/生产者/候选成本/目录请求/后台依赖/直接补位/调度：最终 **142 passed，34.13秒**。包括真实双连接行锁、并发完成、消息恢复、租户轮转、批次1/7/10、空闲互借及30,000活动运行+90,000历史消息的实际SQL访问上限。
- 进一步加入锁住状态队首仍各16、晋升锁冲突、父链目的继承、素材补录目的验证；专项与桥接/隔离/覆盖/关联依赖组合 **81 passed、1 failed，14.90秒**。新增专项全部通过。
- 最后扩大CAS范围到tenant、BC、advertiser、actor、route、partition、generation和query快照核对，专项 **36 passed，3.82秒**；8个实现模块再次通过CI ty，10个文件Ruff通过。
- 该唯一失败为原有 `test_backlog_dependencies.py::test_incomplete_directory_requires_unique_exact_video_identity[regular_complete]`。已在 `git archive HEAD` 的独立1292cdd0源码快照、明确打印该快照的material_links导入路径后复现同一失败（None而非v1）；本轮没有修改此测试或视频解析实现，作为既有基线交由主线整合核对。
- 先前扩大组合277通过/3失败，其中跨租户小批次回归已修复并包含在最终142项；两项直接构造平台DirectoryQuery的测试补齐本地服务键剥离后通过。测试夹具初版三项数据库约束错误也已修正，不将早期失败报告为最终通过。
- 10个变更Python文件Ruff/格式通过，8个实现模块按当前CI的 `ty check --python <测试虚拟环境>` 通过。尝试严格mypy产生37项历史未注解/泛型等诊断，未作为当前CI类型检查通过证据。

运行文件组合：

```text
最终142项：
tests/modules/reporting/test_directory_service_share.py（当时26项，后续增加6项）
tests/modules/reporting/test_directory_candidate_cost.py
tests/modules/reporting/test_directory_direct_delivery.py
tests/modules/reporting/test_job_tasks.py
tests/modules/ads/test_directory_requests.py
tests/modules/ads/test_directory_dependency_share.py

后续81通过/1既有失败：
tests/modules/reporting/test_directory_service_share.py
tests/modules/reporting/test_bridge_dependencies.py
tests/modules/reporting/test_service_isolation.py
tests/modules/reporting/test_backlog_dependencies.py
tests/modules/ads/test_directory_supersession_scope.py
```

## 发布与回退合同

这是无DDL的持久任务私有格式变化，不能按纯内存排序补丁发布。新生产者写入的 `run.query.directory_service_v1` 必须由会剥离该键的新执行代码处理；旧执行代码会把键交给DirectoryQuery并失败。发布协调者须按已授权方案安排全部旧生产者/消费者正常退出和切换，或明确控制生产者停止、新消费者先就绪的顺序，避免新生产者与旧消费者混跑。

不得直接回退到不了解该键的旧消费者，也不能静默清除持久标记。回退前须另行复核任务格式和未完成运行，确定安全方案。当前尚无生产业务验证；保底正确不代表持续事实/目录新鲜度已达标。
