# JB-27 三层刷新与发布可靠性

起点 `7941c17662c2980284491c7fb1548a0638fa07dc`；唯一集成树/分支 `feat/jb24-native-growth-m4a`。只实施 JB27，不更新 Linear，不启动 JB28–30，不合 main，不推送、PR、部署、生产迁移或真实平台调用。保留 `.venv`、`node_modules` 原软链。正式预期来自 JB24 的 T2/T3/T5/T6/T7/R7 及完整 JB27 AC；所有平台行和凭据均为合成测试值。

## 实现

- 原 ReportingData publisher 在既有稳定账户 advisory lock 内统一判定同观测重放/冲突；合同、规范 payload（包含来源证据和币种/时区）一致才 no-op。相同观测冲突抛 `report_publication_conflict`，不进入提供商重试。新观测仍可下修、归零、dash 或完整清空。
- 单页快路径执行与多页相同的连续页、总数和 duplicate 校验；Ad staging 前校验逻辑身份，拆分后合并也不允许重复。checkpoint scope 绑定统一 LEVELS 请求形状及合同，沿用原 PG 首次观测、原 SDK、原 Worker 和执行 fence。
- 提供商失败进入原重试策略时立即标本层 STALE/FAILED，保留成功事实及其水位；只重试该 job。逐层状态计算刷新概览 COMPLETE/PARTIAL/MISSING，不新增任务 owner。
- 查询依赖加入三层 kind、状态、成功/尝试时间、错误、直接版本、合同和摘要；继续携带原 scope/filter、权限与派生修订。相同金额的新证据也使新查询失效，旧快照冻结属于 JB29。

## AC 矩阵

| JB27 AC | 运行证据 / 结果 |
|---|---|
| 1 三层两页、SDK、额度续传、网络无发布事务 | `test_two_page_adgroup_worker_and_query[True]`：三层均请求 `[1,2,2]`，同层除 page 外冻结参数相同；保留第一页 checkpoint，只重读第二页。SDK transport 中独立 PG 会话 NOWAIT 取得任务行与三层账户锁。 |
| 2 冻结范围/合同/路由/维度/总数与重复拒绝 | `test_worker_checkpoint_rejects_frozen_changes` 三层覆盖 account/date/level/route/contract/dimensions/metrics/totals/overlap/single_duplicate；明确领域错误码、无成功水位；publish 入口拒绝同层重复，包括合并后的行。 |
| 3 两 Worker/fence/route | `test_two_workers_reject_lost_fence` 两线程、独立 PG 会话、Event 因果屏障；真实 PG/Redis dispatcher 领取。旧 Worker 暂停网络时替换执行代数或撤销 route，新执行完成后才释放旧回包；旧执行不能发布。另有两个独立 Celery 进程实际消费 canonical 消息，见下。 |
| 4 反序与空包、PG 首次时间 | Worker 围栏测试含新完整空包；`test_publish_watermark.py` 真实并发 PG 锁及反序观测回归；SDK 额度续页保存并比较同一 PG observed_at。 |
| 5 no-op 与冲突、独立版本 | `test_same_observation_replay_noop_and_conflict` 三层 × 空/非空：完整 state 读回不变，冲突拒绝，下次新观测版本逐次 +1；重放源码探针能变红。 |
| 6 下修/0/dash/迟到 | 同用例逐次读回真实 PG money：2、0、NULL；最后旧999不能覆盖。 |
| 7 完整空原子发布 | 三层空包重放/围栏；原 JB26 独立状态测试及本票真实原子故障探针。空状态有合同/摘要/版本，无虚构事实行。 |
| 8 组第二页失败及 PARTIAL | 真 SDK transport 抛合成提供商错误；组保留40及原 observed_at，Campaign/Ad state 完整相同；HTTP/页面检查 PARTIAL、旧收益与错误原因。 |
| 9 只补失败层 | 原 Adgroup job 原策略再领取后恢复；其版本 +1，Campaign/Ad 所有状态字段不变。 |
| 10 旧值持续可读/独立可见 | 失败期间真实 query/HTTP/工作区读旧40；成功层不等待其余层；恢复后再次经过真实工作区。 |
| 11 锁/原子性 | 既有稳定账户锁保持；真实 PG trigger 在状态写入失败，facts/evidence/state 全回滚；去掉原子边界的源码 probe 红。 |
| 12 查询依赖 | `test_query_dependency_detects_each_layer_evidence` 在 Ad 查询上分别改变三层状态/合同，digest 必变；scope/filter/derived 等原标签保留。 |
| 13 既有合同 | 保留原权限/时区/精确 Ad campaign_ids、取消/退休/清理路径；水位相关旧回归通过；新 checkpoint 清理后事实 evidence 仍读回。旧套件失败见独立基线说明。 |
| 14 有界形式覆盖 | 原 R4/R10 48 状态不变；新增 R7 两 Worker/两观测/三层分量模型 57 状态，四种错误变体均反例；五类源码探针与真实 PG 回归绑定。模型边界见下。 |
| 15 请求、checkpoint、重试、锁与 backlog | 正常每层2次，总6；额度每层3次，总9，第一页不重读；失败/恢复只增加组的读取尝试，成功层不重读；最终三层待执行 backlog 为0。锁由独立会话 NOWAIT 实证；生产诊断继续记录 publish_buckets 等等待/执行阶段。未测吞吐容量、不提出容量阈值。 |
| 16 主 Seam | 真实刷新 HTTP、官方 SDK Adapter/transport、PG/Redis、原 Worker、查询/状态 HTTP、真实 FastAPI/Vite/Playwright 页面；失败和恢复用原链路。线程模型不替代此路径。 |

## 真正运行层级

`test_native_levels` 的正常 Adgroup 初次发布使用两个独立 `multiprocessing fork` Celery **进程**、专用随机队列、专用 loopback Redis broker。真实 `reporting.job_step` 消费 dispatcher 生成的 PendingDispatch payload/envelope；重复投递同一 canonical 身份，PG 最终 COMPLETE，仅两次 SDK 页请求、一个本层版本。子进程只替换 urllib3 官方 SDK transport，继承合成测试设置；关闭子进程并清理其专属队列/绑定，不 FLUSHDB。不注册第二 executor，也不改业务 HTTP/repository。

额度让出及恢复的其他步骤用宿主调用原 `run_job_step`，不能说所有场景均在 Celery 子进程中跑过。反序 fence/撤权的细粒度测试调用原 `execute_job`，两个真实线程、真实 PG/Redis 领取和原 TransactionBoundaryReader，Reader 仅返回合成外部页；该层不声称覆盖官方 SDK。真实 SDK/HTTP/页面的独立用例补齐接线。

## 红绿与拒错

最初六条同观测回归全部红：非空再次返回1，空包也推进版本/时间；实现后同输入绿。Ad 重复原先抛 PG UniqueViolation，改为明确领域错误；查询父层状态变化原先未改变 Ad 查询 digest；等待重试时原先仍 COMPLETE。各项均先复现再修复。

增强金额断言曾读到 SQL 整桶替换前留在 identity map 中的旧 Ad 对象；测试改为 expire 后从 PG 读回，未改期望。曾有两次测试进程重叠，共用 MMF 测试库导致领取选择受扰；最终验证严格串行。旧失败不能拿此前绿覆盖，以下最终结果为准。

`source_probes.py` 逐个破坏 watermark、replay、duplicate、fence、atomic；每次 `finally` 恢复原字节并断言一致，再运行同一目标绿。atomic 最初注入点晚于状态 trigger，未检出；移到 facts 与 state 之间提前 commit 后正确变红，未弱化原子断言。

形式模型使用 Python 标准库显式穷举：三层各一个有限账户日桶，两个 Worker、两种观测、VALUE/EMPTY、独立 fence/版本。按 kind 分量分解；假设数据库事务原子、跨桶 SQL 范围正确，真实 PG/Redis 回归检验对应实现。四个 broken 变体必须失败；它不是整个系统、Redis 崩溃恢复、任意数量 Worker 或 SDK 的证明。源码映射和复跑命令在现有 `.agents/skills/formal-verify-reporting-data/SKILL.md`，未建立平行 owner。

## 命令和结果

统一前缀：`python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py`。先读 config/README，测试自动调用 tests/database.py guard；专用 MMF test PG/loopback Redis，未重装依赖。

- `python -m pytest backend/tests/modules/reporting/test_native_publication.py backend/tests/modules/reporting/test_native_levels.py -q`：61 passed；随后只增加请求数/backlog断言，levels 复验 7 passed（每层 request_count=2/3，三层待执行 backlog=0）。
- `python -m pytest backend/tests/modules/reporting/test_publish_watermark.py -q`：前一匹配行为批次与主检查合计72通过；该文件覆盖24项，未在不相关更改后重复全仓测试。
- `python3 .agents/skills/formal-verify-reporting-data/scripts/check.py`：R4/R10 48状态 PASS。
- `python3 .agents/skills/formal-verify-reporting-data/scripts/publication.py`：R7 57分量状态 PASS，四个 broken 变体均 AssertionError 反例。
- `python3 .agents/skills/formal-verify-reporting-data/scripts/source_probes.py`：watermark 6红→6绿；replay 6红→6绿；duplicate 2红/1绿→3绿；fence 6红/6绿→12绿；atomic 1红→1绿。字节恢复确认。
- `mypy --config-file backend/pyproject.toml backend/app/modules/reporting`：当前410条，精确起点410条；去行号后逐条诊断集合比较**无新增**，严格 mypy 仍不全绿。新增 helper 类型诊断已解决，未以数量代替集合比较。
- Ruff scoped check/format 与 diff check PASS。
- frontend 产品源码未改，复用 JB26/root 匹配源码的正式/demo build 和 TypeScript 证据。本票只扩展 Playwright 断言，真实浏览器路径另跑；未部署。

独立基线：以 `git archive 7941c17662c2980284491c7fb1548a0638fa07dc backend` 解包为只读来源测试归档（无新 Git checkout/worktree），新建独立 loopback PostgreSQL test 库并完整迁移。在该起点原测试执行 `test_basic_checkpoints.py` + `test_execution_boundaries.py`：10失败/5通过，与当前相同的六条旧 checkpoint 领取、owner_epoch 夹具及三条 directory 失败；其中 directory 是本机 PG 缺 `transaction_timeout`。未改这些既有预期。归档和独立库验证后清理，不触碰原 main 或共享M3库。

## 交接与边界

JB28 沿用 LEVELS/PARENT_KINDS/DAY_KINDS、Parent/ad 独立事实、ReportSyncState active_contract/publication_digest/publication_version/observed_at。直接 dash/empty 权威；平台比率仍 Decimal 字符串，money 只 Numeric，未请求比率无 ABSENT。

JB29 消费 query dependency 的三层证据变化和 display.report_refresh_status/report_layer_status（最后成功/尝试/原因）；继续完成 frozen metrics、旧 snapshot/CSV 合同。本票未启动这些工作。整 M4a 正式两轴审查、root 验收、合 main/收票、生产环境及真实平台均未完成。


## Root 阶段验收

root核对候选b17b9b36b208514ee4028b5871cb980ff2244e71及其相对7941c176的diff：同观测payload裁决在既有账户锁内；单页快路径已移到分页/duplicate校验之后；重试失败保留旧事实并更新本层状态；三层证据/状态加入query dependency。核对Celery宿主使用fork后dispose继承DB连接和专属队列，未把函数调用冒充独立进程。

root亲跑：`python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py python -m pytest backend/tests/modules/reporting/test_native_publication.py backend/tests/modules/reporting/test_native_levels.py backend/tests/modules/reporting/test_publish_watermark.py -q --tb=short`，84 passed/32.35s，17条既有弃用warning。包含真实主Seam与独立Celery投递。

另亲跑publication.py正常和broken-fence/watermark/duplicate/atomic四变体：正常PASS、四变体均预期AssertionError；日志位于外仓jb27-root-model-*.log。该模型仍只有文内有界分量假设，不是整个系统证明。未重复未修改的五组源码探针，继承候选的精确源码证据。root未修改本票产品代码，未重跑匹配前端源码的build，未声称mypy或全仓全绿。

JB-27可作为后续集成基线；正式独立两轴审查、JB28筛选计算、JB29快照和JB30 CSV及整个M4a验收仍未完成。前票在Linear保持InProgress直到整体合入，不移除阻塞关系。无push/PR/部署/生产变更。
