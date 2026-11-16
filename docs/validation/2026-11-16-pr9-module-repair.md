# PR #9 完整模块门禁修复（2026-11-16）

## 输入和范围

`ee4879fa` 的正式上游 run `43799904966`：frontend、acceptance、browser、两类recovery及image共6项成功；模块实时进度明确出现断言失败，不能再只按运行器终止解释。诊断旧头 `b629efb6` 的 fork run `43796960444` 首个完整分区产物可用，124条失败摘要涉及58文件；与当前头比对后按实际根因处理，旧头证据不替代新头完整检查。

继续保留 M1–M5c、PR7/8 和最新上游，不纳入M6/M7/JB-66。不改历史迁移，不删测试、放宽性能门槛或增加产品兼容回退。完整私有日志/差异清单位于本轮 `/tmp/tkada-pr9-ci-20261115/`，不进入Git。

## 迁移与历史夹具

- ParentReportDay 的三列 ORM AutoString 与已迁移数据库 TEXT 不一致，约束名也不同；模型准确对齐既有DDL，不新增DDL、不改原迁移。
- 旧revision测试使用新ORM播种/读取，访问当时尚不存在的列。复用 `insert_historical_row` 反射与现有显式 historical 夹具入口；当前schema路径继续使用完整模型，历史读取使用反射表。共享scaffold在构造时确定媒体类型/代次，避免误以为修改脱离session的对象会写回。
- 旧快照仍保留原始bytes、ID、成员、scope及名称证据；缺少当前result_contract的身份明确返回409/query_snapshot_contract_changed，不伪造新合同让旧快照复用。

验证：当前头代表4项先4 failed/4.47s后同4 passed/5.88s。原52条迁移相关CI失败均有修复后通过证据；71项原失败及共享消费者首轮66 passed/5 failed，修正新暴露的历史播种点、旧合同断言及PG16客户端/PG17服务器不匹配后定点通过。分批共76个不同用例通过，集合重叠不相加、不伪称单次全绿；最新共享消费者5 passed/2.70s、原始证据两项2 passed/3.53s。旧备份用例用匹配的PG17.11客户端；环境失败独立保留。21路径AST/Ruff/格式/差异检查及产品文件ty通过，既有循环外键warning未压制。

其他模块修复和最终独立审查/远端结果继续追加；本节不宣称完整模块门禁通过。本轮没有部署或真实平台操作。


## 发布、健康修复与需求登记

- 发布阶段真实2404 SQL：复用 `_set_state` 的500桶批处理，同时写水位、Native合同、坐标和digest，返回本事务各桶精确版本；暂存摘要和Ad证据回写批量执行，Parent不再逐事实重读。定向Ad仍使用精确范围和version0，不推进整账户COMPLETE；原空发布、相同观察重放/冲突、旧观察保护及冻结需求revision保留。
- coverage两个生产者补齐PARENT_KINDS，父级独有陈旧也进入原修复预算。实际pending-actions链捕获KeyError/campaign_day后修复；新鲜度SQL每账户一次限定窗口读取，再展开层级，保留原扫描和行数预算。
- `report_demand` 复用已有500账户 `verify_read_routes`，不再每账户重复公共权限查询；仍按阶段读取当前tenant/BC/连接/账户证据及代数，不新增跨阶段缓存或权限绕过。
- 调度/执行单kind夹具使用canonical需求登记及生产priority映射；API/planner/health/Native上层仍验证真实三层展开。重领按精确job_id；补齐真实BC目录授权证据。冻结状态、pinned名称、普通INSTANCE金额和特殊行可见性的断言按既有合同分别核验。

验证：发布71 passed/13.11s（含Ad/Campaign/Adgroup原600桶`<40`门槛、Native及定向来源）；4BC两参数保留900秒SLO；4859账户两参数、跨kind第二账户撤权、错误BC/scope及四种批量撤权共11 passed/146.11s。健康pending-actions和parent-only核心8项通过。部分旧断言吸入原库历史测试数据，改为本例tenant断言并保留旧数据；涉及正式全局控制器的测试另建同一ownedPG上的全新隔离库，不删除原库。清洁库28 passed/27.40s包含整个actual_cost文件前后顺序、全局job_tasks、原健康1000探测/行数/参考结果门槛；健康日期/时区23 passed/148.99s。集合重叠不相加。

环境失败独立记录：本机PG17锁池64在造数、尚未到SLO时不足；仅owned51165测试集群对齐CI既有1024，重启保留原数据，Redis未改。新的私有guard仅改数据库名、权限0600。直接pytest没有加载CI结果插件；正式runner通过 `-p pytest_results` 加载 `deploy/ci/pytest_results.py`，既有文件间干净head-template隔离继续保留。

根代理完整ty检查暴露两个类型边界（SQLModel四列select重载与JSON stamp混合字典），分别使用SQLAlchemy Core select及明确TypedDict，不改SQL或数据。修正后全app ty、Ruff、原三层SQL门槛3 passed/5.39s。最初错误cwd导致的环境解析告警与真实类型错误分别留档；不把环境错误算产品缺陷。46路径格式/Ruff及差异检查通过。最终独立审查和远端门禁仍待后续源修复完成后执行。


## 第二分区的补充历史用例

另9条失败沿同一反射历史夹具修复，8文件仅测试改动。旧索引数据准备使用既有 `_route(seed)`、`ensure_scope` 和 `SyncSchedule`，避免调用今日需要新User授权列的planner/入队服务；当前运行路径仍由同文件其他用例验证。原50001行索引样本、状态计数、迁移升降往返、锁及冻结JSON断言保留。9 failed/18.53s后，包含前一阶段仅静态覆盖的5项共14项取得通过证据：首轮13 passed/1 failed/89.75s，最后历史scope播种修正后1 passed/5.39s。未重放此前52项通过集合，语法/Ruff/格式/差异检查通过。

正式ee4879fa两个完整分区产物均已获取：138/88条失败摘要，和旧两分区对比仅新增一条business prefix进度测试，单独核对上游独立日志规划合同。该正式头仍为6项成功、两个模块分区及汇总失败，不能将本地修复写成已远端全绿。


## 上游独立日志规划与经营补源

正式头唯一新增失败 `test_long_pending_continues_bounded_repair_rotation` 来自旧分支仍预期经营补源顺带触发日志规划。上游8091cfdc已按独立规划合同移除挂接，当前业务源码与上游一致。保留日志spy并断言无隐式日志调用，同时完整保留132账户、prefix128、后四账户及每轮两户的经营修复断言。测试及日志独立轮转/历史续接/开关边界4 passed/0.70s；仅测试和说明改动，不恢复旧产品耦合。


## Native 前置失败、真实管理回读与浏览器生命周期

`native_completion.batch_results` 原本只沿parent_step_id传播失败，遗漏同计划CTA/素材对未发送Campaign的真实前置约束。现按tenant/submission/unit作用域传播明确失败；创建链已终结时，剩余未执行准备步骤不再把计划保持RUNNING。UNKNOWN/MISMATCH、已成功远端对象及可执行兄弟计划优先级保留，不新写第二份状态。当前revision用例6.99s先失败；真实两账户权限围栏产生MATERIAL/CTA失败的两个新例也先失败，修复后3项通过。

41成员管理测试使用HTTP传输替身，通过真实20/20/1请求、回读outbox、目录worker/发布和观察达到SUCCEEDED，再执行不重发；CBO冲突夹具明确冻结CBO事实。ROAS来源按remote_id核验双方ABO/CBO，不依赖随机首行。c5夹具只为本租户按正确触发器顺序清理名称投影，不禁触发器/FK或删其他租户。目录测试复用真实详情产生的冻结快照，不再造无结果行的空快照。SimpleNamespace及旧冻结USD夹具补齐正式模型字段。

旧flat Native验证依赖本机/tmp历史生成器，CI不存在。原样纳入测试fixture，固定30e3b3c5历史源码及Git blob4512e4c28a9a29bcfa92930ea35ac31c77eedf0b；相对路径读取，不用runtime gitshow、不换当前生成器、不跳过断言。原冻结bytes/digest、真实API和触发器检查通过。

浏览器代理收尾Route already handled及漏入8000请求已孤立复现。先卸载轮询页面，再排空已跟踪代理，最后unroute；未吞异常、增加sleep或重试。原完整编辑/准备浏览器场景1 passed/21.59s。fixture组合35项通过，最后目录快照修正与8种Native成功/UNKNOWN/拒绝/取消状态合计9项通过；分批48个不同相关用例有通过证据，不伪称一次全绿。全app ty、修改Python及原样历史fixture Ruff、前端生产类型检查和差异检查通过。可选全前端测试类型检查仍有既有ES2020/旧fixture类型错误，日志留存，未作为已通过或扩展修复。

临时node_modules链接已移除，主树依赖未改。金融查询和证据容量阶段尚未实现，最终完整远端门禁未通过。
