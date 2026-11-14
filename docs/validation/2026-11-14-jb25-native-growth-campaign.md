# JB-25 Campaign Native Growth 本地交付

起点 `b0f3b1443e7fedc19f297d078a706d5f8ef50040`；分支 `feat/jb24-native-growth-m4a`，唯一产品写树为 `projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a`。Codex gpt-6.1-sol/high 串行执行，未派下级。保留 `.venv`、`node_modules` 原软链；未推送、开 PR、合 main、改 Linear、部署或访问真实广告平台。

## 行为与 owner

- 现有完整账户 Ad 日需求同时登记 Campaign 日需求，仍由原 SyncScope、requirement、dispatcher、网关、Redis 准入和 reporting Worker 执行；定向 Campaign 子集的 Ad 读取不冒充完整父级读取。Campaign 需求写入前使用既有 `verify_route` 校验当前租户、操作者、BC、连接与账户授权。
- 官方 BASIC/AUCTION Campaign 日请求采用 `campaign_id/stat_time_day`，读取 Native Growth total、D0、平台 total/D0 ROAS。小时请求仅保留成本/计数，不请求日限定 Native Growth 收益。
- 同一 ReportingData 新增 `ParentReportDay`：主键 tenant/account/level/remote_id/local-day，NUMERIC 窄列、必需证据 JSONB；Campaign 完整分页后在原账户桶锁、执行围栏和事务内替换并发布状态。证据仅保存指标原字段、字段状态、Decimal 字符串、请求合同、观测、版本和账户本地日，不保存凭据或原始响应行。
- 保留 Ad 原事实、金额和旧 Campaign/Adgroup 派生 writer。仅给 Ad 增加 nullable evidence；历史 NULL 不补造证据，读为 LEGACY_UNKNOWN。父级不下摊。
- `campaign_selected_day` 是只读 SQL view：已发布 direct 桶整向量权威（包括缺项和完整空）；没有 direct 发布才采用原 derived。SQL 阈值、排序、summary 和当前页金额使用同一选择源；NULL 不通过阈值并排最后。目录对象不造收益零。
- Campaign 覆盖也按相同对象日选择 direct 状态，避免完整 Campaign 被缺失 Ad 状态误标 PARTIAL。该选择在 SQL 与行构造中共用 `CAMPAIGN_STATE_SQL`。已有 direct 的失败状态仍保留旧事实/旧观测，不转用子级。
- MetricCatalog 是合同修订的唯一常量 owner。目录 HTTP 返回原字段、层级、单位、来源、窗口、聚合规则和定义引用；total/D0 交通口径 UNCONFIRMED，D0=点击后24h，total 成熟截止未知。平台比率仅单对象单日观察，本地 ROAS 为同范围金额/花费。
- 公开契约采用 Spec 的 `MetricContract`、`BucketStamp`、`MetricProvenance` 类型，包含 contract_revision、scope_digest、逐桶水位、字段状态计数、reason/formula；平台观察有独立 SINGLE_OBSERVATION_ONLY 合同。
- 工作区系列名称下常显来源、账户本地日、窗口、观测时点、UNCONFIRMED/非结算及平台比率；表内 ROAS 明确为本地计算。平台长小数原样字符串展示，不转 float。

## 主 Seam 与测试环境

依照 config/README，仅迁移独立本机 PostgreSQL `tkada_native_m4a_jb25_v4_20261114_test`（早期迭代在独立 v2/v3 test 库，最终形状在 v4 从头迁移）。复用既有 venv，无依赖重装。另启动一台仅绑定 127.0.0.1、无持久化的专用 Redis；application 与 test 分别使用非零数据库，TikTok app scope 为每次随机测试标识。未使用共享 M3 数据库、未 FLUSHDB。环境文件、日志及恢复备份只在 `/tmp/tkada-native-series-20261112/m4a-implementation/`，不入仓。

主测试只替换 `urllib3.PoolManager.request`：合成两页 HTTP 等价回执 → 真实官方 SDK → 原 SDK Adapter/网关与 Redis → 原 `run_job_step`/执行围栏/PG → 原查询 HTTP/缓存构建 Worker → Vite/实际工作区。认证测试宿主绑定合成用户和独立 DB；没有浏览器 route/fulfill、fake repository 或直接种最终页面结果。宿主运行原缓存 Worker 函数，不启动独立 Celery broker 子进程；正式 Celery 进程投递未作为本票已验收结果。

浏览器选择真实合成 BC，切系列并填写本地日期/应用筛选，实际显示每个 Campaign total=40、D0=30、spend=50、本地 ROAS=0.8、平台 total ROAS=`0.801234567890123456789`、D0平台比率=0.601。没有将平台比率冒充本地 0.8。

## 运行结果

从任务树根执行（runner 自动设置该树 backend 的 PYTHONPATH）：

```bash
python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-test.py python -m pytest backend/tests/modules/reporting/test_native_campaign.py backend/tests/modules/reporting/test_metrics.py backend/tests/modules/reporting/test_publish.py backend/tests/modules/reporting/test_metric_catalog.py backend/tests/integrations/tiktok/test_sdk_report_reader.py -q -s --tb=short
```

该阶段 53 passed/13.44s，含真实主 Seam。之后新增账户恢复检查、修复 Campaign 覆盖来源；针对实际变动执行 native + dimension_query：24 passed/8.28s。撤权后原 direct snapshot 拒绝访问的补测 1 passed/0.39s；最终对齐类型合同和原字段后 native/metric_catalog/dimension_query 29 passed/9.79s；补齐比率来源字段计数后的 native 7 passed/9.38s；最终state合同/摘要迁移及空桶合同检查 native 7 passed/9.58s（含主Seam）。其余相关已通过且未改动检查沿用，不重复全量运行。5 条 warning 来自现有 websockets/asyncio 弃用。

原断言未修改。独立预期来自 JB-25/JB-24：40/50=0.8；平台比率保留原精度；999 的子广告收益不能填父级 dash；真实0、NULL、DASH、ABSENT、完整无行与 LEGACY_UNKNOWN 不等价。两个租户、两个 BC 相同 remote ID 分别读到40/222/777，越权需求不入队不产事实，撤权原快照被拒绝。实际广告发布、qem 触发器、投影重建与账户重新发现的 INSERT trigger 均未改写父级金额。

合成两对象/单日本机规模：平台请求2次；真实 HTTP 首查289.26ms，limit1首查加下一页154.26ms。类型合同阶段本机一次运行首查430.66ms、limit1首查加下一页378.92ms；这些是本机小夹具测量，不是生产容量承诺。PG EXPLAIN 确认 `ix_parent_report_period` 能约束 tenant/account/level/date；小数据为证明索引适用而关闭 seqscan，不能声称生产自然选用该计划。查询金额走 SQL NUMERIC 聚合；证据仅按当前页对象读取，未将全表搬入 Python。

前端 `bun run build`（TypeScript、正式与 demo）通过。Ruff scoped check/format 与 git diff check 通过。OpenAPI 客户端仅提交新增 contract/provenance 类型，不纳入生成器额外补出的无关 M3 exports。

严格 mypy 未全绿：在精确起点 `git archive` 的独立只读源码归档（无 Git checkout/新分支）运行同一目录命令复现418条；本票静态比较阶段411条，规范化文件路径/行号后无新增诊断。保留 baseline/final/delta 原始日志；不把整个 mypy 称通过。最终类型合同与覆盖状态变动后再执行同一 scoped 静态比较，仍为418→411、无新增诊断。

## Red / Green / 错误探针

- 日请求缺平台 ROAS 的新增检查先 RED，拆出小时成本字段并采集日平台比率后 GREEN。
- 两页 Worker 初始缺 Campaign 发布入口，先 RED，增加原 owner 发布路径后 GREEN。
- 覆盖检查先以 AVAILABLE 对 PARTIAL 实际失败；修正原 Ad 覆盖借用后 GREEN。
- 首次命令 cwd 路径、测试 BC 非数字被既有前端校验排除、SQLModel model_copy 不能当新 ORM 行、网关租约短于执行期限、首屏宿主缺缓存 Worker 等均明确修复为测试环境/夹具问题，不算业务 RED，也没有放松产品授权/选择规则。字体 serving allow list 提示未阻断页面验收。

仓外 `probes.py` 串行执行8个实际源码消融：去掉direct优先、缺项补零、平台比率经过float、小时携带Native日字段、用Calendar字段、用onsite字段、去掉需求授权、direct缺项回退子级。每项均被具体金额/字段/权限断言拒绝；finally逐字节恢复后各自目标测试GREEN。原件备份和各 red/green 日志保留在上述 tmp 目录，无客户原件。最终类型合同与 remote_id 形状再次执行这8个目标探针，结果记 final-probes.log；不重复无关全量回归。

## 迁移、交接与边界

迁移 `native_campaign_day` additive：新增parent表/索引、只读选择view与Ad nullable evidence；新增 nullable state active_contract/publication_digest（完整空发布也保存合同和摘要），不改旧金额、不回填旧direct、不改历史迁移。回滚函数明确拒绝自动drop新money，须另审可读新事实的兼容reader。生产发布前按 Spec 排空旧报表/投影writer，迁移并统一API/Worker版本；此处没有执行生产迁移或部署。

JB-26消费：复用 ParentReportDay 的 level/remote_id/local-day、MetricCatalog NATIVE_CONTRACT、metric_evidence、原需求/分页/Worker与 `_writable_buckets/_set_state`。本票只发布 Campaign，不实现 Adgroup；不得把父级金额写旧派生表。字段状态存 evidence.fields，平台比率存 Decimal 字符串；publication_version/observed_at与 ReportSyncState 同事务。Campaign query 的 direct选择和 Ad evidence 保持本票合同。state active_contract/publication_digest 已保存；完整重放幂等与并发拒绝策略仍由后续票完成，不据此宣称已完成。

未进入本票的后续工作：Adgroup完整采集、三层部分失败/统一总览、完整并发与重放digest、子集/来源分桶计算、冻结行指标/详情/CSV、全M4a形式与验收。当前快照仍沿既有成员冻结/页内金额重算机制；本票不宣称跨刷新指标冻结。自动规则、经营测算、素材金额仍用原输入。本地实现与验证交由root独立审查；不自标Done、不开放JB-26、不宣称M4a完成。真实平台支持和生产验收仍Unknown。

最终目录合同补测：明确公开 levels 和 PLATFORM_DIRECT/LOCAL_CALCULATION，额外断言平台观察与本地公式的区别；主Seam目标检查 1 passed/5.96s，客户端类型同步，TypeScript静态检查通过。该补充只收紧目录 DTO，无广告业务操作。
