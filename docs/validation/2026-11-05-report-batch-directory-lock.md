# 报表整批账户规划并行

状态：真实高峰根因确认，候选验证与独立审查完成，准备生产发布，尚未声明生产收益。

## 具体问题

第一轮faf仅覆盖单户，但真实249个热ad_day仅27个单户，161个5户批次；其余也是多户。jobs._split只处理截断异常，并非普通热报常规拆户。高峰140次pg_locks采样中，61个BC exclusive等待采样行全部同PID持有ad_day发布锁；46个BC exclusive持有行中45个也持发布锁。它们不是61个独立任务计数。已确认主要是不同账户批次间不必要的全BC互斥，非CPU配额或新账户锁。

## 最小修复

所有report入口以原BC键shared为围栏，随后按sorted(set(job.advertiser_ids))一次取得整批账户exclusive锁，读取pending/facts前拿齐，全部持至提交。与publish._writable_buckets已有的账户发布锁排序一致；不重叠批次可并行，重叠批次仍互斥去重。health/default保留BC exclusive，继续保护它跨账户轮转，不做额外health改造。旧版exclusive与新shared保持互斥，滚动发布无去重空窗。每事务只调用一次报告目录规划，不允许先shared后升级默认exclusive。

没有修改任务账户/日期/owner/路由/需求水位或事实范围；没有新DDL、消息合同、配置、业务开关、额度或Worker调整。8后台/typed8/prefetch12及原资源保持。沿已授权同schema快速发布流程核对精确镜像、11角色/9Celery/唯一Beat/schema0101/配置/HTTPS，保留正常退出；不新建备份或恢复演练。

## 验证

四个真实有授权账户的两个不重叠双户批次在faf旧实现下，第二批等待BC exclusive并200毫秒超时。新实现必须让第二批在第一批事务未结束时真实创建6个目录请求并独立提交。反向账户输入的重叠双户批次真实多连接同时运行，必须完整提交且只创建6个请求。保留单户、同户、health双向互斥和旧版本锁域回归。

原始高峰锁、CPU和测试日志保存在忽略目录.runtime/sync-production-20261031/。不把同步状态数量、一次调用增多或短窗混合负载当成因果提速证明。


最终验证：5个针对性文件113例中112通过，唯一新fixture因缺checked_at被真实授权检查拒绝，补齐真实授权观察时间后11个并发用例全通过8.56秒，其余112项未受fixture修正影响。正确授权fixture在精确faf旧源复验仍因BC规划锁200毫秒超时失败。没有跳过verify_route。Ruff通过；strict mypy与既有源同5项旧错误，无新增。独立最终审查无Critical/Important，未增加修改范围。
