# JB-16 本地实现候选：尚不可交最终验收

起点 `820a26e9c2fef02b0d0136e8f0a93b8625ad3b1d`，唯一树/分支 `feat-jb13-native-series-m2` / `feat/jb13-native-series-m2`。未切分支、未创建工作树或线程、无下级代理。保留原 `.venv` / `node_modules` 未跟踪软链。未推送、PR、部署、Linear 修改或真实平台调用。Nowledge 定向检索无命中；使用本树 Spec、完整 JB-16 快照与 JB-14/15 动态证据。

## 已实现的消费者迁移

- 原生 `validate` 以当前 Unit 和 plan_key 复核；每次先读 `read_batch` 校验 manifest/请求摘要，再用该计划当前 checkpoint、账户权限、冻结路由、素材顺序和地区/CTA。历史 M1 payload 不重写。
- `execution._frozen` 及 AD 素材最终围栏传入当前 Unit；封面分支从 Unit 原请求读取。自己的 Campaign/group 排除仅限当前 Unit。
- 既有 `queue_step` 增加原生逐 Unit 准入；拒绝落 LOCAL_FAILED，兄弟 Unit 保留。TTL 允许进入既有刷新流程。没有新的执行器或 gateway。
- 既有提交唯一约束/锁/幂等键沿用，批次提交读取完整冻结 manifest。未增加第二 ENABLE。
- `unit_native_matched` 使用每 Unit 完整原请求和实际三级/全部 Ad readback MATCH；批次 `native_plans` 从原 ExecutionStep 派生，不持久化新状态。EXCLUDED 保持可见。失败祖先的未发送后代按原回执规则派生失败，已创建 ID 保留；只有完整计划 MATCH 才计完成。
- 确认框展示纳入/排除数量、账户、Series、双身份、地区、观察范围、总额和直接 ENABLE。结果页展示逐计划状态、原因、远端 ID 与消耗风险。API schema 与真实生成 client 同步。

## 实际命令与证据

所有 Python 从本树 backend，经以下前缀使用指定 M2 PG/Redis 和 Python 3.14：

```sh
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py
```

未读取 `.env`、输出私有测试配置、清空 Redis 或修改共享全局配置。无新迁移，现有隔离测试 fixture 自建 schema 应用至 native_batch_preview；无生产数据库操作。

1. 前置 `<runner> python -m pytest tests/modules/builds/test_native_batch_preview.py -k 'two_prepared or browser' -q --tb=short`：2 passed / 66.88s。覆盖两 INCLUDED 完整冻结和浏览器两 INCLUDED + 一 EXCLUDED。此时 UI 仍为 JB-15 禁用提交。
2. 新逐 Unit 测试先红：旧 validate 不接受 unit_id，1 failed / 16.67s。迁移后加 M1 `[scene]` TTL：2 passed / 26.18s，日志 `jb16-unit-ttl.log`。TTL 实际刷新原 owner，保持 revision/digest/父级 body/digest/attempt。
3. 初次并发提交测试因测试 Session 退出后 ORM 对象失效报 DetachedInstanceError，改为 expire_on_commit=False；随后创建链通过，但旧 source fixture 清理不保留构建历史导致 FK teardown 错误。改为复用 M1 retain_build_history fixture。不是放松产品规则。
4. 双账户 SDK 创建/重复 worker + 实际 readback：1 passed / 13.31s，`jb16-match.log`。transport double 保存官方 SDK 收到的创建请求并提供读取；字段独立语义沿用 M1 readback oracle，本测试不独立证明每字段比较正确。
5. 成功/部分/零完成/UNKNOWN/拒绝初次 2 passed、3 failed / 66.35s，`jb16-outcomes.log`。失败聚合仍 RUNNING；调用真实 dispatch 后仍 3 failed / 33.09s。修复失败祖先未发送后代派生后 3 passed / 34.13s，`jb16-outcomes-fixed.log`。UNKNOWN 由 SDK POST 丢失响应触发，没有重发。
6. 页面确认初次因 EXCLUDED 无 occupancy 崩溃；修为显示“未取得观察证据；本项已排除”，后 1 passed / 32.51s，`jb16-browser-fixed.log`。原生 host 的 API 登录依赖绑定合成操作者；buildsBoundary 提供页面壳，业务 preview/submission HTTP 穿过正式 router 和真实 PG。该轮与 completion 破坏探针时间有重叠，因此不能将其作为最终源码的无污染验收；末次串行复验记录在下方。
7. `<runner> python /tmp/tkada-native-series-20261112/m2-implementation/jb16-probes.py`：实际源码 completion 无条件 true、当前计划强制首 Unit 均 RED，分别 24.15s / 15.60s。原文件仓外备份、finally 恢复并逐字节核对。仅覆盖两个性质，不能冒称所有要求的破坏探针完成。
8. `bun run --cwd frontend build`：TypeScript、正式/demo 构建通过，`jb16-build-final.log`。Ruff 检查/格式与 diff 检查，最终结果追加下方。

运行日志均在 `/tmp/tkada-native-series-20261112/m2-implementation/`，不提交私有 env 或数据库。

## 形式工具与边界

已创建 `.agents/skills/formal-verify-native-series-batch-submission/`，包括 SKILL.md、TLA+ 模型、cfg、runner，且真实运行。Java/工具只下载至上述临时目录，不改全局。Temurin 21.0.13+9；TLC 2026.11.06.222357 rev d8fb231，v1.8.0 asset。

最终草模：两计划，每项四对象；115300 generated / 16564 distinct、深度 35，无反例。模型初版访问前驱索引 0 报错，改 IF 后通过。这是模型修正，不是实际实现缺陷。金额只以整数百分之一单位建模，不是任意 Decimal 证明。模型仍为草稿，源码/原子性映射和所有关键性质的对应破坏变体尚不完整；不能声称 R5/R7/R8/R9/R12/R13 形式验收完成。R10/R11 未实现。

## 未解决关键缺口（不得推进 JB-16 Done）

- 完成形式 skill 的逐性质源码映射、真实源码破坏探针及对应模型反例；明确检查事务到物理网络之间的时间窗口。
- 完整入队/最终物理发送的跨 tenant/BC、操作者代数、路由绑定代数、撤账户、原 body 和摘要竞态矩阵；当前测试只证当前 Unit、确定 ACTIVE 局部阻断与 M1 TTL。
- 两 INCLUDED + 一 EXCLUDED 从浏览器确认到同一 Submission 的完整执行/readback 合一纵切；目前页面提交与双 Unit 创建/readback 为分开的真实路径。
- 单批同/不同 request key 在全部终态与 needsreview 的真实双会话并发矩阵；已有首次并发、重复 worker 和后续重放，不等于全矩阵。
- 本次 batch 新失败全部修复后应由 root 检查；不能把模型草稿、HTTP 202 或局部绿当整票完成。

## JB-17 消费合同

逐计划结果为 SubmissionView.native_plans，EXCLUDED 可无 unit_id、occupancy 可为 null；不生成对应步骤。INCLUDED 的唯一请求仍在 Unit.scene_snapshot.native_request，plan_key 在同快照。UNKNOWN 继续保留原 step/body/digest/attempt，只允许原只读恢复 owner 处理；不得创建新框架或重发。JB-17 不应在本票关键验收缺口未完成时直接前移。

## 末次串行复验

源码探针全部恢复后，无并行源码修改的末次业务运行：

```sh
<runner> python -m pytest tests/modules/builds/test_native_batch_execution.py 'tests/modules/builds/test_native_execution.py::test_native_expired_evidence_continues_frozen_plan[scene]' tests/modules/builds/test_native_batch_preview.py -k 'validate_current or batch_submit or expired_evidence or browser' -q --tb=short
```

8 passed / 12 deselected / 80.09s，日志 `jb16-final.log`。涵盖当前 Unit、单项新增 ACTIVE 不阻兄弟、完整 MATCH、明确拒绝、部分 MATCH、零完整 MATCH 但有父级 ID、SDK 响应丢失 UNKNOWN、重复提交与 worker、原 M1 TTL、两 INCLUDED + 一 EXCLUDED 页面确认。页面壳/认证仍为上述合成边界。不是完整 task/queue 进程验收：调用真实调度/执行 owner，但 `_require_bounded_worker` 在测试中绕过宿主检查，未启动 Celery 消费进程。

Ruff 初次提示新增测试 import 顺序，已 fix；最终定向 check/format 与 git diff --check 通过。前端最后改动后正式/demo 构建已通过。没有重跑未受影响的旧 Mini/M1 全套。

TLC jar SHA256：`e1f11b75c4b5bc4478ba0ac5cb6ae28e40a01b0016bc33e0aa97da22dfdbcd7f`。扩展模型命令：

```sh
TLA_JAVA=/tmp/tkada-native-series-20261112/m2-implementation/formal-tools/jdk-21.0.13+9-jre/Contents/Home/bin/java TLA_JAR=/tmp/tkada-native-series-20261112/m2-implementation/formal-tools/tla2tools.jar .agents/skills/formal-verify-native-series-batch-submission/run.sh
```

日志 `jb16-tlc-expanded-fixed.log`。以上结果不关闭前述关键缺口，不宣称 JB-16 完成或可验收。
