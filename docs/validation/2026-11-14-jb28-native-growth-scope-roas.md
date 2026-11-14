# JB-28 scope/ROAS — WIP, not accepted

Start: `d72d5837967fc5619f2dcbd8f5a577f2a94f6ae1`.
Branch/tree: `feat/jb24-native-growth-m4a`, existing shared integration tree.
Only this ticket changed; no Linear writes, child agents, model changes, checkout,
main merge, push, PR, deployment, production migration, or real platform calls.
This is a partial local implementation, not completion of JB28 or M4a. JB29 must
not start on this evidence. No external Spec/coordinator files were changed.

## Implemented portion

- Daily ReportingData SQL preserves denominator-null flags through rb/rv, so
  filtering and ordering cannot compute a ratio from an incomplete divisor.
- MetricVector and SQL summary distinguish numeric denominator gaps from a stale
  coverage flag. Known revenue retains the entire available same-scope spend:
  20/(10+90)=0.2 PARTIAL; missing spend returns MISSING; zero spend returns NULL.
- Account/drama source routing now reads Campaign selected-day facts, including
  direct authority, rather than the original derived-only shortcut. Existing
  Campaign membership/naming grouping remains. This switch is not yet safe to
  accept for mixed source/contract cases: separation and aggregate provenance
  are pending. Do not release this intermediate commit independently.
- Material query ratios keep their existing behavior; strict denominator logic
  is explicitly enabled for the daily object/account/drama path. Business and
  automation inputs, STORED_KEYS/DERIVED_RATIOS, and derived writers are untouched.

## AC matrix (numbering follows full JB28 ticket)

| AC | Evidence/status |
|---|---|
| 1 same plan across dimensions | Partial: real PG Campaign threshold/row, Account row+summary; full five-view demonstration pending |
| 2 whole-object attributes | Existing selector retained; dedicated complete matrix pending |
| 3 keyword scope text | Pending UI explanation and semantic audit |
| 4 subset cost5 / full-parent40 | Pending; no implicit child meaning added to ids |
| 5 direct authority / old contracts | Existing dash/empty regression passes; old/mixed contract separation pending |
| 6 account/drama Campaign set | Source routing implemented; mixed-source and aggregate provenance validation pending |
| 7 10/90 +20/90 | Real PG summary 1.1 passes; explicit parent-child-addition mutation pending |
| 8 partial/full/zero divisor | Real PG 0.2 PARTIAL, missing divisor MISSING, zero NULL pass; ZERO_DENOMINATOR reason pending |
| 9 compatible buckets | Existing currency separation passes; source/contract/window bucket coordinates pending |
| 10 platform single observation | Previous implementation retained; aggregation observation expansion pending |
| 11 .801 versus .8 | Existing SDK/levels seam retained; new explicit independent comparison pending |
| 12 D0/calendar/hour | Previous contract retained; R8 scope model and new cases pending |
| 13 NULL threshold/order and visible partial | SQL rejects missing divisor; UI reasons/partial display pending |
| 14 old consumers | No business/automation owner changed; material regression31 includes old behavior; explicit consumer regressions pending |
| 15 PG plan/performance | SQL arithmetic exercised on real PG; EXPLAIN, first/page timing and source-scope plan evidence pending |
| 16 mutations | Three executed; remaining required subset/source/currency/ratio/addition/missing-zero probes pending |
| 17 formal scope/Decimal | Six bounded arithmetic examples only; exhaustive R2/R5/R6/R8 model NOT implemented |

## Actual commands and results

Commands run from integration root. `RUN` below means
`python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py`.
Dedicated MMF guarded loopback PG/Redis; no env printed, no dependency reinstall,
no FLUSHDB. The first two test invocations briefly overlapped by mistake; they
are not the final serial evidence below.

- Initial real PG regression: two intended failures, missing divisor computed11;
  Account direct110 was absent. A first fixture typo used snapshot.summary and
  failed AttributeError; corrected to public page.summary before judging red.
- `RUN python -m pytest backend/tests/modules/reporting/test_native_scope_roas.py backend/tests/modules/reporting/test_dimension_query.py backend/tests/modules/reporting/test_native_levels.py backend/tests/modules/reporting/test_native_publication.py backend/tests/modules/reporting/test_publish_watermark.py -q --tb=short`: **107 passed**, 17 prior deprecation warnings,42.42s, serial.
- After explicitly preserving material legacy ratios: `RUN python -m pytest backend/tests/modules/reporting/test_native_scope_roas.py backend/tests/modules/reporting/test_material_query.py backend/tests/modules/reporting/test_dimension_query.py -q --tb=short`: **31 passed**,4.44s.
- `python3 .agents/skills/formal-verify-reporting-data/scripts/denominator.py`:
  six bounded examples PASS; broken-denominator gives expected counterexample.
- `python3 .agents/skills/formal-verify-reporting-data/scripts/denominator_probes.py`:
  Python incomplete-divisor mutation RED (11), SQL mutation RED (wrong threshold
  member), account-derived-summary mutation RED (110 absent); each restored bytes
  from external backup in finally and reran six green. Account probe initially
  survived because test inspected row only; added independent summary110/spend100
  expectations, then mutation failed. No expected money was weakened.
- Scoped Ruff check/format PASS. No frontend source changed; reuse matching
  JB27/JB26 build evidence. Current real SDK/HTTP/PG/Redis/worker/page seam ran via
  native_levels in107; this does not claim new UI AC implementation.
- `RUN mypy --config-file backend/pyproject.toml backend/app/modules/reporting`:
  still410 errors. Exact start backend was extracted with git archive into an
  external temporary source archive (no new Git checkout) and same command run;
  normalized diagnostics (line numbers removed) have no additions/removals.
  Strict mypy is not green. Archive extraction required original3.14 interpreter;
  system Python lacked extractall(filter=...). Softlinks were preserved.

One existing test expectation changed with an explicit independent contract:
`test_summary_counts_incomplete_advertisers_and_dates` expected PARTIAL ROAS
although one scope account's spend is unknown. JB24 R6/JB28 AC8 require missing
ratio, so the test now asserts NULL and MISSING while retaining partial spend10,
row count2, and incomplete account/date assertions. The new real PG negative
threshold test and both denominator mutations reject calculating that ratio.

## JB29 handoff is NOT ready

Do not freeze the current payload as the completed M4a contract. Existing
MetricVector values/availability/provenance, ReportRow.display and three-layer
query dependency remain available, but source/contract/scope bucket coordinates,
aggregate provenance, per-observation platform ratio data, and explicit reasons
still need JB28 completion. JB29 owns frozen_metrics/contract/publication-map
persistence and old-snapshot handling; JB30 owns CSV. This work adds neither and
makes no claim of values frozen across refreshes. Remaining ACs above must be
implemented and accepted before the root opens JB29.

## Fresh-context continuation from 854b86ec (still not accepted)

Authorized Sol/high, same branch and shared integration tree, no child agents.
Initial HEAD matched `854b86ec8800b39f9ccbfe26223f400f4b18c9f9` exactly;
`.venv` and `node_modules` untracked symlinks retained. No other checkout, main,
Linear, external coordinator files, real platform, push/PR or deploy touched.

### Changes and initial failures

- Real PG independently reproduced direct Campaign40 + next-day child20 merging
  into60. `_facts_cte` now groups level/source/contract/currency before row bounds,
  sort and summary. Page hydration calls the same SQL facts builder with page IDs
  pushed into the fact predicate, replacing its separate daily `_aggregate`
  path. Account/drama follow selected Campaigns. Material retains its own path.
- `MetricVector` adds optional `level`, `source_kind`, `contract_revision`, and
  `reasons`. `bucket_key` preserves these coordinates when merging. Unknown direct
  contracts remain separate and amounts are NULL/CONTRACT_MISMATCH; thresholds
  cannot admit them. `semantics_for` currently explains only native-growth-day-v1;
  a historical interpretable revision registry is not implemented.
- `ReportingFilter.ids` remains current dimension/whole-object filtering. New
  canonical `AdContributionScope(kind='ad', ids=...)`, only allowed for parent,
  account and drama dimensions, restricts AdReportDay before aggregate SQL.
  GET ads exposes `ad_contribution_ids` separately. Filter digest includes it.
  Independent PG test keeps selected Ad spend5 with parent revenue40 inapplicable;
  no parent40/5, no expansion to parent spend50. Existing material contribution
  queries remain separate; creative/material scopes are not added to this union.
- Platform0.801 and local40/50=.8 remain separate. Multiday platform value is NULL
  NON_ADDITIVE, with expandable per-day fields/Decimal strings. This check publishes
  synthetic rows via the real publisher, not SDK; the existing levels harness
  separately exercises SDK transport → worker → PG/Redis → HTTP → browser.
- UI now states “匹配对象的全部数据”, explicit Ad subset reason, bucket coordinates,
  visible “部分” labels, ratio missing/zero reasons and expandable observations.
  Summary labels source/level/contract when buckets split. No frontend formulas.
- First extension fixture used invalid ad_row keyword `campaign_id`; corrected to
  its existing `campaign` argument before judging red. Plan experiment first used
  snapshot.id instead of snapshot.snapshot_id; corrected without changing expected
  money. An inherited exact report_source assertion caught replacing its enum
  with prose: restored enum and moved detailed coordinates into report_coordinates.
- Parent empty page fallback now keeps all amounts NULL instead of fabricating
  spend0; its full source/contract empty-bucket provenance still needs closure.

### Executed evidence

`RUN` is the guarded dedicated local runner defined above. Suites run serially;
no FLUSHDB, reinstall or other-process termination. Build is independent of PG.

- Initial focused source regression RED: expected two buckets20/40, observed60.
  Initial explicit subset check RED: canonical filter rejected unknown contribution.
- Focused daily/material/dimension checks33 passed before subsequent additions.
  Added unknown-contract and platform-observation checks: scope suite10 passed.
- Broader six-file run:118 passed/1 failed solely for changed report_source enum;
  fixed the product field rather than weakening its old assertion.
- Four new scope source probes: concrete assertion RED, finally byte restoration,
  then GREEN each. The first source-collapse probe caused an invalid SQL GROUP BY
  constant; rejected as evidence and replaced with a valid SQL expression. The
  final probe detects wrong monetary buckets, not an SQL exception.
- Three inherited denominator probes rerun after refactoring: Python divisor11,
  SQL erroneous threshold membership, account derived summary missing40/110 all
  rejected; external backups/finally/byte equality and scope-suite11 green each.
- Actual `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` on two synthetic Campaigns:
  members planning3.187ms/execution1.222ms; summary1.086ms/.293ms. Real query first
  page39.214ms, cursor page13.887ms; stable order c0,c1 and summary1.1. Full synthetic
  plans are `2026-11-14-jb28-plan-synthetic.json`. This is a small plan experiment,
  not production capacity evidence. A representative large-cardinality plan is pending.
- Mypy410 errors; normalized diagnostics equal inherited `mypy-final.log` (zero
  additions/removals). Strict type checking remains non-green. No claim of a new
  exact-start archive baseline; the previous author's exact-start comparison is inherited.
- Build/tsc and final six-file suite results are appended below after completion.

### Superseding AC matrix and next work

| AC | Current evidence and remaining boundary |
|---|---|
|1|Same facts SQL feeds source grouping/bounds/sort/summary/page hydration; five-view SDK/browser demonstration still incomplete.|
|2|Existing whole-object selectors preserved; full attribute-filter matrix pending.|
|3|Visible whole-object scope; explicit Ad contribution filter implemented. Material/creative real-entry audit and all scope combinations pending.|
|4|Real PG parent40/selected Ad spend5 regression green, matching source probe rejects ignored filter. HTTP/browser subset demonstration pending.|
|5|Dash/empty authority inherited; unknown direct contracts NULL/CONTRACT_MISMATCH added. Complete-empty coordinate ownership and old-contract cases need closure.|
|6|Campaign-only account/drama source grouping regression green; aggregate provenance and ambiguous naming audit pending.|
|7|Summary1.1 and inherited three-level anti-overwrite green; explicit cross-layer addition probe pending.|
|8|Complete/partial/missing/zero divisor cases and reasons implemented; full known/missing count provenance pending.|
|9|Source/level/contract/currency buckets separate; timezone MIXED inherited. Explicit per-range/window/count aggregate provenance pending.|
|10|Single/multiday platform Decimal regression and first-day-as-multiday probe green; aggregate account/drama observations still pending.|
|11|Explicit .801 vs .8 regression green; SDK/HTTP/page precision harness inherited and rerun.|
|12|D0 existing labels unchanged; generated midnight/calendar/hour model and matching product regressions pending.|
|13|Visible partial/scope/reasons added; null threshold regression green; five-view missing/same-name/paging UX demonstration pending.|
|14|Material tests green; consumers unchanged. Explicit full named-consumer regression audit pending.|
|15|Real small PG plans and actual first/cursor page evidence added; representative scale and full vector/provenance plan evidence pending.|
|16|Seven source probes executed (three inherited, four new); cross-layer addition, average ratio, currency, cross-source division, missing-to-zero probes still pending.|
|17|Existing six examples only; generated R2/R5/R6/R8 model remains unimplemented.|

Continuation must start by finishing actual provenance, complete-empty coordinates,
consumer/entry audit and full required formal model, then finish the five-view
live harness. Do not mistake these tests for all17 AC closure. The current session
reaches a context boundary with these explicit gaps; preserve this checkpoint
for a fresh implementation context. JB28 remains unaccepted, JB29 must not start.

JB29 can inspect the added MetricVector coordinates/reasons and canonical
ReportingFilter contribution, but **must not freeze them as accepted contracts**:
aggregate provenance/stamps/counts/windows are incomplete. Snapshot persistence,
publication-map freezing and detail/page immutability still belong to JB29;
CSV remains JB30. No migration or those consumers were implemented here.

Final continuation checks: six reporting suites **120 passed**,20 existing
warnings,37.48s, serial, after all source probes restored. Exact command:
`RUN python -m pytest backend/tests/modules/reporting/test_native_scope_roas.py backend/tests/modules/reporting/test_dimension_query.py backend/tests/modules/reporting/test_material_query.py backend/tests/modules/reporting/test_native_levels.py backend/tests/modules/reporting/test_native_publication.py backend/tests/modules/reporting/test_publish_watermark.py -q --tb=short`.
Scope suite contains11 tests including the SQL/paging experiment. Existing
native_levels real HTTP/worker/PG/Redis/browser harness ran in this120; it is
**not** the missing five-view/subset/partial UI scenario. Formal/demo frontend
`bun run build` (including tsc) passed again after final UI changes. Seven changed
Python/script files passed Ruff check and format; diff check passed. No final
claim of exhaustive formal coverage, whole-ticket acceptance or production use.

## Superseding AC matrix — 658e7700 continuation product candidate

本轮起点精确 `658e7700b905c5394a0df480b31ed4c8f4867ffd`，原分支/共享树串行单写，Sol/high，无下级代理。保留原 `.venv` / `node_modules`。本节覆盖上一份“继续实现”的产品缺口清单；**不是 JB28 整票接受、全面形式验收或 JB29 放行**。root 仍须独立审查。

### 本轮产品合同

- `row_set._facts_cte` 继续唯一决定来源、成员、阈值、sort、summary 与页向量；行证据合同与发布合同冲突时独立 `CONTRACT_MISMATCH` 桶并清空金额。已知 `LEGACY_UNKNOWN` 保持原 derived 映射；registry 只有 `native-growth-day-v1`，没有杜撰历史 native revision。未知直接 revision 不解释金额或平台比率。
- 同层/source/contract/currency 分桶；window 由每指标的 `metric_contract` 明确，当前入口只有账户本地日 native 合同，没有可混进此查询的 Calendar/hour 参数。每指标保留 known/missing、分母 known/missing、公式/原因、实际本地日/时区范围、逐对象日观察和逐桶水位。`vector_provenance` 是行与 SQL summary 共用解释函数；金额合计不移入 Python。
- Account 从同账户 selected Campaign 集合去重，Drama 从已选 Campaign refs 去重；不会加入 Adgroup/Ad/creative。页面 Account/Drama 同样显示来源、缺项/部分、窗口与可展开的原始逐日平台 Decimal；多对象/多日平台值 NULL/NON_ADDITIVE。
- 原 `report_sync_state` 新增 nullable `publication_coordinates` JSONB，迁移 `native_publication_coordinates`（承接 `native_adgroup_day`）。三层原 publisher 随事实事务写入实际币种/时区/本地日/level/source/contract/scope/window；空发布摘要包含坐标。查询在同 SQL 计划补 NULL 对象日，不能用账户现值或旧 derived 替代空发布。历史无坐标不回填，明确 UNKNOWN。现存有金额行继续使用原行坐标。迁移只在专用本地 PG 执行；没有生产迁移或部署。历史旧摘要同观测重放不追认新增坐标，需新观测刷新；旧程序回滚不能默默读取新合同。
- 现有 `AdContributionScope` 接上生成客户端、GET transport 和页面“广告贡献子集 ID”；选 Ad1 保留 cost5，父收益40不适用，收益/本地比率均缺项。切换到 Ad/素材清掉不适用子集。`ids` 未重定义：仍沿现有本层整对象/层级属性选择，不成为隐式广告贡献子集。

### 实际入口与消费者 owner 审计

`AdsWorkspace.navigateToChild/filterTo`、`search.parentPath` 与 `row_set._dir_hits/_dimension_rows/_filter_conditions` 是现有父路径/ID/名称/状态/预算/创建日/阈值入口；父约束选择本层对象，然后读本层向量。query 的多词 AND 是对象搜索，页面明确匹配对象全部数据。素材经济入口仍为独立 material 维度；广告详情素材页显示本广告素材，不存在把 creative/material 条件提交给 Campaign direct 的父贡献入口，本轮没有发明这种入口。

- 素材：`material_query.materialize_material_rows` 与 creative_query 原 source/公式不改；定向 material suite 随本轮39项通过。
- 自动规则：`automation_rules/inputs.py:406` 仍读 `ad_report_day` / `creative_report_day`，evaluate 仍用原 `STORED_KEYS/DERIVED_RATIOS`；没有接入 selected parent 或平台 ratio。
- 经营：`business/projection.py` 仍读旧 `CampaignReportDay`，未接新 direct。首页 `WorkspaceEntry` 与 `BusinessDashboard` 的 BusinessService 未更改。
- MINI_APP、creative、目录管理：原报表存储/目录 owner 与 SDK 请求指标保持原输入，未由 native 收益判资格。上述除 material 外是明确源码审计证据，不冒充全消费者动态回归。

| AC | Superseding evidence / remaining verification |
|---|---|
|1|产品实现；同一 SDK→Worker→PG/Redis→真实 HTTP/页面依次显示五视图；成员/阈值/summary/续页共用事实 SQL。|
|2|现有 ID、父级、名称、状态、预算、创建日 owner 源码审计；dimension suite定向通过。完整组合生成矩阵留 fresh 验证。|
|3|页面 whole-object 文案及真实独立 Ad 子集输入/传输/切层清除通过；无 creative/material 父贡献入口，审计如上。|
|4|真实 PG parent40/Ad5继承回归，本轮真实页面 cost5/收益dash/ROASdash通过，未40/5。|
|5|空桶实际时区与账户现值不同、历史无坐标UNKNOWN、未知发布合同、行/桶合同冲突均有PG回归；无逐指标derived回退。|
|6|Campaign集合去重与已有命名投影保留；Account/Drama provenance/原观察/计数新增回归；歧义名按既有external独立行，不重复归剧。|
|7|20+90 / 10+90=1.1等既有真实PG回归保留；没有跨层相加。专属父子相加源码探针留下一验证轮。|
|8|完整/部分/缺分母/零分母原回归保留；新增 row/summary known/missing及完整分母计数。|
|9|同source/contract/currency/level SQL桶；逐指标window/公式与各账户日/时区/水位可解释；跨时区沿原MIXED规则。|
|10|聚合平台NULL/NON_ADDITIVE与原逐日Decimal在Account页面真实展开；所有五视图共用证据。|
|11|平台.801…与本地.8分别保留；独立PG测试及SDK/HTTP/页面原精度断言通过。|
|12|D0点击后24h、total截止未知；Calendar/hour入口不能借日列。完整生成R8模型/跨午夜反例绑定仍待fresh验证，未称形式通过。|
|13|账户部分收益、两条同名Campaign且ID不同、Ad缺项、旧观测失败提示实际页面通过；HTTP limit1首/续页通过。同一SDK fixture随后发布52个Campaign，浏览器实际50/2条下一页/上一页、跨页身份无重复通过。|
|14|material定向回归通过；其余named consumer明确源码审计，无新增direct执行决策入口。|
|15|2,000 Campaign/单账户/单日合成PG，首查3491ms、续页1300ms；members执行1406ms、summary1448ms；完整JSON见同目录plan-representative。非生产容量，非多租户/多日负载。|
|16|继承原7探针（源匹配的部分）；本轮两个产品探针实际断言RED/bytes restore/GREEN。剩余完整源码探针矩阵交下一验证轮。|
|17|未做完整R2/R5/R6/R8生成模型；仅继承六例，不宣称全面形式验收。|

### 实测、初始失败与继承边界

RUN = `python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py`，全部 PG/Redis fixture 串行，无FLUSHDB/依赖重装/真实TikTok调用。

- `RUN python -m pytest backend/tests/modules/reporting/test_native_scope_roas.py backend/tests/modules/reporting/test_dimension_query.py backend/tests/modules/reporting/test_material_query.py -k 'not representative' -q --tb=short`：最终39 passed/1 deselected，5.99s；随后只加历史空坐标定向回归与未知比率缺项断言，分别补跑。
- 新 publication 坐标与原发布行为：scope+native_publication（排除representative）68 passed/18.89s；正常重放/冲突/丢fence未弱化。
- `RUN python -m pytest backend/tests/modules/reporting/test_native_levels.py -k 'two_page and False' -q --tb=short`：最终分页场景1 passed，22.58s，22既有warnings；真实SDK transport→原Worker/HTTP→PG/Redis→浏览器，五视图、同名IDs、账户部分/原比率展开、Ad子集以及切层清除；52个Campaign的实际浏览器50/2条翻页与返回。没有mock service/browser response。浏览器由已有harness驱动，无新增五套环境。
- 代表基数：`RUN python -m pytest backend/tests/modules/reporting/test_native_scope_roas.py -k representative -s -q --tb=short`：1 passed/9.85s，JSON计划和时间入库。首版相关子查询有19.4s首查，换为证据JOIN后3.49s；未隐藏初始慢查。
- `python3 /tmp/jb28-product-probes.py`：空桶借账户时区、删除聚合证据两条实际源码探针，均 AssertionError RED，外部临时备份/finally/cmp字节恢复，分别1 passed GREEN。首个时区探针曾存活，因为测试发布时区恰与账户时区相同；修成独立Los_Angeles发布/原Hong_Kong账户，再拒绝错误代码，没有减弱预期。
- 初次PG新增测试发现账户refs为空，改用selected Campaign集合；排序改变scope digest导致原dimension等价断言失败，去掉sort字段；无事实向量缺坐标导致DTO失败，补明确未知derived坐标。行合同审计最初错误访问projection缺失的evidence列，改canonical evidence JOIN。发布坐标摘要起初混合dict/tuple不可排序，改稳定坐标tuple后原发布68项绿。fixture缺ad_type已修，不当作产品RED。追加52对象SDK发布后，原后续Adgroup隔离断言初次仍对照旧Campaign水位而失败；先明确断言新发布row_count52/version+1，再以刚发布的Campaign作隔离对照，未放宽Adgroup恢复不改变其他层的预期。
- 严格mypy最终仍410条，归一化对继承mypy-final无新增。Ruff check/format、diff check、正式/demo build（含tsc）通过。没有重跑旧120全量或原7探针，也没有精确起点archive重做。

### 精确JB29消费合同与fresh验证交接

**JB29未放行。** 可评审字段：MetricVector `currency/timezone/attribution/level/source_kind/contract_revision/values/availability/reasons/provenance`；MetricProvenance `level/source_kind/contract_revision/scope_digest/buckets/field_state_counts/reason/formula/known_count/missing_count/denominator_known_count/denominator_missing_count/local_ranges/observations/metric_contract`。BucketStamp逐账户/层/日保存contract/publication_version/observed_at/published_at/last_attempt_status。发布新字段为 `ReportSyncState.publication_coordinates`；明确贡献为`ReportingFilter.contribution`，GET为`ad_contribution_ids`。ReportRow.display平台值及可展开JSON观察只展示，不加入本地可加量。

本轮没有冻结快照指标、publication map持久化、详情不可变或CSV（JB29/30所有权）。完整发布坐标与指标合同应由JB29在其获准后冻结；旧无坐标明确UNKNOWN不能借当前账户元数据。

下一fresh上下文只做验证/必要发现修复：
1. ReportingData formal owner：在 `.agents/skills/formal-verify-reporting-data/scripts/denominator.py` 生成R2/R5/R6/R8的完整有限状态（NULL/DASH/ABSENT/zero/部分、level/source/currency/contract/scope/window/账户日与跨午夜）。正常模型通过；每条反例绑定本文件真实PG测试，不能用六例替代生成范围。
2. 同owner `denominator_probes.py` / `scope_probes.py`：补父子相加、平均平台比率、跨币种、跨来源相除、missing→zero/阈值；继承完整分母与subset/source探针按当前源码映射审定。所有源码破坏仍外备份/finally/cmp，实际断言RED，不以SQL异常算捕获。
3. AdsWorkspace验证owner：实际52对象浏览器翻页已通过；剩余完整组合属性/父级scope与NULL阈值/排序生成矩阵，复用上述owner和PG回归，不另起产品实现。
4. 复核代表基数JSON：同一SQL selected来源决定成员/filter/sort/summary/向量；证据序列在SQL中生成，只把本页/汇总需要的provenance带回。没有生产容量结论；更大经营范围按真实风险追加，不无限benchmark。

产品实现候选已包含本轮要求的功能owner；全面动态/形式验收仍有上述明确缺口，不能称17AC全过或自行关闭票。无Linear/Spec改写、JB29实施、main合并、push/PR/deploy/生产迁移/真实平台或广告写。
