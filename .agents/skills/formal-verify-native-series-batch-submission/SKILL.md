---
name: formal-verify-native-series-batch-submission
description: 修改原生批次冻结、批准、预算、执行围栏或完成判定时，运行 TLC 有界交错及真实源码串行破坏检查；保留尚未证明的范围。
---

# 提交与冻结验证（候选，JB-16 仍待最终验收）

目的：防止旧批准发出新广告、重复创建、批准范围被改变、预算算错或部分创建被显示为全部完成。
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

省略 `--all` 仅运行模型，不能声称跑过源码破坏。换机器需提供已配置隔离 PG/Redis 的等效 runner；本 skill 不安装环境、不读取或输出私有配置。所有 pytest 经 runner 从 backend 执行。源码恢复失败立即停止，先从仓外备份恢复，不提交污染。
工具固定 Temurin 21.0.13+9、TLC 2026.11.06.222357 rev d8fb231（v1.8.0 asset）；models.py 验证 jar SHA256 `e1f11b75c4b5bc4478ba0ac5cb6ae28e40a01b0016bc33e0aa97da22dfdbcd7f`。

## Bounds 与假设

- Submission：一份预览，1 或 2 个 INCLUDED 计划，另一个 EXCLUDED 计划；单计划模型为 Campaign→AdGroup→两个 Ad（兄弟 Ad 可交错），双计划模型每计划为 Campaign→AdGroup→一个 Ad。每个 Ad 的 creative 比较压缩为一个“全部 creative MATCH”事实，与对象 readback 分开；不证明实际多重集合比较器。
- 冻结选择金额 1 或 2 个最小货币单位（这里只解释为 USD cents），每个 INCLUDED Campaign 一次。整数模型不是任意币种/任意精度 Decimal 证明。真实生成检查为 18 种小数位、1–3 计划、0/1/7 广告，共162组及空集；独立等差数列整数 oracle。EXCLUDED 过滤发生在 native_preview，真实 PG 用例负责接线。
- Claim、arm、持锁检查、send、响应结果、receipt、readback 分开。arm 后退出记 UNKNOWN，即使物理发送未发生也不盲重发；没有 R10 的 confirmed-absent 重发。发送失败、丢响应、明确成功均非确定完成。
- 重复提交/worker 抽象为同一动作任意重复使能；数据库事务锁/唯一约束让成功提交和每次 claim 各自原子，不把网络和本地回执视为原子。没有显式两个进程的局部 lease 状态，不能声称证明租约实现。
- Authority：实际 actor scope 折叠为一个 generation，1→2→3 对应撤权/再授权；另存逻辑 epoch 防止 ABA 变体靠相等值假绿。connection、binding、Draft revision 各1→2。历史 NULL=0；批准前可发生变更，批准后不刷新 stamp。无关 scope 是独立变化，不使批准失效。
- scope 锁阻止本地权限/路由/意图改变；Exit 模拟正常释放或进程退出。Send 与 Receipt 分开，丢响应落 UNKNOWN。真实 HARD_LIMIT/task_deadline 限制锁期，模型没有时钟，也不证明超时必定释放；真实 5.2 秒持锁证据见 validation。
- 无公平性假设，仅安全性，不声称最终完成或无死锁。TLC `CHECK_DEADLOCK FALSE` 允许终态与无授权停滞；不检查 liveness。
- 两个模型分开，没有 refinement/composition 证明；Submission 中锁后的 intent 变化交错由 Authority 的 HeldAuthority 单独约束，不能以两个模型分别通过宣称已证明二者接线。

## 性质、反例与源码对应

已读源码对应版本 **be06268759d6b72013120d936b9fe2b458afc4ce**。仅以下 owner 已核对；不是未来提交占位符。覆盖代码改变后先重读事务/调用方，运行对应检查，再推进 sync commit。哈希/关键词不能替代动态证据。

| 性质 / 破坏变体 | 反例含义 | 真实 owner 与动态对应 |
|---|---|---|
| R5 FrozenBody / body | arm 后改正文，原 snapshot 不再匹配 | execution_state.arm_request；0006_build_execution.build_execution_scope（armed request 不可变）；send-lock 用例实际 SQL UPDATE 被拒。源码 unit-scope 探针强制首 Unit，被 test_validate_current_plan 拒绝；digest 探针忽略 manifest，被真实 digest 变异检查拒绝。body 模型变体不是 DB trigger 源码变体。 |
| R7 ExactBudget / budget | freeze 把 Campaign 金额乘对象数 | frozen_requests.native_batch_budget；native_preview.generate_batch 的 expanded→total 与 INCLUDED 写入循环。source_checks 的 double/float 在真实函数上产生断言失败；生成预期不调用被测函数计算。 |
| R8 UniqueSubmission / unique | 同预览第二次 Submit 创建第二份批准 | submissions.submit_preview 的 request-key advisory lock、Draft/Preview FOR UPDATE、0006_build_execution.uq_submission_preview；test_batch_submit_and_execute 的双 PG session 同/不同 key 重放。source_checks unique-replay 在新 key 分支遗忘已有 Submission，真实 PG 回归因返回范围冲突而非原 identity 失败；未移除唯一约束，未产生第二份持久 Submission。模型 unique 的第二份创建与源码 replay 的错误拒绝是 R8 的不同违反，不能宣称两者逐步等价。 |
| R9 WriteOnce / retry | send→unknown→重新排队→第二次 send | submissions.claim_step、execution_state.arm_request/record_unknown、execution.process_step；同一真实批次用例观察重复 worker 的 transport 次数/ID/body 不变。模型不含许可的 confirmed-absent 恢复，交 JB17。source_checks unknown-retry 同时放松 process_step 与 claim_step 的 UNKNOWN/armed、未过期 lease 及原请求重试证据检查；SDK POST 丢响应后同消息重复执行，transport 新增写被独立次数断言拒绝。原 body、stamp、DB 约束和 SDK 编译保持真实。 |
| R5/R9 Excluded / excluded | Submit 给排除计划生成步骤 | native_preview.generate_batch/read_batch；submissions.expand_submission。真实批次用例明确 EXCLUDED 没有 Unit/步骤/预算/写；未新增过滤绕过源码探针。 |
| R13 Completion / completion | 尚无三级及 creative MATCH 就报告完成 | native_completion.unit_native_matched/batch_results；source_checks completion 无条件 True，真实零完整 MATCH 场景失败；恢复后保留已有 Campaign ID。另有 receipt 探针将相同键重放硬写 COMPLETED，UNKNOWN 场景拒绝。模型 Publish 分别检查完整、部分、无完整与 UNKNOWN；实际回执不是持久调度 status。 |
| R12 FinalFence / stamp | 批准→撤权→再授权→旧批准发送 | tenants.permissions.build_authorization_stamp/require_build_authorization_stamp；submissions.submit_preview 唯一 INSERT 冻结；dispatch.queue_step、execution._frozen 两边界消费。真实 stamp 比较绕过被 user/superuser regrant send 检查拒绝。 |
| R12 FinalFence / route | 批准后 connection/binding/revision 改变仍发送 | native_execution.validate、accounts.routing.verify_route；既有 test_batch_fences queue/send 证据复用；本轮没有再做三种源码绕过。 |
| R12 FinalFence / null | 历史 NULL 批准获准发送 | actor_authorization_stamp.upgrade 的历史 NULL 和不可变 trigger；test_legacy_approval_readable_but_cannot_send 已有实证复用。未做真实 NULL 绕过源码探针。 |
| R12 HeldAuthority / lock | 锁内撤权改变已准入权限 | execution.process_step.send_scope→native_execution.lock_send_authority→_frozen→SDK request_scope→scope exit；真实 account-lock 删除 BCAccountAccess 行锁，5.2秒 transport 内独立 PG 撤权成功，断言拒绝；恢复后绿。 |
| R12 FinalFence / aba | 再授权复用旧 generation，旧批准重新发送 | actor_authorization_stamp.upgrade 的 DB trigger/唯一 sequence；现有成员删除重建/权限恢复证据。未改真实 trigger 为复用代数，模型反例不冒充源码反例。 |

## 结果解释与核心性质覆盖

每个模型变体必须出现指定 invariant violation；解析错误、环境错误或非零 exit 不算反例。source_checks 必须 exit 1 且含断言失败；两个新 case 还要求明确的 R8/R9 行为断言标记，恢复必须 exit 0。任何正常模型反例先按 action trace 对照真实事务，区分模型错误与产品错误；不要弱化性质来通过。

本轮六个核心性质 R5/R7/R8/R9/R12/R13 均已有代表性真实源码破坏→行为红→仓外备份 finally 恢复/cmp→绿，以及对应有界模型/反例。已有8个源码探针、3个正常模型与11个模型反例复用 v2 证据；新增 unique-replay、unknown-retry，详见 validation v3。产品源码仍与 **be06268759d6b72013120d936b9fe2b458afc4ce** 相同，不虚构新 sync 版本。JB16 放行仍归 root。

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

R10/R11 留给 JB17/18：在相同原请求、Unit、账户、路由与原批准 stamp 下加入只读恢复候选集（零/一/多个、完整/不完整 MATCH）、confirmed-absent 的明确批准以及取消 PENDING 与 armed/sent 的边界。不能把 UNKNOWN 自动转回 queued、回填历史 stamp、重建成功兄弟或通过取消隐藏远程 ID。扩展后重跑所有既有正常/破坏模型及受影响源码检查。根验收前 JB17 不放行。
维护入口：`/maintain-formal-verification-skill`，先核对源码映射再推进覆盖版本。

## JB17 R10 候选扩展（未达到整票放行）

新增恢复 owner 源码 sync commit：`d4b63a34b4366a4abbd00269e02bc6c51386916b`。其余六核心 owner 语义未改，沿用上文证据。`Submission.Recover` 消费候选数0/1/2、full、scope，只允许唯一完整原范围候选补充原对象；不改变 sends/body/snapshot。scope 抽象代表原 Unit/计划键、账户、冻结路由和原请求范围，**不是这些字段的完整实现证明**；当前实现映射为 reconciliation._claim 的原步骤锁、digest/route 校验，reconciliation_scan.advance 的分页完整性/计数和全字段 comparison，以及 _finish 的活跃 claim/授权复核和原步骤证据事务。模型的 Recover 折叠一次扫描结果提交；SDK读取与 claim/finish 是分开的真实事务，尚未在模型细化读期间 scope 变化，依赖原有 claim fence，不能称 refinement。

扩展运行（不会重新执行旧11变体/10探针）：

```sh
FORMAL_ONLY=recovery TLA_JAVA=/tmp/tkada-native-series-20261112/m2-implementation/formal-tools/jdk-21.0.13+9-jre/Contents/Home/bin/java TLA_JAR=/tmp/tkada-native-series-20261112/m2-implementation/formal-tools/tla2tools.jar FORMAL_OUT=/tmp/tkada-native-series-20261112/m2-implementation/jb17-formal-rerun .agents/skills/formal-verify-native-series-batch-submission/run.sh && python3 .agents/skills/formal-verify-native-series-batch-submission/source_checks.py --runner /tmp/tkada-native-series-20261112/m2-implementation/run-test.py --out /tmp/tkada-native-series-20261112/m2-implementation/jb17-source-rerun --only recovery-ambiguous
```

实际 TLC 正常一计划三层 1034、两计划三层175454 distinct；Authority1456。R10多候选认领反例45、R9重发187、R13伪完成14 distinct。R10实际源码变异 `matches != 1`→`matches < 1` 后，多候选从UNKNOWN错误变SUCCEEDED，HTTP恢复检查断言失败；仓外备份/finally/cmp恢复后1 passed。日志见 validation。恢复动作后仍检查全部已有Submission不变量；未削弱既有R5/R7/R8/R9/R13，R12复验未修改的Authority模型。既有2广告正常模型本轮未跑，新增恢复模型是每计划一广告的边界。

明确未覆盖：confirmed-absent/原已知拒绝解析、T16–T18真实纵切、浏览器恢复、恢复期间授权变化的完整组合、creative配对/重复项是原比较器测试而非模型证明。Recover当前不含negative-evidence分支；零候选始终UNKNOWN。R11仍归JB18，但JB17尚不放行。禁止凭此候选推进取消实现。
