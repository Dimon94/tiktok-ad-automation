# Native Series M2 最终验证维护记录（JB18）

状态：**验证维护产物已执行；M2 未宣称完成，root 仍需独立 Standards/Spec 审查与集成**。
起点及当前产品 sync：`5d6706d1594fb5395e3e2b2e7c9ca3661f531ed9`。分支 `feat/jb13-native-series-m2`。唯一既有集成树 `/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb13-native-series-m2`。保留 `.venv`、`node_modules` 软链；不派子代理，不切换树，不改 main，不 push/PR/deploy/Linear。

所有 pytest 均由 `python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py` 在 `backend` cwd 执行。真实本地 HTTP、PG、Redis；唯一外部业务替身是 SDK transport。测试隔离元数据不是生产认证或真实平台证明。产品 `backend/app` 最终无 diff。

## 结论和新增实测

1. R11 有界模型已加入 `Submission.tla`：`LocalCancel` 只设置一次取消标记；`CancelPending` 只关闭未 arm、零发送且未 MATCH 的 idle pending；`CancelFence` 禁止取消后新发送。armed/sent/UNKNOWN 的 body、digest、attempt、ID 和核查事实不由取消擦除。
2. 实际最终物理发送围栏为 `backend/app/modules/builds/execution_state.py:742` 的 `require_local_send_open`，由 `arm_request` 和 `backend/app/modules/builds/execution.py:1111` 的 `send_scope` 共用。真实 owner `submissions.cancel_submission` 持 Submission 行锁。新增 `backend/tests/modules/builds/test_native_batch_execution.py::test_cancel_before_final_send_fence` 在原 authority 锁前暂停，独立 PG Session/真实 HTTP 取消先提交，再执行原围栏。实测 FAILED/local_submission_cancelled、SDK 零写，armed body/digest/attempt_id 不变。
3. 正式探针 `cancel-send-fence` 仅破坏该共享 fence，真实 RED 为 `AssertionError: ('SUCCEEDED' == 'FAILED')`；finally/cmp 恢复后 GREEN。日志 `/tmp/tkada-native-series-20261112/m2-implementation/m2-final-verification/source-cancel/cancel-send-fence-broken.log` 与最终 source 输出。之前 queue/process/selector 及组合破坏仍绿，未算反证。没有用环境错误冒充红。
4. 历史 flat 纵切由真正 flat 版本 `312468f7ae2188b3f7fb6917612004fe7100d23f` 的 `native_preview.generate()` 写入隔离 PG；不关闭 immutable trigger、不硬改当前冻结 JSON。仅补充该版本之后新增的非空 `batch_short_id`，不改 native_request、Unit snapshot、digest、ID。`backend/tests/modules/builds/test_historical_native_flat.py` 实测原 flat 请求、digest、Unit 在统一 read_batch、HTTP 提交/展开、结果读取和新键重放后不变。`1 passed/6.43s`，日志 `/tmp/tkada-native-series-20261112/m2-implementation/m2-final-verification/historical-flat-real.log`。这是读取/结果纵切，不声称新增 UNKNOWN 恢复。
5. 最终默认、recovery、historical、historical-witness、cancel 五个模型入口均成功。13 个正式源码探针全 RED→finally/cmp→GREEN；T18 no-closure/lost-request 源码探针恢复绿。`test_batch_browser` 本轮 `1 passed/12.42s`。取消场景、真实 UNKNOWN/inflight 竞态、`historical-rejection` 本轮均绿。最终批次浏览器三场景本轮 `3 passed/63.56s`，见末节。

## 最终模型证据

输出根：`/tmp/tkada-native-series-20261112/m2-implementation/m2-final-verification/final-models/`。正常模型零剩余队列、零不变量错误；破坏变体的 invariant violation 是预期反例，不是正常模型失败。

| 分支 | 实际 distinct states / 结果 | 边界 |
| --- | --- | --- |
| default | Submission N=1,K=4：56524；N=2,K=3：1181344；Authority：1456。9 个 Submission 及 5 个 Authority 反例均命中指定性质。 | N=1,K=4 每计划两 Ad；其他分支 K=3 每计划一 Ad。小状态安全性，不是任意规模证明。 |
| recovery | Submission 3804/1181344；recovery、retry、completion、cancel-fence、cancel-armed 反例命中；Authority 1456。 | 恢复折叠一次扫描提交；不证明真实锁或扫描期间 scope 全交错。 |
| historical | 正常 3804/1181344；witness 193638；negativeproof/recovery/retry/completion 及两 R11 反例命中。 | 仅持久历史明确无效果拒绝；Timeout/空 GET 仍 UNKNOWN。 |
| historical-witness | `NoHistoricalTerminal` 预期 witness 193638 distinct。 | 故意可达 FAILED 路径，不当安全失败。 |
| cancel | 正常 3804/1181344；cancel-fence 155、cancel-armed 127 反例命中。 | LocalCancel/CancelPending 抽象 PG 行锁；不证明锁实现、liveness 或进程调度。 |

命令入口为 `.agents/skills/formal-verify-native-series-batch-submission/run.sh`，分别设 `FORMAL_ONLY`；默认带 `--all` 串行运行正式源码探针。首轮 `--all` 工具 360 秒超时，随后完整重跑成功。旧模型基线先于任何模型修改执行，位于 `.../m2-final-verification/old-baseline/`。

## 源码破坏和真实并发

正式 `source_checks.py` 当前全部 13 项：budget-double、budget-float、digest、completion、unit-scope、actor-stamp、receipt、account-lock、recovery-ambiguous、unique-replay、unknown-retry、cancel-send-fence。每项均要求行为断言红，finally 字节恢复/cmp，再绿。`unit-scope` 随当前统一 `read_batch` owner 更新锚点；恢复探针收窄到精确恢复行为，避免浏览器环境混入恢复反证。

- `backend/tests/modules/builds/test_native_batch_execution.py` 取消 before/partial/after-campaign/unknown：4 个场景分别通过。原最终组合日志 `final-targeted.log` 中 `[cancel-before]` 的旧超时已被后续数字 BC fixture 修正，不能拿该旧日志作最终通过证据。
- `backend/tests/modules/builds/test_native_execution.py` cancel_inflight/unknown_late：`2 passed,46 deselected in 37.10s`。独立 PG 会话真实 transport 中取消，已发 ID、原 attempt、RESULT_UNKNOWN/LATE_CREATED 保留；这是 send-first 证据。与新增 cancel-first 一起覆盖两个可观察顺序，但不宣称任意并发证明。
- `historical-rejection`：`1 passed,45 deselected in 33.11s`，统一历史明确拒绝恢复、原 ID/body/digest/attempt 保留。T18 no-closure/lost-request 当前源码破坏红、恢复绿。

## T1–T20 证据矩阵

“本轮”指本 fresh-context 直接运行；“继承”是已读验证记录中同产品 sync、未重跑的有效结果。来源 commits：JB14 `9066dc2274d7cb29e1ed66b27ba864679eaa828c`，JB15 `820a26e9c2fef02b0d0136e8f0a93b8625ad3b1d`，JB16 `48adee03771e8647b6b7a5d34ff46c23141bf951`，JB17 `f80201b0654a3c9b39144fd07e3b9d0b05d8f639`，取消产品 `5d6706d1594fb5395e3e2b2e7c9ca3661f531ed9`。

| 项 | Owner | 行为用例与实际证据 | 本轮影响/边界 |
| --- | --- | --- | --- |
| T1 | `NativeSeriesPlanSet` / plan_set HTTP | 重复键可见错误；显式账户/身份/地区 mapping 的实际用例见 JB14 验证记录，不虚构不存在的测试函数名。 | 继承。显式计划，不按名称合并；未跑真实平台。 |
| T2 | scene jobs / plan preparation | `test_independent_preparation_retry_and_revision`、真实 Draft HTTP prepare 和逐计划任务；JB14。 | 继承。真实 PG/Redis，SDK transport 替身；非平台证明。 |
| T3 | `native_preview.read_batch` / generate_batch | READY/BLOCKED/PARTIAL 覆盖、EXCLUDED 原因和单计划预算；JB15。历史 flat 包装本轮实测。 | 读取 owner 受影响，已有 ready/blocked 结果继承；flat 本轮绿。 |
| T4 | readiness / generate_batch | 全部冲突或资格失败不可提交、原因可见；JB15。 | 继承。与单计划排除不同，不全局误封。 |
| T5 | scene facts / sibling TTL | `test_http_two_authorized_plans_and_expired_sibling`、独立 PG 重试；JB14。 | 继承。TTL 原证据不改写，模型不证明时间。 |
| T6 | Draft revision / previews | 四父状态编辑增 revision、旧预览提交拒绝；JB14。 | 继承。R12 queue/send fence 另列。 |
| T7 | per-plan Units / preview | 多账户、Series、身份、地区、创意展开及已知 ACTIVE 单计划排除；JB15。 | 继承。页面可见性由前端用例，非官方支持矩阵。 |
| T8 | freeze transaction / immutable scope | 证据/摘要坏输入回滚、全排除不可冻结、并发 freeze 唯一；JB15。 | 继承。真实 PG trigger 保留；非任意 DB 故障证明。 |
| T9 | Submission/expand/execution | 仅 INCLUDED 建 Unit/Step，SDK POST 全 ENABLE 且无第二激活；JB16、取消本轮保留。 | 继承并受取消影响复核；仅 SDK transport，不调用真实 TikTok。 |
| T10 | submit_preview / advisory+unique lock | `test_same_preview_replay_returns_original`、独立 PG 同/新键和 worker 重放；JB16/17。 | 继承。unique-replay 本轮红绿；DB 唯一约束未移除。 |
| T11 | native_completion / readback | 全计划三级+creative MATCH→COMPLETED；JB16/17 `match/recovery-complete`。 | 继承。回读和 creative 比较真实 owner，模型只表示结论。 |
| T12 | native_completion / cancel | 一 MATCH、另一取消/失败→PARTIAL；`cancel-partial` 本轮通过，JB16 partial。 | 本轮实测。EXCLUDED 不伪装 MATCH，成功兄弟保留。 |
| T13 | native_completion / cancel | 全部失败或取消且无 MATCH→FAILED；`cancel-after-campaign` 本轮通过；T18 historical-rejection 本轮绿。 | 本轮实测。UNKNOWN 仍覆盖失败派生。 |
| T14 | record_unknown / recovery | Campaign Timeout→UNKNOWN/NEEDS_REVIEW，不盲重发；`cancel-unknown` 本轮通过，JB17 recovery。 | 本轮及继承。Timeout/空 GET 不是负证据。 |
| T15 | reconciliation / unique recovery | 部分唯一 MATCH、其他项继续，无新 POST；JB17。 | 继承。恢复只消费原范围，模型非 refinement。 |
| T16 | reconciliation / batch results | 全部唯一 MATCH→COMPLETED；JB17 recovery-complete；真实浏览器 recovery 本轮通过。 | 继承加浏览器实测。恢复只消费原范围，模型非 refinement。 |
| T17 | reconciliation / partial results | 部分 MATCH、其余终态→PARTIAL；JB17 recovery-partial。 | 继承。不重建成功兄弟。 |
| T18 | `_recover_known_rejection` / result | 历史明确无效果拒绝→FAILED；`historical-rejection` 本轮绿、两源码探针恢复绿。 | 本轮实测。仅同 source/attempt 的持久拒绝，未知响应不可清 UNKNOWN。 |
| T19 | `cancel_submission` / UI/API | 全未发送取消、CANCELLED、零 SDK 写、重放同一 Submission；JB18 记录。新 cancel-first 真实 HTTP 和最终浏览器取消本轮通过。 | 实现、竞态及浏览器本轮实测。 |
| T20 | `cancel_submission` / result/readback | partial/after-campaign/unknown 本轮 4 场景绿；send-first 两场景绿。 | 本轮实测。armed/sent/UNKNOWN body/digest/attempt/ID/迟到事实不擦除。 |

## R1–R13 证据矩阵

| 项 | Owner | 行为/反证证据 | 影响与边界 |
| --- | --- | --- | --- |
| R1 | PlanSet schema/plan key | 同 Series 不同账户、同名不同身份不合并；重复键拒绝。JB14。 | 继承。规范校验不是平台组合支持证明。 |
| R2 | explicit plans/read_batch | 两显式计划不扩笛卡尔、复制/批量编辑保持映射。JB14。 | 继承。当前统一读取新 flat 本轮补测。 |
| R3 | occupancy matrix | 同账户/地区交叠和 ACTIVE 才逐项排除，跨账户不全局封禁。JB15。 | 继承。UNKNOWN 覆盖限制透明，不宣称无冲突。 |
| R4 | readiness/occupancy | UNVERIFIED/BLOCKED 排除；PARTIAL/STALE/UNKNOWN 且 READY 可预览；JB14/15。 | 继承。资格与占用分开。 |
| R5 | frozen request/digest/DB scope | manifest 变异、body/digest/actor/stamp 源码红绿；历史 flat 本轮原正文/digest/Unit 不变。 | 本轮 sync 到 5d6706d1；没有摘要碰撞/全字段形式证明。 |
| R6 | deterministic allocation | 顺序/并发稳定，广告数=creative_count×基础广告；JB15。 | 继承。模型不证明分配算法。 |
| R7 | native_batch_budget | Decimal 多计划、EXCLUDED 过滤、double/float 真实函数红绿，独立整数 oracle。 | 本轮源码探针重跑。整数模型不证明任意 Decimal。 |
| R8 | submit_preview/PG unique | 并发同/新键同 Submission、worker 同 Unit/Step；unique-replay 红绿。 | 本轮探针；源码反例是错误拒绝重放，不是制造第二 DB 行。 |
| R9 | claim/arm/record_unknown | 失败/UNKNOWN/取消不重建成功兄弟，重复 worker 无新写；unknown-retry 组合破坏红绿。 | 本轮探针。confirmed-absent 恢复另受 R10 边界。 |
| R10 | reconciliation/recovery | 零/多/不完整保持 UNKNOWN，唯一完整候选补 ID；recovery-ambiguous 红绿。 | 本轮探针。多候选模型与真实 claim/finish 分事务，不称组合证明。 |
| R11 | cancel_submission/require_local_send_open | idle pending 才可关，取消后零新发送；cancel-fence/cancel-armed 模型反例；共享 fence 源码 RED→GREEN。 | 本轮新增。已发/UNKNOWN 保留；不证明任意调度、锁实现或平台远程取消。 |
| R12 | dispatch/send_scope/Authority | revision/route/actor/account/BC/NULL/ABA queue+send 边界；5 Authority 反例，真实 stamp/lock 探针。 | 本轮 sync/探针。只在小模型与既有真实边界内，不是分布式原子性证明。 |
| R13 | native_completion | 缺三级/creative/EXCLUDED/FAILED/UNKNOWN 不完成；completion/receipt 源码红绿；取消状态本轮。 | 本轮 sync。真实 creative 比较器另测，模型只压缩 MATCH。 |

## 最终浏览器实测与尚缺项

`backend/tests/modules/builds/native_browser_http.py` 启动真实本地 uvicorn HTTP 与真实 PG/Redis，仅 SDK transport 为替身；Playwright 操作真实页面。

本轮 `batch-workspace`、`recovery-browser`、`cancel-before` 三场景实测 **3 passed/63.56s**，日志 `/tmp/tkada-native-series-20261112/m2-implementation/m2-final-verification/final-browser-numeric.log`。覆盖真实页面 prepare/freeze/submit/results、UNKNOWN 恢复和本地取消。此前 prepare 超时的根因是 harness 使用 `bc-fixture`，父租户路由仅接受数字 `bc_id`，导致 scope 丢失。仅为该 batch 场景复用 `source_env={"bc_id":"90071992547409931234"}`；无产品源码变化。

另测 `test_native_plan_set.py` 六场景 **6 passed/28.08s**（含真实 Draft 编辑/准备），`test_batch_browser` **1 passed/12.42s**。计划编辑/准备仍标为直接补测；重复页面证据不扩大为真实平台证明。

数字 BC fixture 更新后，`recovery-ambiguous` 以精确 `[recovery-source_env0]` 重跑：破坏 RED、恢复 GREEN。日志 `/tmp/tkada-native-series-20261112/m2-implementation/m2-final-verification/final-source-numeric-recovery.log`。

尚缺 root 独立 Standards/Spec 审查、集成和 M2 放行。无真实 TikTok/生产验收，无全系统 refinement/liveness/任意规模证明，无新增迁移。本轮模型、验证测试、skill、文档已聚焦提交；不宣称 M2 完成。
