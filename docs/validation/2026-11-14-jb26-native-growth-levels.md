# JB-26 三层 Native Growth 本地实现候选

起点 `7d987d8ebb3bad16b86dbf1e0becd5e7bff04829`，唯一写树 `projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a`，分支 `feat/jb24-native-growth-m4a`。Codex gpt-6.1-sol/high，唯一 writer，未派下级。未修改外仓 Spec/协调状态、Linear、main；未推送、PR、部署、生产迁移或广告操作。原 `.venv`、`node_modules` 软链保留且不提交。本文记录候选与未闭合验收，不是 Done 或整个 M4a 验收。

## 实现与 AC 对照

| JB-26 AC | 本片实现/证据 | 边界 |
|---|---|---|
| 两页 Adgroup 主 Seam、Campaign 可用、Ad 真实缺项 | `test_two_page_adgroup_worker_and_query`：真实刷新 HTTP → 原需求/控制派发、PG/Redis → 三层 Worker 函数 → 官方 SDK transport → PG → query/status HTTP → Vite/Playwright 工作区，Campaign/Adgroup 各40，Ad dash | 未运行独立 Celery broker 子进程；没有真实平台调用 |
| 请求类型/字段/维度 | `levels.py` 注册 BASIC/AUCTION 三层；Adgroup AUCTION_ADGROUP、adgroup_id/stat_time_day、Native Growth total/D0/平台 ratios；SDK 日 reader 与 evidence 共用注册 | Ad 保留原请求集合；未请求比率显示 NOT_REQUESTED，不标 ABSENT |
| 统一注册、原 owner、消费者枚举 | 下表；不新增执行器/资格 owner | 健康旧夹具的验收尚未全部闭合 |
| 三层获准需求、合并、越权 | 原 `/ad-sync-refresh`；完整 Ad 日需求登记两父级，重复同需求不增加三个 job；父级子集拒绝；三层需求均在 scope/outbox 前 verify_route | 需求重复合并通过真实 PG；全面撤权/交错重放沿 JB27 |
| 完整分页与过滤子集 | 原 `_read_all`/CheckpointReader；父级需求及 Worker 拒绝 campaign_ids；Ad 定向发布不推进桶版本，实际错误探针拒绝错误状态推进 | 截断父级沿既有账户/日期缩窗，不增加父级过滤发布 |
| 独立状态和页面 | kind 独立 observed_at/version/active_contract/digest/success/attempt；status HTTP 新增 typed levels；顶部三层完整数与成功/尝试，行内逐日状态/原因/合同/版本 | 原广告完整起始日仍是旧 feed；不能用它宣称三层完整 |
| 原子正值/完整空发布 | 共用 `publish_parent_day`，事实/证据/状态同事务；PG trigger 在 Adgroup state 更新时抛错，旧40和旧证据/版本全部保留；空桶清除 Adgroup direct 行并保留 COMPLETE/合同/digest，不影响 Campaign 版本 | R7 完整迟到/重放竞争未在本票宣称完成 |
| 父级各40、spend50、Ad dash | 三层各两页、各2次SDK请求；两个父级本地0.8，平台0.801234567890123456789独立字符串；浏览器真实切三层 | 每对象父级40，不把父子相加 |
| 广告发布/删除写入/投影/重建/恢复隔离 | `test_three_levels_independent_and_direct_survives_writers` 真 publish_ad_day（先dash后999）、qem 删除/写入、enqueue_fact_buckets/rebuild_pending；两个层级同ID各40，后续父级dash不回退999；Campaign/Adgroup各自账户恢复 trigger 测试 | Parent 物理表不进入旧 writer；未接自动规则/经营输入 |
| direct/derived 互斥、空发布权威 | `adgroup_selected_day` 与 Campaign 相同的对象日向量选择；直接桶存在时抑制整桶 derived，SQL过滤/排序/summary/行构建同源 | 全部子集/来源分桶计算由 JB28 加厚 |
| 原规则、经营、首页、creative/目录 | 未替换这些消费者的事实表/金额 owner；仅工作区 Campaign/Adgroup 使用 selected views；平台ratio未加入 METRIC_COLUMNS | 复用 JB25 未变路径与相关 metrics/publish/SDK 回归；未跑无关全仓 |
| additive、旧来源 | 新迁移 native_adgroup_day 仅新增 Parent 的父系列元数据列和只读 view，无旧金额 DML/历史迁移修改；Ad NULL evidence仍LEGACY_UNKNOWN（真实旧回归拒绝误追认后修复） | 本机新独立库从头迁移，未生产迁移 |
| campaign_day 辅助用途/旧 checkpoint | `_read_campaign_batches` 仍实时读 campaign_day 辅助清单，仅用于 Ad 截断拆分；主kind/方法/冻结route/日期/合同参与 checkpoint digest；Ad合同从report-v2改为日注册合同，旧断点不能沿用 | 未新增 parent→Ad 截断推断 |
| 错误实现探针 | state/subset/source/writer 四个实际源码探针，具体断言RED → finally逐字节恢复 → GREEN；另48状态模型和 broken-selector 反例 | source探针首版只改未命中的 SQL 分支仍绿，已换成真实行物化 source 再验证红，不计首版为拒错证据 |
| 独立可读与请求/状态/合并 | Campaign 先发布时 Adgroup/Ad state不存在，Campaign40仍可读；组失败保留旧40/STALE和原因，Campaign COMPLETE；三层主Seam6次请求，3个状态；重复需求3个job | 不新增三层全部成功才能读的门槛 |

## 消费者逐项核对

- manual/hot：原 API → plan_hot → enqueue_jobs；完整 Ad 请求按 PARENT_KINDS 登记父级，账户池、时区窗口、路由及执行权不变。
- daily：按 DAY_KINDS 加既有 creative/account kind 规划，原 requirement 合并；history：原 Ad 历史入口同样登记父级。
- closeout：原昨日账户/活动窗口，kind 候选由 DAY_KINDS 加 creative 生成；lane live/closeout 同样来自注册。
- missing/confirmed-empty：工作区 row/snapshot 覆盖按 query_kind/state_sql 选本层；开销筛查空账户只能确认旧广告/creative/account，不可证明父级收益无行，父级仍必须完整读取。旧 complete_data_start 是广告 feed 的历史边界，不充当三层 COMPLETE。
- freshness/health：FRESHNESS 三层独立；健康遍历日注册；已登记父级 scope 单独观测缺失/旧水位，父级修复按自己的 kind，不因组过期重拉成功 Ad。原广告/creative/目录修复输入继续独立。未登记父级的旧 scope 不自动追认收益状态，工作区明确 MISSING。
- demand：注册合同、原 SyncScope/requirements/head；三层权限在写 scope 前核验。endpoint 仍原 reports.integrated，无额外通道或预算域。
- outcome：原 `_set_state`/satisfy_requirements，kind独立；Ad子集只满足原冻结精确需求，不写整桶状态。Ad完整发布补齐自己的合同/digest/证据观测版本。
- cleanup：原 BASIC_KINDS 统一包含三层，完整后按job删除 checkpoint 正文；原 report compaction 不按 Native kind 分叉、保留事实/水位，相关真实PG锁回归通过。Parent普通表不参与月分区DDL，未新增数据删除策略或清空证据。

## 环境、命令和结果

先读 config/README；复用既有依赖与本 MMF 专用 loopback Redis，未 FLUSHDB。新增独立本机 PG `tkada_native_m4a_jb26_v1_20261114_test`，从空库 Alembic head 迁移；验证迁移初版缺派生view的nullable标记/列时修复未提交的新迁移，最终独立库使用完整形状。未安装依赖。runner：仓外 `/tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py`；环境值不打印、不提交。

从本树根：

```bash
python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py python -m pytest backend/tests/modules/reporting/test_native_levels.py -q --tb=short
python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py mypy --config-file backend/pyproject.toml backend/app/modules/reporting
python3 .agents/skills/formal-verify-reporting-data/scripts/check.py
cd frontend && bun run build
```

- 最终新增 levels：6 passed/7.89s，包括真实刷新 HTTP、6次SDK请求、原Worker函数、三层实际页面与状态API。后续增加未请求比率显示及空发布断言，目标 independent 检查再通过1 passed/0.69s。Campaign/native+dimension阶段31 passed/22.32s；最终 nullable派生标记修正后dimension17 passed/2.05s。
- 扩大相关 metrics/publish/catalog/SDK/compaction阶段83 passed、1 failed；失败是新增Ad active_contract误把 evidence=NULL旧行解释为新合同，已修复；上述最终native检查包括原断言通过。未改旧断言。
- frontend正式及demo build通过；Ruff scoped check/format和diff check通过。严格mypy未全绿：本片最终410条、与精确起点归档的412条规范化比较无新增。另精确起点独立源码归档（非checkout）配置mypy412条，当前410条，诊断集合比较无新增，仍须按诊断集合比较，不把数量减少等同全绿。曾使用follow-imports=skip得到不同542/545基线，不能混作配置命令的结论。
- 原 planner/health 大套件仍有失败。精确起点7d987d8独立归档＋独立PG `tkada_native_m4a_jb26_baseline_20261114_test` 复现8个旧任务种类/数量断言失败（预期Ad/creative两类，JB25实际已有Campaign）；本片再增加Adgroup，未修改这些旧断言。全量迭代一次29 failed/68 passed还包含父级未准备水位的旧健康夹具，以及本片已修复的修复集合耦合。最后按实际修改重跑健康目录/跨午夜/SLO目标：33 passed、1 inherited数量断言失败；完整健康大套件未在最终源码全绿，正常热轮夹具只种Ad/creative却期待无任何issue的断言仍需按三层合同审定，不冒称这些新增合同失败均为基线。此项留root验收，不自动改既有断言。
- 外仓 `jb26-probes.py` 和 `/tmp/jb26-probe-{state,subset,source,writer}-{red,green}.log` 保存实际源码消融及恢复证据。模型48状态通过，broken-selector实际失败；模型只表达R4/R10有限假设，不代替PG或R7。

## 继承边界与交接

JB27消费：LEVELS/PARENT_KINDS/DAY_KINDS、ParentReportDay、publish_parent_day、三层独立状态/Ad evidence stamps、status levels DTO、现有PG桶锁/checkpoints/执行fence。继续做R7完整反序竞争、同观察不同摘要、重放no-op、失败页/重试及冻结路由等可靠性验收。本票没有完成这些或独立Celery进程投递。

JB28消费：campaign_selected_day/adgroup_selected_day、query_kind/state_sql、report_provenance、SQL过滤排序路径；完整子集范围/分桶金额/快照冻结/详情CSV仍按后票合同，不把本片页面结果冒充冻结快照完成。平台ratio是Decimal字符串观察，money只持有于Numeric，不增加证据money副本。

仍需root独立验收：完整消费者行为回归（上述旧健康夹具/新合同差异）、正式Standards/Spec审查；本票局部形式检查不覆盖全MMF。不得据本文标Done、合main、开放后票或声称M4a完成。没有生产验收结论。


## Root 消费者阶段验收与收口

root亲核2662522d9f1020d49206322c9e4704260b358342，统一注册/独立state/需求与health/closeout接线；主线程实跑发现旧planner/health断言及fixture仍按Ad+creative两类。按已批准三层合同更新明确预期，未降低水位、授权、去重或原子性要求：

- hot/daily/closeout明确包含Campaign、Adgroup、Ad及creative（daily另保留account）；昨日两个时区的实际8个任务按账户×kind逐项核对。Ad已闭日成功时，两个未观测父级仍须独立补拉。历史dedup六状态夹具对四kind均准备匹配/不匹配scope，不用遗漏父级任务制造假失败。
- 正常热轮fixture先完成实际登记的三个日任务和三层水位，验证整轮不增加任务；父级过期负向检查仍必须返回独立stale/repair，成功Ad不能掩盖它。
- 锁竞争fixture原先锁report-v2，已改为当前native-growth-day-v1精确scope；500ms数据库预算内仍记录健康、锁释放后可补修，原断言保留。
- 新登记父级scope会带来第二次真实告警变化；事件数检查保留了相同issue集合不重复发事件，四层成功后恢复HEALTHY。

发现一处本票真实消费者回归：未知账户的修复此前隐含借plan_hot触发creative；拆分kind修复后漏掉creative。root恢复unknown范围同时进入原广告/素材修复集合（父级随完整广告需求登记），不新增owner、不写成功水位。新合同检查先因缺creative实际失败，修复后绿。

实跑结果：
1. health+planner（除正常热轮）83 passed/119.37s，包括真实scope锁、时区边界、查询索引历史噪声等。
2. day_closeout＋历史dedup六状态/八种scope＋正常热轮74 passed/18.17s。
3. 最终health关键缺失/未知/锁竞争3 passed/1.04s。
4. root另一批运行中native_levels六条及health status/known-scope等通过；该批的失败均已被上述明确scope/数量夹具收口，未把整批称全绿。三层主Seam保留真实SDK transport→HTTP/Worker函数/PG/工作区证据。
5. 两个真实源码探针：去掉未知素材探测、忽略父级freshness，均行为RED→finally字节恢复→GREEN。外仓jb26-root-*-red.log/green.log；未重复未改的四个旧探针。48状态R4/R10模型正常通过，broken-selector命中direct-authority反例；不覆盖完整R7。
6. Ruff check/format和diff check通过。root没有改前端，复用匹配前端源码的build/TypeScript证据；未声称严格mypy全绿，未运行全仓套件。

当前可作JB-27/JB-28前置集成基线，票保持InProgress直至完整MMF验收合main。正式两轴审查、独立Celery投递、R7完整可靠性、快照/CSV仍待后票，不宣称整个M4a完成。
