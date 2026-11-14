# JB37 / M4B-04 查询生命周期本地候选

指定树 `projects/tiktok-ad-automation.wt/feat-jb33-native-material-m4b`，分支 `feat/jb33-native-material-m4b`，精确起点 `b62be2ca3a10fe3effe3eefd6a32335c34ea10fb`。一名写者，无子代理，保留 `.venv` / `node_modules` 原链接。没有另建树/分支、写 main/外层仓库、平台调用、Linear 写入、推送、PR、合并或部署。JB35/JB36 的 root 阶段接受有效，票状态和依赖边不变。

## 实际前置探测

本轮先亲跑 JB36 最终 metrics + instance tracer：13 passed /18.48s；包含真实浏览器→HTTP→PG/Redis→原同步和导出Worker→五类CSV，完整 contract3 / `material-instance-result-v1`，实例、五态creative证据、独立父控制、对账、普通实例summary、coverage/publication_map/digest。没有以首票窄payload代替最终接口。

随后在真实存储执行层用 SQLAlchemy 执行事件，在第二批100行后的冻结更新、creative观测读取、父Ad直接事实读取时执行 PostgreSQL `SELECT 1/0`。未mock service、repository或浏览器响应。首个RED记录 `/tmp/jb37-red.log`：第二批失败后PG无快照、无行、Redis无页面，但读取只得到 `query_build_busy`。原Worker吞掉失败导致调用方等预算耗尽。后续最初测试误用 test Redis DB1，而实际page_cache用app DB0；改为真实page_cache.client，不改变业务预期。

另一个实际RED `/tmp/jb37-contract-red.log`：把现有冻结快照合同置旧后，不带旧snapshot ID重新查询仍复用旧身份并报409。此探测源数据未变，不声称随机新ID意味着金额或digest有业务变化。

## 实现

- 继续使用原RR、freeze_material_results的100行批次、QuerySnapshot和15分钟TTL。完整payload仍在一次RR提交后才开放；没有新增迁移、查询/恢复/报表owner或TTL系统。
- 原page_cache在仍持有当前租约时发布泛化 `query_build_failed`；复用原60秒缓存寿命、原watch/Worker重试与force_refresh入口，不包含异常内容或业务数据。失败先完成PG回滚，Redis没有半成品。显式重查清失败通知，成功清通知；旧租约不能发布本代错误。
- query_models 的原合同判断提取为唯一 `snapshot_contract_current`，read_snapshot和query_reuse三处候选复用共同消费。旧ID仍409，显式新查询跳过旧合同另建ID，不在旧身份内修补。过期仍由原read_snapshot拒绝。
- 工作区停止自动重试错误查询，隐藏错误查询的旧行/summary/snapshot，显示可操作错误与“重新查询”。复用原loadLatest清选择、详情、页码、游标和旧snapshot；错误期间停止自动刷新。用户“应用筛选”明确发起新一轮force刷新。
- 查询失败/过期均不修改旧ReportExport。旧已完成对象可按原24小时到期读取；旧待生成合同被原export owner拒绝，无对象写出。导出owner本轮未改。

## 真实验收与边界

`test_material_query_lifecycle.py` 从真实PG/Redis和API观察结果：三种故障分别持续两轮；每轮快照header、所有行、summary/detail和新export不可读，HTTP查询503、对失败ID创建导出404。第二批测试有101条素材，故障前确实走过首批冻结写入。删除故障后新查询成功且ID不属于两次失败ID；额外发布creative40→90说明新结果digest变化来自输入观测。

记录样例 `/tmp/jb37-green.log`：第二批失败ID `ade9ef40-01b6-4279-aedc-602a82cb9262` / `2f18a623-cfd5-40fd-8149-396191069a5c`，恢复ID `c5603a64-fc6b-4033-bb92-3f3a0aadc0cb`，digest `c121fcebbe6e87cbf0cddab64ea6d12a89f02f9fdb11ac2024ce7c7735a67d19`。这是本地合成数据，不是平台观测。

到期用真实PG保存旧时间坐标（非等待15分钟）：page、detail、新export均409；新查询新ID，旧值40不被新值90覆盖。旧合同相同拒绝；旧成品字节和原expires_at保持，旧待生成合同生成失败、object_key为空。私有事务和实际共享租约构建另测无源变更的旧合同重查，获得新ID且业务payload保持相同。

最高Seam扩展原 live_campaign_workspace / material tracer：浏览器选账户/日期/素材，打开实例、creative/父控制和差额，点击原导出；浏览器同路由HTTP实测过期与合同409；仅为当前合成tenant安装PG INSERT故障trigger，浏览器应用筛选显示错误，第二次重新查询仍失败；移除trigger后重新查询成功、新ID、素材行恢复。trigger在finally删除。原export Worker随后生成同一旧冻结五类CSV并核对digest/证据/字节。没有mock业务或浏览器响应，SDK urllib3 transport是唯一平台替身。过期和合同拒绝用HTTP实测，未声称通过UI操纵真实15分钟流逝。

第一次浏览器探针试图用URL传snapshot_id，发现现有工作区不从URL加载查询条件；这是工装路径错误，改用浏览器同路由HTTP验证旧ID，真实UI故障/重查仍从按钮完成。最终浏览器1 passed /13.83s，取代该失败轮。

## 形式验证与命令

使用现有 `formal-verify-reporting-data`。material_lifecycle模型24有界组合；broken atomic/TTL/isolation均须AssertionError。假设固定两批、已提交授权事实、PG RR语义和有限时钟；不是生产时间、无界并发或全系统证明。实际源码探针覆盖提前提交半批、放行过期、复用旧合同和隐藏Worker失败；每次外部备份→业务RED→finally恢复→cmp→真实PG GREEN。旧源更新隔离另外复跑既有material_probes的真实源码绑定。

所有验证从指定root/backend经 `/tmp/tkada-native-series-20261112/m4b-implementation/run-test.py`，专用loopbackPG/Redis和已有Python3.14；没有FLUSHDB、并发fixture suite、依赖重装或终止无关进程。私有环境文件未打印或提交。

最终命令和结果在下段补齐。已亲跑前置13项、实际故障/时间/合同链与浏览器；JB36既有金额/范围模型、四项金额探针和此前25项相关回归为继承证据，不冒称本轮全重跑。creative平台支持、口径/成熟度、生产容量仍Unknown。

## JB38 精确消费交接

消费 contract3 / `material-instance-result-v1`，原ReportExport.frozen_rows/result_contract/publication_map/coverage保持；五类CSV仍是原 `_export_rows` / `_csv_rows`。查询结果完整RR提交后才可创建导出，查询15分钟到期或旧合同返回409明确重查；原已完成对象不受查询失效影响，待生成旧合同不能重新解释。JB38继续原export状态机、对象存储、Worker、幂等键、阶段权限和24小时owner，补对象写前/后故障、重试/重复消息/重复下载、逐阶段撤权、排队/运行/完成24小时到期矩阵。不得改回实时读取或重建查询，不把本轮兼容测试当作全导出矩阵验收。

JB37为本地可审阅候选；整个M4b的fresh Standards/Spec独立审查、本地集成和关闭由root负责。

## 最终亲跑结果

- `/tmp/jb37-final.log`：lifecycle + tracer + page_cache + query_reuse 四模块，35 passed /1 failed /27.71s。唯一失败为既有清理测试的随机UUID窗口：全局5000行清理预算也删除了其他到期快照1行，目标因此剩3而非2。只将该夹具ID固定为UUID最大值，使其游标窗口只包含自己；没有改业务清理或清空其他数据。`/tmp/jb37-cleanup-fixture.log` 定向重跑1 passed /1.57s。其余35项和最终浏览器结果未失效，不为测试夹具修复重复整个suite。
- 最终浏览器完整快照 `d3f87e07-90b6-4cfd-a390-1ebb4239bb40`，5素材行、4实例、父控制3份（CSV按身份去重）、对账4份、publication_map5坐标、summary `SELECTED_MATERIALS`。首行digest `e2e06c334ab5007acad8ef4694212590e5184f411976892ad2241de422aecff4`；全部5个digest在上述日志。原Worker CSV357,311 bytes，冻结payload437,107 bytes，Worker+HTTP117.46ms。本地合成样本，不是生产性能承诺。
- `/tmp/jb37-probes-final.log`：四个实际源码近似错误均业务AssertionError RED，每次finally外部备份恢复/cmp后6项PG/Redis/HTTP GREEN；各轮5.97/7.00/8.15/7.88s。最初新增detail断言漏传EntityRef.tenant_id导致一次工装TypeError，已修正；另将旧合同重查失败显式变成业务AssertionError以满足探针判据，未改成功预期。最终结果替代这些失败轮。
- `/tmp/jb37-isolation-probes.log`：既有parent/scope/tenant-sql/live-freeze四个实际源码探针本轮重新执行，全部RED→finally/cmp→GREEN；其中live-freeze绑定旧快照源40→90不变。
- 新模型24组合GREEN；broken-atomic /broken-ttl /broken-isolation均exit1、AssertionError。原子性和TTL的真实PG绑定见上述故障/时间测试；不是把抽象模型结果当成数据库证明。
- `/tmp/jb37-build-final.log`：`bun run --cwd frontend build`（tsc +生产/演示Vite）通过；最后只作TSX换行排版。修改Python产品/测试文件Ruff全部通过，`git diff --check`通过。不重跑无关巨型suite或历史410诊断mypy。

本票所列本地AC已闭合。独立审查、整个MMF集成/关闭、JB38完整导出矩阵、真实平台语义及生产容量不包含在此结论中。
