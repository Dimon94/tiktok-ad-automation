---
name: formal-verify-native-series-batch-submission
description: 修改原生批次冻结、批准、预算、执行围栏或完成判定时，运行 TLC 有界交错及真实源码串行破坏检查；保留尚未证明的范围。
---

# 提交与冻结验证（M2 最终验证证据，待 root 独立验收）

目的：防止旧批准发出新广告、重复创建、批准范围被改变、预算算错、取消清除已发事实或部分创建被显示为全部完成。
使用 TLA+ TLC 检查小状态空间内全部可达顺序；Python 没有本项目可直接使用的代码 verifier，金额用生成输入调用真实函数、独立整数系数 oracle 检查，不把手写模型当代码证明。

## 运行与独占要求

从本仓库根执行。`--all` 串行执行模型正常/破坏变体及真实代码 break→assertion failure→finally 恢复→cmp→green。必须持有唯一写者权限，期间不能并行运行其他产品测试或改源码。源码备份、TLC 状态及日志均在仓外；不接触共享软链，不调用真实平台。

```sh
TLA_JAVA=/tmp/tkada-native-series-20261112/m2-implementation/formal-tools/jdk-21.0.13+9-jre/Contents/Home/bin/java \
TLA_JAR=/tmp/tkada-native-series-20261112/m2-implementation/formal-tools/tla2tools.jar \
TEST_RUNNER=/tmp/tkada-native-series-20261112/m2-implementation/run-test.py \
FORMAL_OUT=/tmp/tkada-native-series-20261112/m2-implementation/formal-rerun \
.agents/skills/formal-verify-native-series-batch-submission/run.sh --all
```

省略 `--all` 仅运行模型，不能声称跑过源码破坏。`FORMAL_ONLY=recovery`、`historical`、`historical-witness`、`cancel` 是独立分支；完整验证必须分别运行，不能只看默认分支。换机器需提供已配置隔离 PG/Redis 的等效 runner；本 skill 不安装环境、不读取或输出私有配置。所有 pytest 经 runner 从 backend 执行。源码恢复失败立即停止，先从仓外备份恢复，不提交污染。
工具固定 Temurin 21.0.13+9、TLC 2026.11.06.222357 rev d8fb231（v1.8.0 asset）；models.py 验证 jar SHA256 `e1f11b75c4b5bc4478ba0ac5cb6ae28e40a01b0016bc33e0aa97da22dfdbcd7f`。

## Bounds 与假设

- Submission：一份预览，1 或 2 个 INCLUDED 计划，另一个 EXCLUDED 计划；单计划模型为 Campaign→AdGroup→两个 Ad（兄弟 Ad 可交错），双计划模型每计划为 Campaign→AdGroup→一个 Ad。每个 Ad 的 creative 比较压缩为一个“全部 creative MATCH”事实，与对象 readback 分开；不证明实际多重集合比较器。
- 冻结选择金额 1 或 2 个最小货币单位（这里只解释为 USD cents），每个 INCLUDED Campaign 一次。整数模型不是任意币种/任意精度 Decimal 证明。真实生成检查为 18 种小数位、1–3 计划、0/1/7 广告，共162组及空集；独立等差数列整数 oracle。EXCLUDED 过滤发生在 native_preview，真实 PG 用例负责接线。
- Claim、arm、持锁检查、send、响应结果、receipt、readback 分开。arm 后退出记 UNKNOWN，即使物理发送未发生也不盲重发；没有 R10 的 confirmed-absent 重发。发送失败、丢响应、明确成功均非确定完成。
- R11 LocalCancel 只设置一次本地取消标记；CancelPending 仅转换未 arm、零发送且未 MATCH 的 idle pending。armed/sent/UNKNOWN 保持原 body、sends、ID 和核查事实；CancelFence 禁止取消标记之后的新 Send。模型抽象 PG Submission 行锁在 admission 与 cancel 之间串行，真实独立 PG cancel-first 和 send-first 用例负责接线；不证明锁实现或进程调度。
- 重复提交/worker 抽象为同一动作任意重复使能；数据库事务锁/唯一约束让成功提交和每次 claim 各自原子，不把网络和本地回执视为原子。没有显式两个进程的局部 lease 状态，不能声称证明租约实现。
- Authority：实际 actor scope 折叠为一个 generation，1→2→3 对应撤权/再授权；另存逻辑 epoch 防止 ABA 变体靠相等值假绿。connection、binding、Draft revision 各1→2。历史 NULL=0；批准前可发生变更，批准后不刷新 stamp。无关 scope 是独立变化，不使批准失效。
- scope 锁阻止本地权限/路由/意图改变；Exit 模拟正常释放或进程退出。Send 与 Receipt 分开，丢响应落 UNKNOWN。真实 HARD_LIMIT/task_deadline 限制锁期，模型没有时钟，也不证明超时必定释放；真实 5.2 秒持锁证据见 validation。
- 无公平性假设，仅安全性，不声称最终完成或无死锁。TLC `CHECK_DEADLOCK FALSE` 允许终态与无授权停滞；不检查 liveness。
- 两个模型分开，没有 refinement/composition 证明；Submission 中锁后的 intent 变化交错由 Authority 的 HeldAuthority 单独约束，不能以两个模型分别通过宣称已证明二者接线。

## 性质、反例与源码对应

已读源码对应版本 **5d6706d1594fb5395e3e2b2e7c9ca3661f531ed9**。核心、恢复、历史与本地取消 owner 均重新核对；不是未来提交占位符。覆盖代码改变后先重读事务/调用方，运行对应检查，再推进 sync commit。哈希/关键词不能替代动态证据。

| 性质 / 破坏变体 | 反例含义 | 真实 owner 与动态对应 |
|---|---|---|
| R5 FrozenBody / body | arm 后改正文，原 snapshot 不再匹配 | execution_state.arm_request；0006_build_execution.build_execution_scope（armed request 不可变）；send-lock 用例实际 SQL UPDATE 被拒。源码 unit-scope 探针强制首 Unit，被 test_validate_current_plan 拒绝；digest 探针忽略 manifest，被真实 digest 变异检查拒绝。body 模型变体不是 DB trigger 源码变体。 |
| R7 ExactBudget / budget | freeze 把 Campaign 金额乘对象数 | frozen_requests.native_batch_budget；native_preview.generate_batch 的 expanded→total 与 INCLUDED 写入循环。source_checks 的 double/float 在真实函数上产生断言失败；生成预期不调用被测函数计算。 |
| R8 UniqueSubmission / unique | 同预览第二次 Submit 创建第二份批准 | submissions.submit_preview 的 request-key advisory lock、Draft/Preview FOR UPDATE、0006_build_execution.uq_submission_preview；test_batch_submit_and_execute 的双 PG session 同/不同 key 重放。source_checks unique-replay 在新 key 分支遗忘已有 Submission，真实 PG 回归因返回范围冲突而非原 identity 失败；未移除唯一约束，未产生第二份持久 Submission。模型 unique 的第二份创建与源码 replay 的错误拒绝是 R8 的不同违反，不能宣称两者逐步等价。 |
| R9 WriteOnce / retry | send→unknown→重新排队→第二次 send | submissions.claim_step、execution_state.arm_request/record_unknown、execution.process_step；同一真实批次用例观察重复 worker 的 transport 次数/ID/body 不变。模型不含许可的 confirmed-absent 恢复，交 JB17。source_checks unknown-retry 同时放松 process_step 与 claim_step 的 UNKNOWN/armed、未过期 lease 及原请求重试证据检查；SDK POST 丢响应后同消息重复执行，transport 新增写被独立次数断言拒绝。原 body、stamp、DB 约束和 SDK 编译保持真实。 |
| R5/R9 Excluded / excluded | Submit 给排除计划生成步骤 | native_preview.generate_batch/read_batch；submissions.expand_submission。真实批次用例明确 EXCLUDED 没有 Unit/步骤/预算/写；未新增过滤绕过源码探针。 |
| R11 CancelOnlyIdle / cancel-armed | 取消转换已 arm、已发送或已 MATCH 步骤 | submissions.cancel_submission 的 Submission 行锁、PENDING/QUEUED 且未 arm 选择器；CancelPending 模型只关闭 idle pending。真实 cancel/partial/UNKNOWN 用例保留 body/digest/attempt/ID/UNKNOWN。 |
| R11 CancelFence / cancel-fence | 本地取消提交后仍通过物理发送围栏 | execution_state.require_local_send_open 同时供 arm_request 与 execution.send_scope 使用；真实独立 PG cancel-first 在 authority 锁之前停住、取消提交后原发送失败且 SDK 零写。source_checks cancel-send-fence 破坏共享 fence，真实断言 SUCCEEDED 违反 FAILED/零写；恢复后绿。 |
| R13 Completion / completion | 尚无三级及 creative MATCH 就报告完成 | native_completion.unit_native_matched/batch_results；source_checks completion 无条件 True，真实零完整 MATCH 场景失败；恢复后保留已有 Campaign ID。另有 receipt 探针将相同键重放硬写 COMPLETED，UNKNOWN 场景拒绝。模型 Publish 分别检查完整、部分、无完整、全取消与 UNKNOWN；实际回执不是持久调度 status。 |
| R12 FinalFence / stamp | 批准→撤权→再授权→旧批准发送 | tenants.permissions.build_authorization_stamp/require_build_authorization_stamp；submissions.submit_preview 唯一 INSERT 冻结；dispatch.queue_step、execution._frozen 两边界消费。真实 stamp 比较绕过被 user/superuser regrant send 检查拒绝。 |
| R12 FinalFence / route | 批准后 connection/binding/revision 改变仍发送 | native_execution.validate、accounts.routing.verify_route；既有 test_batch_fences queue/send 证据复用；本轮没有再做三种源码绕过。 |
| R12 FinalFence / null | 历史 NULL 批准获准发送 | actor_authorization_stamp.upgrade 的历史 NULL 和不可变 trigger；test_legacy_approval_readable_but_cannot_send 已有实证复用。未做真实 NULL 绕过源码探针。 |
| R12 HeldAuthority / lock | 锁内撤权改变已准入权限 | execution.process_step.send_scope→native_execution.lock_send_authority→_frozen→SDK request_scope→scope exit；真实 account-lock 删除 BCAccountAccess 行锁，5.2秒 transport 内独立 PG 撤权成功，断言拒绝；恢复后绿。 |
| R12 FinalFence / aba | 再授权复用旧 generation，旧批准重新发送 | actor_authorization_stamp.upgrade 的 DB trigger/唯一 sequence；现有成员删除重建/权限恢复证据。未改真实 trigger 为复用代数，模型反例不冒充源码反例。 |

## 结果解释与核心性质覆盖

每个模型变体必须出现指定 invariant violation；解析错误、环境错误或非零 exit 不算反例。source_checks 必须 exit 1 且含断言失败；两个新 case 还要求明确的 R8/R9 行为断言标记，恢复必须 exit 0。任何正常模型反例先按 action trace 对照真实事务，区分模型错误与产品错误；不要弱化性质来通过。

本轮 R5/R7/R8/R9/R10/R11/R12/R13 有界性质及代表性真实源码闭环见最终 validation。源码 sync **5d6706d1594fb5395e3e2b2e7c9ca3661f531ed9**；验证 harness、模型与文档提交不冒充产品 sync。默认最终 Submission N=1,K=4：56524 distinct，N=2,K=3：1181344；Authority 1456。9 个 Submission 反例（含 cancel-fence/cancel-armed）和 5 个 Authority 反例均命中指定 invariant。另跑 recovery、historical、historical-witness、cancel 分支。13 个正式源码探针全部 RED→finally/cmp→GREEN，T18 no-closure/lost-request 另行恢复绿。产品源码最终无 diff。JB18 最终两轴审查仍归 root。

只复跑这次新增 case（串行，运行时独占源码）：

```sh
python3 .agents/skills/formal-verify-native-series-batch-submission/source_checks.py --runner /tmp/tkada-native-series-20261112/m2-implementation/run-test.py --out /tmp/tkada-native-series-20261112/m2-implementation/formal-rerun-source --only unique-replay
python3 .agents/skills/formal-verify-native-series-batch-submission/source_checks.py --runner /tmp/tkada-native-series-20261112/m2-implementation/run-test.py --out /tmp/tkada-native-series-20261112/m2-implementation/formal-rerun-source --only unknown-retry
```

原子性对应：submit_preview 的 request-key advisory lock、Draft/Preview 行锁、existing 查询、COVERED 与 uq_submission_preview 共同提供提交唯一性和原 identity 回执。只删一个冗余 guard 后仍正确不是反证。R8 探针不改 DB DDL；约束还有效，错误表现为拒绝合法重放。R9 的 process_step/claim_step 共同阻止已有 UNKNOWN/armed 被领取；request-body 已存在时还必须有明确未发送证据。正常源码保留全部保护，探针同时放松组合，仍走真实 arm/send/record_unknown，而非伪造已发事实。claim 和 arm 是不同事务，SDK 网络写与回执非原子；丢响应必须保留 UNKNOWN。模型 Retry 抽象的正是将该保护整体放宽后第二次 Send，不是许可的 confirmed-absent 恢复。

覆盖边界（不是本轮新增门槛，也不是已证明内容）：

1. 本轮没有每个 owner 独立变异：EXCLUDED/冻结 DB trigger、route/NULL/ABA 的真实回归与模型反例可复用，但不宣称分别做过真实源码破坏。六个核心性质的代表性闭环不等于每个 guard 的证明。
2. 两模型无 refinement/composition 证明。Submission 的 intent 可在 Lock 后 Edit，且 Send 不再读 intent；Authority 的 HeldAuthority 单独承担实际锁期不变假设。映射为真实 send_scope 持锁复核到 SDK 调用，已有 queue/send/browser 集成证据验证接线；不能把两次 TLC PASS 当组合证明。当前未发现该接缝的真实源码缺陷。
3. actor User/Tenant/Membership/superuser 与账户/tenant/BC scope 折叠；旧准备、权限代数与任意规模 lease 完整交错未穷举。24项真实围栏检查不等于该形式证明。
4. creative 多重集合、冻结 manifest 全字段、TTL 刷新由真实既有检查覆盖，模型只表示比较结论/意图版本；没有字节级摘要碰撞或任意 Decimal 精度证明。没有真实平台或生产证明。

R10/R11 的有界扩展已合入同一模型。真实恢复仍只允许唯一完整候选或历史明确无效果拒绝；UNKNOWN 不自动转 queued、不重建成功兄弟。取消不隐藏 armed/sent/UNKNOWN、远程 ID 或迟到回执。历史 flat frozen-read 的真实旧 writer→当前 HTTP→PG→结果纵切见最终 validation；恢复中 scope 变化、真实锁实现和任意规模仍未证明。禁止由 TLC PASS 推断平台、生产、liveness、refinement 或任意并发正确性。
## JB18 R11、历史 frozen-read 与全模型维护（2026-11-13）

源码 sync 为 `5d6706d1594fb5395e3e2b2e7c9ca3661f531ed9`。更新自当前源码 diff，不由模型 PASS 反推。R11 只证明有界 idle pending、取消标记后禁止新发送及保留既有事实；真实 cancel-first/send-first 与独立 PG 恢复证据见 `docs/validation/2026-11-13-native-m2-final-verification.md`。历史 flat 输入由真正 flat 版本 `312468f7ae2188b3f7fb6917612004fe7100d23f` 的合法 writer 构造，原不可变 trigger 开启；当前读取/结果不改原正文、digest 或 ID。

最终默认、recovery、historical、historical-witness、cancel 五个分支全部执行；所有反例都是预期 invariant violation。真实源码 `cancel-send-fence` 的破坏产生 `SUCCEEDED != FAILED` 行为红，finally/cmp 后绿；之前 queue/process/selector 保持绿不算反证。最终真实浏览器 prepare/freeze/submit、results/recovery、cancel 三链已通过；批次场景复用数字 BC fixture 以符合父路由 search 合同。模型没有全系统 refinement、liveness、任意规模或真实平台证明；不据此宣称 M2 完成。

维护入口：`/maintain-formal-verification-skill`，先核对源码映射再推进覆盖版本。

## JB13 M2 R1 八行 live-sibling 独立同步（2026-11-13，待 root Spec R2）

本轮源码 sync 为 `0d9c8846fe1d2ba7318d28c159e891a275018c62`，前基线 `5d6706d1594fb5395e3e2b2e7c9ca3661f531ed9`；产品 diff 仅为 `native_completion.batch_results` 新增失败后代闭包外非 READBACK 活跃兄弟保持 RUNNING 的八行分支。原五分支模型在本轮修改前通过，但旧 `Submission` 没有失败后代闭包及 live-sibling 中间态，故该 PASS 不覆盖 R1。

新增 `Completion.tla` 是**最小独立聚合模型**，不扩展旧 Submission 的完整调度维度。N=1,K=4 表示一个 Campaign→AdGroup→两个 Ad，N=2,K=3 表示每计划一个 Ad；另有一个代表性准备步骤。create/readback/creative MATCH 独立；`pending` 只在 live guard 抽象 PENDING/QUEUED/RUNNING/RETRYABLE，真实 HTTP 四态回归区分原状态。`RecordBlocked` 表达稍后持久化的 blocked 后代，闭包直接决定聚合。优先级依次为完整 MATCH、UNKNOWN/MISMATCH、取消、可执行兄弟、失败派生；无完整 MATCH 的多计划不是 PARTIAL。

```sh
FORMAL_ONLY=completion \
TLA_JAVA=/tmp/tkada-native-series-20261112/m2-implementation/formal-tools/jdk-21.0.13+9-jre/Contents/Home/bin/java \
TLA_JAR=/tmp/tkada-native-series-20261112/m2-implementation/formal-tools/tla2tools.jar \
FORMAL_OUT=/tmp/tkada-native-series-20261112/m2-implementation/m2-r1-formal-sync/completion-final-clean \
bash .agents/skills/formal-verify-native-series-batch-submission/run.sh
```

正常两组分别 6941 generated/2082 distinct、327889/69696，均无反例。live-sibling 错误变体 N=1,K=4 在 53 generated/45 distinct 处违反 `LiveSiblingSafety`：一 Ad 失败、另一 Ad 仍可执行时提前 PARTIAL，而非 RUNNING；N=2,K=3 另违反 `Completion`（23/23）。正常模型的 `NoBlockedDescendantTerminal` 预期反例是两计划 Campaign 成功、AdGroup 失败、未记录 Ad 后代仍 pending，闭包排除兄弟后计划 PARTIAL、批次 FAILED；2362 generated/1066 distinct，证明该中间态可达且非无限 RUNNING。这是 witness，不是产品安全失败。

真实源码 `live-sibling-completion` 探针已复跑：删除 `s.id not in failed` 后真实 `native-ad-PENDING` 红，finally/cmp 字节恢复后绿。其余未变 12 个权限、金额、冻结等探针复用，不全量重跑。受影响 completion/receipt 选择 `16 passed, 32 deselected`；真实 HTTP 四态 `4 passed`、blocked 对照 `4 passed` 与此前受影响代表 `13 passed` 复用 R1 修复日志。产品源码最终无 diff，未发现新的产品反例。完整证据与边界见 `docs/validation/2026-11-13-native-m2-r1-formal-sync.md`。

这里只证明上述小边界内的聚合安全性；没有 full refinement、composition、liveness、调度推进、任意规模或真实平台/生产证明。不把 R1 摘要中的“两条新 Ad 创建后最终 PARTIAL”解释为改变批次终态：完整报告、Spec 与真实日志仍是两计划各缺一 Ad MATCH→计划 PARTIAL→批次 FAILED。M2 仍待 root Spec R2。

新增恢复 owner 源码 sync commit：`d4b63a34b4366a4abbd00269e02bc6c51386916b`。其余六核心 owner 语义未改，沿用上文证据。`Submission.Recover` 消费候选数0/1/2、full、scope，只允许唯一完整原范围候选补充原对象；不改变 sends/body/snapshot。scope 抽象代表原 Unit/计划键、账户、冻结路由和原请求范围，**不是这些字段的完整实现证明**；当前实现映射为 reconciliation._claim 的原步骤锁、digest/route 校验，reconciliation_scan.advance 的分页完整性/计数和全字段 comparison，以及 _finish 的活跃 claim/授权复核和原步骤证据事务。模型的 Recover 折叠一次扫描结果提交；SDK读取与 claim/finish 是分开的真实事务，尚未在模型细化读期间 scope 变化，依赖原有 claim fence，不能称 refinement。

扩展运行（不会重新执行旧11变体/10探针）：

```sh
FORMAL_ONLY=recovery TLA_JAVA=/tmp/tkada-native-series-20261112/m2-implementation/formal-tools/jdk-21.0.13+9-jre/Contents/Home/bin/java TLA_JAR=/tmp/tkada-native-series-20261112/m2-implementation/formal-tools/tla2tools.jar FORMAL_OUT=/tmp/tkada-native-series-20261112/m2-implementation/jb17-formal-rerun .agents/skills/formal-verify-native-series-batch-submission/run.sh && python3 .agents/skills/formal-verify-native-series-batch-submission/source_checks.py --runner /tmp/tkada-native-series-20261112/m2-implementation/run-test.py --out /tmp/tkada-native-series-20261112/m2-implementation/jb17-source-rerun --only recovery-ambiguous
```

实际 TLC 正常一计划三层 1034、两计划三层175454 distinct；Authority1456。R10多候选认领反例45、R9重发187、R13伪完成14 distinct。R10实际源码变异 `matches != 1`→`matches < 1` 后，多候选从UNKNOWN错误变SUCCEEDED，HTTP恢复检查断言失败；仓外备份/finally/cmp恢复后1 passed。日志见 validation。恢复动作后仍检查全部已有Submission不变量；未削弱既有R5/R7/R8/R9/R13，R12复验未修改的Authority模型。既有2广告正常模型本轮未跑，新增恢复模型是每计划一广告的边界。

明确未覆盖：confirmed-absent/原已知拒绝解析、T16–T18真实纵切、浏览器恢复、恢复期间授权变化的完整组合、creative配对/重复项是原比较器测试而非模型证明。Recover当前不含negative-evidence分支；零候选始终UNKNOWN。R11仍归JB18，但JB17尚不放行。禁止凭此候选推进取消实现。

## JB17 T18 历史拒绝条件路径（2026-11-13）

源码映射基于 `9fed527926c795fda1d42baa529d2aa09e056be3`，本次没有产品源码修改。
`HistoricalReceipt` 是**合成历史持久输入**，不是当前 SDK 的自然状态转移：原
StepEvidence.RESULT_UNKNOWN 持有同一 source/attempt 的明确拒绝回执，
`known_no_effect_rejection` 对 ADGROUP 40002 或 AD 40002/51002 证明无创建效果，
`_recover_known_rejection` 明确排除 41021。模型 historical 集合仅抽象该已绑定事实，
不把空 GET、Timeout、缺字段或任意非零码当作 negative proof；不表示平台新增查询能力。

`RejectHistorical` 映射原 owner 的事务内 FAILED 与 REMOTE_REJECTED_RECOVERED；
NegativeProof 禁止无该历史事实就 UNKNOWN→FAILED，HistoricalPreservation 检查该动作不改
所有对象 ID、sends、body、snapshot。原 R9 WriteOnce、R10 Recovery、R13 Completion 保留。
双计划 NoHistoricalTerminal 的**预期反例**提供可达 witness：两计划父级创建后历史拒绝关闭，
无 UNKNOWN、无完整 MATCH，Publish 按原表达式得 FAILED；不是正常安全性质失败。

```sh
FORMAL_ONLY=historical TLA_JAVA=/tmp/tkada-native-series-20261112/m2-implementation/formal-tools/jdk-21.0.13+9-jre/Contents/Home/bin/java TLA_JAR=/tmp/tkada-native-series-20261112/m2-implementation/formal-tools/tla2tools.jar FORMAL_OUT=/tmp/tkada-native-series-20261112/m2-implementation/t18-formal .agents/skills/formal-verify-native-series-batch-submission/run.sh
```

实际运行 N=1,K=3：10,718 generated / 1,628 distinct；N=2,K=3：5,198,052 /
437,408，均通过安全性质与 HistoricalPreservation。negativeproof、recovery、retry、completion
均命中对应不变量。随后 `FORMAL_ONLY=historical-witness` 独立跑新增可达 witness：
312,018 / 40,642，NoHistoricalTerminal 预期反例。R12 Authority 未改复用前轮，不重跑旧全集。

真实接线：test_batch_submit_and_execute[historical-rejection] 经正式 HTTP、process_recovery、
deliver_step(reconcile=True) 与持久证据、Submission GET/事件 GET/submit HTTP。
seed 真实创建两个 Campaign、原 ADGROUP 请求获 transport double 40002；当前 SDK 正确归类拒绝。
fixture 只追加标注旧 RESULT_UNKNOWN 并把源状态预置 UNKNOWN，保留原拒绝证据、request_id、
attempt/attempt_id、body/digest、Unit 与 stamp。恢复不调用私有 owner、不手写 FAILED。
no-closure 与 lost-request 真源码探针分别由终态与请求关联断言拒绝，finally/cmp 后恢复绿。
空 GET 对照复用此前真实 Timeout 的 recover_batch(empty) 证据及 R10 模型；不新增大矩阵。
原模型 composition、任意 scope/lease、liveness 与生产边界仍未证明。
