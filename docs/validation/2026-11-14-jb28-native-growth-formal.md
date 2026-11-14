# JB28 fresh Sol/high 验证候选

起点与产品 sync_commit：`a749ad484ed76d48d41bde2c588eb62706996669`。
唯一树 `projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a`，分支
`feat/jb24-native-growth-m4a`。仅验证文件/测试/skill/本文及进度更新；原产品字节保留。
这是交 root 独立验收的验证候选，不是 M4a 放行，JB29 仍不得启动。

## 范围与独立预期

旧入口亲跑：R4/R10 48状态、R7 57组件状态、denominator六例通过。
六例只证明六个算术例子；旧检查不覆盖 R2/R5/R6/R8 生成范围。

新增 `generated.py` 直接调用真实 metric_vector/merge_metric_buckets，独立预期使用
Fraction 有理数及明确缺项集合，不调用产品求和或比率函数产生期望。
七类有界性质：全未知仍NULL/已知部分金额求和；完整分母的金额比率；
level/source/contract/currency分桶；跨账户时区MIXED；D0合同24小时；
total成熟截止未知；跨本地午夜不截断点击窗口。

- 算术穷举：两个成员，各自spend/revenue取 NULL、DASH、ABSENT、0、0.1、90，6^4=1,296。
  数值层将前三者归为缺项；真实PG发布路径另检查三种字段状态原样保留。
- 坐标分解：以基准Campaign/direct/native/USD逐一改变level三个值、source两个、
  contract两个、currency两个，共9检查。键是四元组等值关系，逐坐标分解假设
  产品没有隐藏的坐标交互；真实PG另绑定source+contract及多币种跨日反例。
- R8：UTC/Hong_Kong/Los_Angeles × 00/23点 × 10/50分，12案例。
  仅验证固定日期上的点击后24小时解释，包含23:50→次日00:10。
  不模拟平台事件归因、夏令时切换或收益成熟；不是liveness证明。
- window不是任意用户输入的向量键：固定native revision决定每个metric窗口。
  Calendar/hour不能拼接日收益的入口沿原目录/小时合同审计，不伪造可查询窗口。
  scope也不是任意向量键：实际ReportingFilter/AdContributionScope由PG组合矩阵验证。

真实PG生成：36个固定锚点10/20＋生成成员，NULL/DASH/ABSENT/0/0.1/90两字段遍历。
每例发布→同一SQL筛选/summary→MetricVector/原字段证据→阈值/双向NULL-last，
Campaign/Account/Drama使用同一预期；账户本地日固定Los_Angeles而非机器日期。
另有两日USD/EUR .8与4独立桶、direct40/50和derived20/5独立source合同桶。

目录矩阵共9个fixture（Campaign/Adgroup各4种whole/nonempty Ad masks，Ad只whole），每个执行7组属性正例、5组反例，并检查可用父路径。目录组合覆盖三层实际入口：ID、名称、状态、审核、预算模式、创建日、广告类型，
逐属性及名称＋状态＋预算＋创建日组合；正反例、父ID路径、阈值与Ad子集交叉。
预算数值只支持显示/排序，未发明不存在的预算数值过滤或creative/material父贡献入口。
Account/Drama同日Campaign40/50＋Adgroup40/50＋Ad缺项50仍汇总40/50。

## 源码同步审计与分类

读取相关提交：2662522d（Adgroup/统一levels）、7941c176（消费者/调度）、
b17b9b36（publication可靠性）、854b86ec（完整分母）、658e7700（source/scope）、
a749ad48（provenance/空桶坐标）。旧R4 sync=7d987d8，R7文档基准7941c176。

- **updated**：R2/R5/R6/R8验证描述。旧六例并非当前SQL范围模型；补真实PG生成和有理数探索。
- **in sync**：R4/R10物理分离与direct权威。publish_campaign_day拆为publish_parent_day、
  新Adgroup selected view同构；层级注册和空桶仍保持父级独立直接发布、子writer只写derived。
- **in sync**：R7有界事务/水位描述。a749坐标进入publication digest是同事务载荷扩充，
  没有把一次事务拆成多个步骤；真实发布68项源匹配证据继承，不重跑竞态全集。
- **updated**：旧scope_probes多日比率替换字符串已不在当前源码，daily改成records后保持相同性质。
- **bug**：本轮尚未发现已确认产品counterexample；发现的探针存活均先诊断验证可达性/缺断言。
- **blocked/Unknown**：真实平台/生产、任意规模、任意并发、未支持窗口组合、JB29冻结均未证明。

映射：`row_set.fact_coordinates/_facts_cte/_metric_expr/_row_buckets/_summary_select`负责
选择/分桶/阈值/汇总；`dimension_query.metric_vector/aggregate_metrics/merge_metric_buckets`
负责完整分母与同坐标合并；`vector_provenance/report_provenance`保留field_state_counts、
known/missing、denominator counts、scope_digest、BucketStamp/local_ranges/observations/metric_contract。
`publish._publication_coordinates`持久化空桶currency/timezone/local_day/level/source/contract/window/scope。
`metric_catalog.semantics_for`负责D0/total窗口，不从现在账户元数据追认历史空桶。

## 源码错误探针

全部只修改实际源码，仓外临时备份，finally恢复，cmp逐字节核对，恢复后同命令GREEN。
新增探针运行入口 `generated_probes.py [probe-name ...]`；TEST均为
`backend/tests/modules/reporting/test_native_scope_roas.py`，通过守卫运行pytest `-k <下表test> -q --tb=short`。

|性质/探针|实际owner|test / 必须RED的业务断言|
|---|---|---|
|父子相加|row_set._metric_sums|generated_directory_scope_matrix：40/50不叠加Ad派生花费|
|平均平台比率|dimension_query.report_provenance|platform_ratio_is_not_averaged_across_days：平台聚合NULL，逐日.801/.903保留|
|跨币种|row_set._facts_cte|generated_currency_days_pg：USD .8/EUR4各自独立|
|跨来源相除|row_set.fact_coordinates|source_contract_buckets_are_not_combined：direct .8/derived4，不是60/55|
|missing通过阈值|row_set._metric_bounds|generated_field_states_pg：缺项c1不通过>=0|
|missing→0|dimension_query.metric_vector|generated_field_states_pg：c1原缺项向量仍NULL|
|D0午夜截断|metric_catalog.semantics_for|generated_field_states_pg：原PG证据metric_contract.click_hours=24|

初始父子探针改在随后被empty-bucket SQL覆盖的位置，5项绿属不可达破坏；改到实际
metric_sums读入child派生值后业务断言RED。初始币种常量破坏产生GROUP BY语法错误，
不计有效；改合法CASE表达式后断言RED。missing→0初次存活：锚点汇总已有20，
缺乏单成员NULL断言；补c1真实向量NULL和逐字段状态独立预期后再跑，未加产品guard。
脚本导入首次因模型注册顺序失败，补app.models注册；目录fixture首次group参数写错，
改已有adgroup参数。Ad contribution在Ad维度拒绝是既有合同，矩阵只列实际支持维度。

继承且亲核替换位置/测试仍匹配：denominator_probes三条（Python完整分母、SQL完整分母、
Account不能读derived）；scope_probes前三条（忽略子集、source合同合桶、旧合同误解释）；
/tmp/jb28-product-probes.py两条（空桶借当前账户时区、丢聚合证据）。
旧scope第四条因源码移动更新并定向重跑。这里的继承不冒充本轮新执行。

## AC1–17最终证据矩阵

实测=本轮；继承=产品字节匹配上轮记录；审计=静态代码路径；Unknown列出动态边界。

|AC|证据类别与结论|
|---|---|
|1|继承SDK→Worker→PG/Redis→HTTP/五视图浏览器；实测生成SQL成员/阈值/summary/向量一致。|
|2|实测目录组合矩阵三层ID/名称/状态/审核/预算模式/创建日/父路径与子集；预算金额显示owner静态审计。|
|3|实测整对象与显式Ad子集；继承工作区输入/切层清除；审计没有creative/material父贡献入口。|
|4|实测parent40不能除subset5；Account/Drama子集也保留spend5和NULL收益/比率。|
|5|实测旧合同/行合同冲突/空桶回归；继承direct dash/空行无derived回退及旧观测展示。|
|6|实测同日三层不重复计；Account/Drama只Campaign；命名归属版本及歧义沿原owner审计/继承。|
|7|实测20+90/10+90=1.1；父子叠加源码探针RED/恢复GREEN。|
|8|实测36组生成/1,296算术探索：部分收入完整分母、缺分母、零分母与真实零。|
|9|实测source/contract、多币种跨日、有限坐标分区；时区MIXED有界检查；跨时区PG既有dimension证据。|
|10|实测多日平台NULL/原Decimal观察；平均平台比率源码探针RED。|
|11|实测平台.801与本地.8分别保留；继承SDK精度与页面展开。|
|12|实测12本地时间窗口解释及PG合同D0=24/total未知；Calendar/hour入口审计，非真实事件归因证明。|
|13|实测NULL不过阈值、双向NULL-last；继承同名/部分/旧观测五视图及52对象50/2分页。|
|14|继承material定向测试；其余自动规则/经营/首页/MINI_APP/creative/目录消费者静态审计，未声称全部动态回归。|
|15|继承2,000合成PG计划（首查3.49s/续页1.30s）；实测生成查询同SQL路径，非生产容量。|
|16|上表七条本轮源码探针，加源匹配继承与更新旧探针；均以业务断言拒绝，不计SQL异常。|
|17|实测R2/R5/R6/R8有界生成与PG/源码反例绑定；七类性质不是全系统/无界/liveness证明。|

主Seam继承39+68、SDK五视图/52分页、build/tsc/Ruff与2k计划源匹配证据，不重跑浏览器或全量。
严格mypy旧410诊断无新增结论继承；本轮仅验证改动，不重跑全仓mypy或archive。
Native Growth原字段名称、自然＋付费待确认、D0点击后24h保持原批准合同。

## 执行回执

RUN=`python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py`。
最终聚焦suite/Ruff/diff与源码字节核对结果在本节补记。所有数据合成，专用本地PG/Redis，
串行fixtures；无生产/真实TikTok/API/广告write，未改Linear/Spec，未合main/push/PR/deploy。

新增探针实际RED摘录（仅合成数据）：父子相加 `Decimal(100) != Decimal(50)`；
平台平均 `0.852 is not None`；跨币种错误只剩USD桶；跨来源 `[60] != [20,40]`；
missing阈值错误出现c1；missing→0 `Decimal(0) != None`；D0合同0小时不等于24。
七条均已cmp恢复并同一测试GREEN。旧平台多日first探针也已重映射RED→恢复→1 passed。

最终回执：scope+dimension聚焦suite `78 passed, 1 deselected in 21.93s`。
随后只增强目录组合中的.7阈值交叉及Ad维度拒绝参数边界，定向结果见本轮末行。
实际skill入口四项（48状态、57组件、1,296+9+12生成、聚焦PG suite）均已运行。
Ruff check/format、diffcheck通过；逐文件读取精确起点Git对象与磁盘cmp等价核对
1115个backend/app及frontend/src受跟踪产品/迁移文件，字节完全相同。
提交仅含验证产物。未确认产品bug、无待修产品counterexample；root独立验收仍是下一门槛。

最后增强矩阵定向：`RUN python -m pytest backend/tests/modules/reporting/test_native_scope_roas.py -k 'directory_scope_matrix or rejects_unsupported' -q --tb=short`：10 passed/53 deselected，7.73s。
随后Ruff check全部四个本轮Python验证文件通过，diffcheck通过。
