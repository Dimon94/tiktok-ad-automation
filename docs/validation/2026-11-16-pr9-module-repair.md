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


## 查询金额、缺失成员与素材汇总（Phase A）

父级历史金额按子事实行是否存在选择，不以指标真假值回退；真实子表NULL不回填旧金额。无返回的发布权威仍可抑制旧来源，但不再把所有目录对象变成默认查询成员。完整无事实对象仅保留既定四项成本/计数零值，收入仍为空；真实NULL与不完整范围不转零。

缺失同币种成员独立传播到SQL筛选、排序、汇总及冻结指标：已知金额为PARTIAL，缺分母的ROAS保持MISSING；实际不兼容来源桶不合并，不伪造平台观测。素材缺失向量使用预期creative来源坐标，浏览器与AI共用贡献收集器，仅INSTANCE参与普通素材金额，零实例缺失覆盖仍保留。精确观测按真实范围与身份稳定排序，等价查询的证据摘要不依赖搜索文字或数据库聚合顺序。

验证保留精确金额、原20k事实规模和性能门槛。首轮5 failed/39 passed，素材4 failed；中间174 passed/3 failed/2 deselected含原20k金额/成本守卫通过，剩余冗余Ad自连接在产品修正，旧桶位置断言改为精确来源坐标与完整证据比较。最终核心57 passed/7.82s；Native/定向/冻结/导出/素材/AI截止相关104 passed/2 deselected/25.47s。集合重叠不相加。14个修改/新增Python文件Ruff及格式、全app ty和差异检查通过。使用owned测试库且仅一个数据库执行者。

本节只完成Phase A。原5001账户/100k Campaign约970MB的summary仍需Phase B端到端表示合同修复；没有降低原样本规模/限制，也不宣称远端模块门禁已通过。


## 冻结证据容量与实际消费者（Phase B）

既有QuerySnapshotRow/ReviewSource仍是完整原件owner；每行存一次完整来源，指标与汇总只存统计及精确引用。原GET广告接口增加冻结证据页，游标包含行内偏移并沿用每次请求的权限、过期及合同检查；SQL切片校验原件完整性，不回查实时来源。AI保留64KB普通读取与成功交付才可引用的收据规则，原件引用归档本地化；DSH工具schema、真实来源UI和生成类型同步迁移。CSV保留现有数据行种类和数量，每行完整来源与本地引用独立解释；不新增观察/发布数据行。

结果表示版本更新为selected-report-result-v2、material-instance-result-v2、material query4、financial-review-v2；既有Native/creative金额口径不变。旧快照/不兼容重新导出沿既有409机制处理，无实时回退。保留公共publication_map及现有cache prefix，用canonical合同失效。

独立Standards/Spec R1各发现同一P1：跨两天仅一天完整空发布会误补零、生成ROAS并通过筛选。真实PG先红后绿，修正为canonical覆盖owner在完整请求窗口判定零资格，合成/缺失成员继续传播分母缺口。最终专项范围30项通过；两轴修正复审仍待提交后执行。

原5001账户/100000系列完整规模通过：快照19.658437875秒/429SQL，首页0.047892秒；汇总JSONB13989B，完整冻结行145824269B，首个HTTP指标页749330B，legacy夹具publication_map为2B。逐页读取全部100000条精确身份、Decimal金额、原字段状态和完整摘要独立重建通过，295.936519秒；整个测试含造数/EXPLAIN/分页393.53秒，不混为查询时间。陈旧物理表禁止断言改为实际选择源最多两次线性扫描，原样本/金额/数量/预算保留。EXPLAIN夹具以实际Connection建立保存点，防止测试回滚误影响冻结行；保留红绿证据。

其后仅语义范围元数据改为共享date/dimension/contribution投影、包含所有实际普通成员的scope；原始记录、SQL选择、分页和记录摘要流未改。最终等价搜索/排序不改摘要、不同缺失成员改摘要的正反证明通过，复用原10万条分页证据，不宣称再次全量遍历。

消费者152项通过（单独运行的SDK/浏览器用例当批未运行），最终范围/AI/素材30项通过；原1000系列/10000广告验收保留<=30SQL/<30秒，实测11SQL/363.06ms。新鲜隔离库的真实HTTP/SDK/Worker/浏览器/CSV完整链1项通过13.71秒，涵盖INSTANCE/COMPOSITE/UNMAPPED及父控制完整记录、原行数/金额、快照失效前取证与失效后CSV独立性。独立调度完整文件8项通过8.24秒；混合库残留导致的旧失败另记，不改生产控制器。DSH2项、来源展开浏览器1项通过；最终生产/demo构建、全app ty、28个Python文件Ruff和差异检查通过。集合重叠不相加。

本地证据不替代完整模块远端门禁。尚待额度配置、Linux任务夹具串行集成、最新主线合并及最终独立审查/CI；未部署或执行真实平台操作。


## Native SDK额度及Linux规范投递

完整226失败节点清单确认SDK注册了identity/native_series/get，但Ultimate50物理端点表遗漏该项，保留base并发3而既有SDK合同要求96。原精确用例先1 failed（3!=96），仅补同类端点配置后整文件52 passed/0.12s；基础额度、MCP、上传4并发及部署值不变，未修改线上配置。

隔离Linux原6c源码的完整Nativelevels[False]先失败：两个Celery消息均SUCCESS/IGNORED、无SDK请求、任务仍RUNNING；PendingDispatch正式task_name为reporting.backfill_step，测试helper硬编码reporting.job_step，消费端正确拒绝。helper在现有session内保存record.task_name并发送相同正式消息两次；不改产品围栏、等待时间、计数或重试。固定副本完整场景1 passed/76.72s，包括两页发布和真实浏览器；两次投递分别COMPLETE及重复竞争失败，PG最终COMPLETE/request_count2/pages2。诊断补丁与正式两行修复分离，主分支只纳入后者。

原EE与6c的helper/dispatcher/envelope/job_tasks blob相同，旧红绿证据适用于该根因；最终合并头Linux场景仍待验证。独立容器、网络及PG测试卷已清理，仅保留依赖缓存镜像；本地ownedPG/Redis继续用于根代理集成。Python语法/Ruff、JSON与差异检查通过。


## 最新主线 b5ee9fa4 联合集成

非破坏性合并b5ee9fa4，保留上游素材链接、AI经营报告/说明恢复、报表时效与空尾页续跑、目录身份、任务收尾及侧栏改动。客户端按两侧实际声明并集保留1756个唯一类型及SDK引用，不全量刷新无关schema；前端用例保留当前冻结选择/来源分页和上游历史时效两侧行为。文档完整保留两侧时间线，上游生产记录不代表本PR已部署。

新增纯合并0133_native_material_merge连接0132_native_ai_archive_merge与0132_material_link_upload。原分支历史迁移逐文件字节不变，新0132与上游字节一致；原迁移测试保留，新两侧升级保留tenant及Native/AI/素材链接表，迁移图/真实升级9项通过9.44秒。

Python冲突保留上游批量说明恢复及captured_at投影、SQLModel列类型边界。上游两处类型诊断以col/纯typing.cast解决，不改查询或身份逻辑。调度保留上游可执行候选/归档摘要围栏、UNKNOWN/在途判定及业务更新时间，也保留Native授权/本地取消与all_native_matched闸门。

联合125项先124 passed/1 failed：缺失回读用例拿排队前NEEDS_REVIEW摘要比较新RUNNING。真实PG采集表明显式单元唤醒已重新排队1条只读READBACK，修正测试按新时点重读公共结果并保留排队前NEEDS_REVIEW断言，精确验证新RUNNING/1条QUEUED；未为该例增加生产状态补丁。

另补既有MATERIAL/CTA两个前置失败例的真实续跑：实际PendingDispatch→deliver_step/finish_delivery唤醒→成功兄弟MATCH；新主线原始步骤标签会使已确定Native计划PARTIAL仍存RUNNING。现在仅在无实际在途步骤的收尾阶段复用native_completion.batch_results canonical owner，内部事实汇总不依赖原操作者仍可读公共API；非Native/真正QUEUED/RUNNING/UNKNOWN分支保留。真实红后专项16项通过66.18秒，包括兄弟成功/失败计划隔离、撤销账户无平台写、操作者停用后仍保留PARTIAL、上游收尾7例、实际financial-review-v2分析归档到HTTP封面（D7有效/缺失、精确金额及截止）。

最终全app ty、Ruff、生产/demo构建通过；独立端口且重试0的冻结来源/今日超时/历史完整三项前端通过4.6秒。默认本地服务器/环境组合此前三例未加载工作台，独立端口及明确本地API构建后通过，不将其判为源码修复。DSH联合12通过/2既有真实运行时门槛未运行；严格证据工具2例包含在其中。通过集合重叠不相加。最终提交后的两轴复审、合并头Linux及远端完整CI仍待，不宣称已上线。
