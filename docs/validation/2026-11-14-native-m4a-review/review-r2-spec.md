# M4a 独立 Spec R2 / 3

**PASS — 未解决 P0: 0，P1: 0；新增 P2: 0，P3: 0。R1 P1 已关闭；R1 两项 Spec P2 已修复。**

只放行 R1 修复 scope，未宣称 M4a 整体完成、集成或上线。审查者为用户指定 Codex gpt-6.1-sol/high；未派下级代理。按 [code-review 技能](/Users/dimon/.agents/skills/code-review/SKILL.md) 的严重度、基线与 R2 规则执行，仅 Spec 轴。

## Refs 与边界

- 树：`/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a`；亲查 HEAD 为 `3b9f44d0d933d58e82ab5d1d46c2a2b7d5afae3e`。
- 原全范围 base：`b0f3b1443e7fedc19f297d078a706d5f8ef50040`；其余范围继承 [R1 Spec](/tmp/tkada-native-series-20261112/m4a-implementation/review-r1-spec.md) 及 [R1 Standards](/tmp/tkada-native-series-20261112/m4a-implementation/review-r1-standards.md)，不重新寻找 P2。
- 本轮精确 diff：`git diff 573ebc8fc2f50d212856c9d8ec23a29645ae3b59...3b9f44d0d933d58e82ab5d1d46c2a2b7d5afae3e`。亲查 merge-base 等于上述 R1 head；3 个提交（95c5f272、7a91da71、3b9f44d0），14 个路径，其中产品为 publish、row_set、dimension_query、frozen_result、detail 五个模块。
- 权威需求：[JB-24](/tmp/tkada-native-series-20261112/m4a-implementation/JB-24.md:48) 及同目录 JB25–30。工作树起止仅原有 `.venv`、`node_modules` 未跟踪项；源码只读。唯一写入为本报告。

## R1 P1 关闭证据

需求原句：[JB-24:56](/tmp/tkada-native-series-20261112/m4a-implementation/JB-24.md:56)“快照固定实际指标结果、所选来源与水位”；[JB-24:194](/tmp/tkada-native-series-20261112/m4a-implementation/JB-24.md:194)“Ad既有campaign_ids定向任务继续发布其精确范围，只满足原冻结需求，不推进整账户日COMPLETE”；[JB-29:18](/tmp/tkada-native-series-20261112/m4a-implementation/JB-29.md:18)“每行冻结本次实际 MetricVector、逐指标 provenance … 实际 scope 和逐日水位”。另核对 JB-24:57 的证据同事务、:202 的 direct/derived 互斥要求。

1. **事实写入不再漏掉 target。** [publish.py:404](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a/backend/app/modules/reporting/publish.py:404) 对 full 和 target 都计算 payload digest，包含真实请求坐标与 campaign_ids；[publish.py:496](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a/backend/app/modules/reporting/publish.py:496) 在原发布事务内给本次 job 写入的 Ad 行追加 publication，含 job publication_id、真实 requested_at、合同、digest、账户/日期/精确 Campaign scope。stamp 还受 tenant、advertiser、day、sync_job_id 约束。只有 full 调用账户 `_set_state`，target 版本为 0，不冒领账户单调版本；未更新行保留自己的旧观测。t0/V/H 维持旧完整性是正确行为。
2. **普通 Ad 与无旧 full。** [row_set.py:224](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a/backend/app/modules/reporting/row_set.py:224) 从实际行 evidence 解释合同；[row_set.py:352](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a/backend/app/modules/reporting/row_set.py:352) 有行时只读该行 publication，不再借账户 s。故 t1 的新金额不会被归入 t0；无旧账户状态也不会把新合同事实误记 LEGACY_UNKNOWN。真实 legacy 无证据保持未知。只有无返回行的完整空桶才使用账户发布。
3. **贡献子集。** [row_set.py:257](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a/backend/app/modules/reporting/row_set.py:257)、[row_set.py:369](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a/backend/app/modules/reporting/row_set.py:369) 明确从 Ad 事实并按 contribution_ids 限制金额和 observations，使用 Ad 自有合同与观测；不拿 full parent 的收益或旧账户 stamp 解释子集。核对 Campaign、Adgroup、account、drama 四种维度调用。
4. **无 direct 的派生父级。** [row_set.py:323](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a/backend/app/modules/reporting/row_set.py:323) lateral 分支逐个读取真实子行金额、evidence 与 remote_id；求和与 NULL 状态改用同一批子事实，保留 CHILD_DERIVED 以及逐子 t0/t1。已有 full parent 时只接 parent 原始证据和自身发布状态；两层 selected view 的既有 NOT EXISTS 继续压制 derived。完整空 direct 通过目录空桶给 NO_RETURNED_ROW，不恢复子级正值。核对 Campaign/Adgroup 两条同类分支。
5. **查询与冻结输出一致。** SQL observations 随同一 facts CTE 进入成员、阈值、排序和 summary；[dimension_query.py:986](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a/backend/app/modules/reporting/dimension_query.py:986) 消费所选 observations，后备证据读取也已改用 `_facts_cte`（:1402），不再独立拼账户 stamp。summary 调用同一 vector_provenance。 [frozen_result.py:87](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a/backend/app/modules/reporting/frozen_result.py:87) 冻结最终向量，并把所选实际 publication 追加至 publication_map，标 SELECTED_FACT，与账户完整性记录分开；derived 的 publication 坐标仍保留实际 Ad 层。详情读取 frozen_row；[exports.py:195](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a/backend/app/modules/reporting/exports.py:195) 复制 publication_map，CSV 读取复制载荷，后续刷新不重算原快照。

R1 反例“full t0 的 a1=20/a2=90，target t1 后 a1=100”现在会保留 a1→t1/精确范围、新 digest/job，a2→t0，账户→t0/V/H；无 full、贡献子集、derived、已有 direct 和空 direct 的同类路径均有上述实际 SQL/producer 分支依据。未发现仍可走通的 R1 P1 反例或修复新增 P0/P1。

## 既有 P2 与 Spec 措辞

- Spec P2 两项关闭：[dimension_query.py:1281](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a/backend/app/modules/reporting/dimension_query.py:1281) 的 legacy NULL 先判 LEGACY_UNKNOWN；:1453 对真实返回但 Ad 合同未请求比率判 NOT_REQUESTED。符合 JB-24:49、:190。新增测试分别断言这两个业务结果。
- 原 Standards creative P2 仅核对修复影响：[detail.py:317](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a/backend/app/modules/reporting/detail.py:317) 将 serializer 导入放在所有实体共享函数作用域，creative+snapshot 不再读未绑定局部变量；不重做 Standards 轴。
- R1 歧义原句 JB-24:122“本次新增采集平台total/D0 ROAS”有“三层均增加”与“父级新增、Ad 保留原请求”两种读法；:190 又规定区分未请求。用户本轮已明确接受后一读法，未额外要求 Ad ratio 采集。此处不判实现错误，不作为 blocker。traffic_scope 未确认、D0 点击后24h 沿已接受合同。

## 验证边界

**本轮亲跑：** targeted.py 正常 8 例 PASS；`--broken-account-stamp` 预期 AssertionError、exit 1（另以 subprocess 明确校验）；snapshot.py 128 例 PASS；精确 fix diff 的 `git diff --check` PASS。模型仅验证有限抽象坐标/冻结关系，不执行产品 SQL或证明任意并发。

**本轮亲查：** 精确 diff、上述实际 SQL/发布/派生/summary/冻结/详情/CSV 消费链，以及新增回归与消融脚本。[targeted 回归](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a/backend/tests/modules/reporting/test_targeted_provenance.py:17) 使用独立金额/时间预期，覆盖有无 full、四维度普通派生/贡献、混合 t0/t1、SQL 阈值/summary、刷新后的旧详情/CSV、两父层 direct/empty。消融脚本确实分别替换账户 observation 和合同归属，要求业务 AssertionError 并 finally 恢复；本轮未运行源码变异。简单模型的冻结等式本身不是产品证明，真实回归承担该绑定。

**继承而非亲跑：** 已读 [P1 修复证据](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a/docs/validation/2026-11-14-native-m4a-r1-targeted-provenance.md) 与 [root P2 证据](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a/docs/validation/2026-11-14-native-m4a-r1-root-p2.md)。作者 134/17、两项 source-probe RED/restored GREEN，root 28、三个 P2 精确旧产品 RED→GREEN，以及 SDK transport→Worker→PG/Redis→HTTP/browser/CSV 1 passed/6 deselected/68.06s 均按原作者/调用方记录继承。亲读 root-p2-red.log，确认三条业务失败（creative UnboundLocalError、ABSENT、NO_RETURNED_ROW）；不把初始 job_id/date-key fixture 错误当业务反例。原 JB25–30 大范围验收不在此轮重跑，也不冒称全在最终 head 亲跑。

未运行共享 PG fixtures、外部平台、生产、Linear、分支/提交/push 操作。重叠 target 时序的无界保证、真实平台语义和生产验收仍 Unknown；不扩大为本轮新 finding。无未解决 P0/P1，故本轮修复 scope 的 Spec verdict 为 PASS。
