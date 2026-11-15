# M4a Standards 独立 R2/3

**PASS**。未解决 P0=0、P1=0；本轮新增 P2=0、P3=0。仅放行 R1 修复 scope，不代表 M4a 整体完成。

## Refs

- 树：`/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a`。
- 原全范围 base：`b0f3b1443e7fedc19f297d078a706d5f8ef50040`。
- 本轮固定点：`573ebc8fc2f50d212856c9d8ec23a29645ae3b59`；head：`3b9f44d0d933d58e82ab5d1d46c2a2b7d5afae3e`。
- 亲查 HEAD/merge-base；`git diff 573ebc8fc2f50d212856c9d8ec23a29645ae3b59...3b9f44d0d933d58e82ab5d1d46c2a2b7d5afae3e`：14文件，3提交（95c5f272、7a91da71、3b9f44d0）。其余继承 [R1 Standards](/tmp/tkada-native-series-20261112/m4a-implementation/review-r1-standards.md) 与 [R1 Spec](/tmp/tkada-native-series-20261112/m4a-implementation/review-r1-spec.md)。
- 权威：[JB-24](/tmp/tkada-native-series-20261112/m4a-implementation/JB-24.md)及同目录 JB25–30；已读 code-review skill、AGENTS.md、CONTRIBUTING.md、development.md/引用部署指引、docs/engineering-baseline.md、当前视觉计划。适用规则：tenant/BC 隔离、单 owner、同事务持久化、评估旧消费者、保留外部接口契约（视觉计划:20）。

## 修复核对

- R1反例：full t0/a1=20，target C1 t1/a1=100。`publish.py:496–544` 将 t1、job publication_id、digest、合同、精确 scope 与金额同事务保存；target version=0，账户 t0/V/H 不推进。`row_set.py:210–225,320–369` 的普通 Ad、贡献子集均读事实证据；无旧 full 不借账户状态追认。
- 同类：无 direct 的 Campaign/Adgroup/account/drama 逐子求和并保留 t0/t1；full parent/empty direct 仍权威。`dimension_query.py:1402–1433` 复用同一选择器；`frozen_result.py:88–107` 冻结 SELECTED_FACT 发布图；详情读 frozen_row，exports.py:195/373 复制并输出该图。未发现新增权限入口、跨 tenant/BC 连接或第二发布 owner。
- R1 creative P2关闭：`detail.py:317` 在共享函数作用域绑定 serializer；合法 creative+snapshot 不再读取未绑定局部变量。legacy NULL 与未请求 Ad ratio 分别保留 LEGACY_UNKNOWN/NOT_REQUESTED；不要求新增采集。

## 验证边界

亲跑：targeted.py **8通过**；snapshot.py **128通过**；targeted.py --broken-account-stamp **AssertionError/exit1（预期拒错）**；diff --check通过。模型假设原子发布/RR捕获，不证明真实SQL或无界并发。

继承：[P1记录](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a/docs/validation/2026-11-14-native-m4a-r1-targeted-provenance.md)的134/17与两项真实源码probe RED→恢复GREEN；[root记录](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a/docs/validation/2026-11-14-native-m4a-r1-root-p2.md)的28、三个精确旧产品RED→GREEN、完整链1passed/6deselected/68.06s。原JB25–30证据按原覆盖继承，未冒称本轮亲跑。

动态缺口/Unknown：本轮未运行共享PG fixtures、源码变异、SDK/Worker/HTTP/browser/CSV、平台或生产验证；不因此自动BLOCK。traffic语义待确认、D0=click24h沿用用户决定。

Codex gpt-6.1-sol/high，自行完成、未派下级。起止仅原未跟踪 .venv/node_modules；源码只读，唯一写入本报告。未操作平台、生产、Linear、分支、提交或推送。
