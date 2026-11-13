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

## Fresh-context 运行修复（起点 9805d8d，仍非 JB-16 完成）

唯一写树/分支不变；开工 HEAD `9805d8d02df35994368769d81759bc41d43d6f8f`，只有原 `.venv` / `node_modules` 未跟踪软链。读取本树 AGENTS、外层业务 GLOSSARY、本 Spec、完整 JB-16 快照和协调状态；按 tdd/git-commit 执行。Nowledge 精确检索无命中。不派代理、不新建线程/分支/工作树、不改外层/main/Linear，不推送、部署或调用真实平台。形式 skill 未改、草模未重跑；JB-17 未推进。

### 本轮实际修复

1. **真正的发送权限窗口**：原 `execution.before_request` 事务先提交，SDK 再发送，独立 PG 会话能在 transport 边界完成撤权。现在既有 gateway 的 `build.create_*` request scope 在准入后进入执行 owner 的 `send_scope`。原生发送持有当前 User/Tenant/Membership、BC、connection、binding、账户/grant、ConnectionAuthorization、Draft/Preview 的共享行锁，当前 step 和 Submission 的原排他锁也保持到一次 SDK 请求退出；锁内重新执行原 `_frozen`、当前 Unit validate、执行 owner、取消及 AD 素材围栏。没有新执行器或 HTTP gateway。
2. **慢请求锁期限**：默认 bounded_session 的 5 秒 idle timeout 会在 SDK 慢请求期间释放锁。5.2 秒 transport 延迟真实复现 UNKNOWN；物理发送 scope 改用既有 HARD_LIMIT 上限且仍受 task_deadline 限制；非发送预查维持 5 秒。此路径明确例外于此前“不跨网络持事务”的实现：一笔本地授权锁事务覆盖一次有界请求；撤权会等待该已准入请求结束，先提交的撤权则阻止新发送。不承诺远端平台权限更新和本地数据库原子。
3. **重放回执过时**：`submit_preview` 相同请求键和不同键重放原先直接返回持久化 `Submission.status`，实际全部 MATCH 时仍返回 QUEUED。两条分支现在复用 `get_submission` 的实际聚合状态，原唯一约束、幂等账本、步骤/范围不变。
4. **页面冻结入口遗漏**：真实工作区准备成功后，`BuildPreparationPage` 仍要求计划数等于 1，批次按钮永久禁用。移除旧单项限制并更新 `NativeCheckpointCard` 的旧“尚未开放”提示。不是放宽后台资格：实际 prepare、freeze 和确认仍经原 owner。

### 真实运行证据

所有 Python 命令仍从本树 backend，经 `python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py` 使用固定 Python 3.14、M2 PG/Redis。未读取或打印私有 env，没有 FLUSHDB、停 PG 或修复共享软链。下列日志都位于 `/tmp/tkada-native-series-20261112/m2-implementation/`。

- `python -m pytest tests/modules/builds/test_native_batch_execution.py -k send-lock -q --tb=short`：`jb16-send-lock-red.log` 1 failed / 13.23s（独立 PG UPDATE 未被锁）；初修 `jb16-send-lock-green.log` 1 passed / 15.00s。最初探针在 SDK 线程内使用 pytest.raises，pytest 的 BaseException 使 SDK future 等待不结束；只终止该自有 pytest PID，改为 transport 记录结果、主线程断言。这次宿主问题不算业务 red。
- `-k 'execute and match'`：`jb16-receipt-red.log` 真实复现回执状态过时；修复后 match/UNKNOWN 通过，后续全状态最终复验见下。
- `-k fences`：`jb16-fences.log` **10 passed / 106.39s**。真实保存→prepare→freeze→submit→步骤展开，从入队前和入队后两个时点分别撤第二账户 can_build、停用操作者、增加 connection authorization_revision、增加 binding revision、通过真实 PATCH 修改 Draft。调用 `queue_step` 和 `process_step`，不是直接 validate；transport 写计数不增，兄弟已成功 CTA 保持原 ID。撤操作者导致执行入口返回 DomainError 的情形不伪造平台拒绝。该组源码在最终检查前仅格式化，结果复用。
- 同一 `test_batch_submit_and_execute[browser]`：真实页面打开保存的三计划工作区，点击准备→真实任务推进→点击冻结→确认 INCLUDED=2/EXCLUDED=1→取得真正 Submission。随后同一个 Submission 经 `process_submission`、`queue_step`、`process_step`、官方 SDK transport double 创建，再由 `process_reconciliation` 实际读取 Campaign/group/ad 与全部 creative，最后浏览器打开该原 Submission 结果页，显示两 COMPLETED、一 EXCLUDED、账户/Series/远端 ID、总状态已完成及消耗风险。EXCLUDED 无 Unit、步骤、预算或写。没有 mock Submission/service/readback helper。
- 页面纵切先在“生成搭建预览” disabled 失败，`jb16-browser-combined.log` 1 failed / 2 passed；移除遗留单项限制后 `jb16-browser-fixed2.log` 1 passed / 28.55s。进一步接上真实入队后，`jb16-scheduled.log` 两个夹具问题：SQL text 将 JSON 的 `:true` 当绑定参数；结果页 ID 展示无固定排序而测试假设 Campaign 在 group 前。分别改 jsonb_build_object 和逐层无序断言，不改产品结果。`jb16-scheduled-fixed.log` **2 passed / 56.49s**。
- `jb16-lock-timeout-red.log` **1 failed / 21.27s**，5.2 秒传输延迟超过旧事务 idle timeout；上述 scheduled-fixed 已覆盖期限修复，SDK transport 内独立 PG 撤权被 lock_timeout 拒绝。每次实际发送的 operation_status=ENABLE；三级实际回读 MATCH 后才完成。
- 全部成功、部分成功、零完整 MATCH、UNKNOWN、全部拒绝，各自两独立 PG session 以同/不同 request key 并发重放；同 Submission、当前回执、步骤集合、原 body/digest/ID 及 transport 写次数不变。零完整 MATCH 仍保留 Campaign ID。成功/UNKNOWN 重复 worker 不重建；旧 dispatch revision 在真实入队之后无新 write。旧首次并发唯一性证据继续沿用，新增的是当前回执与终态/待核查。
- 数据库变异实际尝试修改整批 config/digest、Unit scene_snapshot、已 armed request_body/digest、step tenant/BC，全被现有 trigger/约束拒绝；没有禁用 trigger 人造可变历史。跨 tenant 的真实 process_step 被拒绝且零写。此证据证明不可变数据边界，不虚称后续 validate 读到了不存在的被篡改行。

截图为同一 Submission 的 `/tmp/tkada-native-series-20261112/m2-implementation/jb16-confirm.png` 和 `jb16-result.png`。认证由测试宿主绑定合成操作者，导航/租户壳为 buildsBoundary；账户目录和已有上传映射为合成输入。测试宿主临时 `/test-drive/{task_id}` 仅调用原准备 continuation/Scene/material owner；无新外部 Celery 进程。执行绕过 `_require_bounded_worker` 宿主检查，回读模拟有界 Celery host 元数据；真实数据库、Redis、任务入口、SDK 编译和 transport/readback 链参与。Vite 共享字体 allow-list 警告未改共享软链。

### 错误探针及最终检查

串行近似错误：省略账户 grant 行锁，`jb16-runtime-probe-account-lock.log` 1 failed / 24.67s；将相同键重放回执硬写 COMPLETED，`jb16-runtime-probe-receipt.log` UNKNOWN 用例 1 failed / 21.43s。两份源文件仓外备份、finally 恢复、字节比较和 cmp 均通过。没有与其它业务验收同时修改源码。旧 completion/首 Unit 探针直接复用，不重复做。

最终恢复后命令：

```sh
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest tests/modules/builds/test_native_batch_execution.py 'tests/modules/builds/test_native_execution.py::test_native_six_creates[None]' 'tests/modules/builds/test_native_execution.py::test_native_expired_evidence_continues_frozen_plan[scene]' -k 'not fences' -q --tb=short
```

结果及构建追加于下。M1 TTL、ACTIVE 局部阻断和实际 MATCH 定向复验；PARTIAL/STALE/UNKNOWN 占用、稳定抽样、多重集合回读 oracle 未改，沿用 JB-15 与 M1 证据，不声称重新穷举。

### 形式 worker 映射与未满足范围

- 唯一 Submission 仍由原 Draft/Preview 事务锁、request-key advisory lock 与数据库唯一约束负责。终态回执从 `get_submission` 派生，不能以持久化调度 status 建模为完成事实。
- claim、arm、receipt 仍为分开的持久事务。原生 SDK `request_scope → send_scope → lock_send_authority → _frozen/validate → SDK create/get → scope exit` 现在是**本地权限与发送串行化范围**；最终回执另事务保存。模型不能把 arm 当作已发送，也不能把 arm 后崩溃当作可盲重试。锁覆盖一次实际 SDK 调用及有界期限；不是网络与 PostgreSQL 的分布式原子提交。
- 新 grant 锁探针和 5.2 秒延迟反例供后续源码/模型对应；需要建模先撤权后拒绝与先锁定发送后撤权等待两个顺序、锁释放/进程退出、UNKNOWN 和兄弟 write-once。原生 OFFICIAL_MCP 仍由原 Scene owner 拒绝，本轮 send_scope 不宣称修复其它 MCP 业务。
- **R12 操作者授权代数仍未满足/未证明**：当前 User/TenantMembership 权限 owner 无独立单调 actor authorization generation，也无冻结该代数的请求字段。本轮证明冻结 actor_id、当前 active/权限复核及发送期间锁定；不能宣称“撤权后重新授权仍拒绝旧 actor 代次”。没有擅自给权限体系和历史冻结请求增加第二个代数 owner；root 需先明确/补齐此合同再最终验收。connection authorization_revision 与 binding revision 已有真实双边界测试。
- 旧准备完成隔离复用 JB-14（准备 owner 未改）；本轮没有新增“撤权→重新授权→旧准备完成”的交错证明。跨 BC 可变伪行被 DB 拒绝，不将其改称运行时跨 BC 成功读取。
- 形式模型的 Decimal 预算、Completion 集合、R5/R7/R8/R9/R12/R13 源码映射与反例仍由下一位 fresh-context worker 串行补；R10/R11 属后续票。**本提交不能令 JB-16 Done，也不放行 JB-17。** root 先读实际源码/缺口再决定后续。

最终恢复后的 `jb16-runtime-final.log`：**10 passed / 10 deselected / 141.81s**；10 deselected 为已通过且未改语义的 fences 矩阵。`bun run --cwd frontend build` 正式/demo TypeScript + Vite 构建通过（`jb16-runtime-build.log`）。6 个变更 Python 文件的 `ruff check`、`ruff format --check` 均通过，git diff --check 通过。未运行无变化整仓。新持锁范围会使同 Submission 的物理写等待原 Submission 排他锁，且管理员撤权可能等待当前有界请求完成；不宣称本轮测量过吞吐或外部 Celery 硬超时。上述 actor generation 缺口仍是运行 AC，不只是形式文档缺口。

## R12 操作者授权代数（起点 15cb546，待 root / 形式验收）

本轮唯一写树、分支保持不变；起点 `15cb546468cc270249f09cd626635c7040244833`，原 `.venv` / `node_modules` 未跟踪软链保留。未派代理、未改形式 skill、未推进 JB-17、未修改 Linear、未推送/部署/真实平台调用。Nowledge 定向检索未取得相关历史，依据本树 Spec R12 和完整 JB-16 快照实施。

### Owner、批准与历史策略

- 权限仍由 `tenants.permissions.require_tenant` 的 User/Tenant/TenantMembership 事实决定。新增三行各自的 `authorization_generation`；向前迁移 `actor_authorization_stamp` 的 DB trigger 在 User 的 active/superuser、Tenant 的 active、Membership 的 active/role 改变时分配新代数。插入也重新分配，成员删除重建不能 ABA。全局 sequence 仅提供不重复的序号（允许空洞），不作为全局失效版本；只比较实际 actor scope 的三行。普通昵称等非权限修改保留代数。ORM、管理 API/service、后台 SQL 共用触发器，不增加影子角色或权限 owner。
- 代数批准的唯一持久 owner 是 `build_submission.authorization_stamp`。首次明确 `submit_preview` 为真实提交者读取并锁定当前权限，在原唯一 Submission INSERT 时冻结；不是准备任务或预览创建者，也不限制原本合法的操作者协作。Native 预览仅冻结广告意图，提交批准单独绑定权限代数，无第二份 FrozenRequest。平台 superuser 使用 User/Tenant 代数，不依赖无关 Membership。
- `submit_preview` 在 Draft/Preview 锁之前依 User→Tenant→Membership 顺序取得共享锁并核验权限，直到原提交事务结束。重复提交仍返回原 Submission，临时读取当前权限不覆盖原 stamp。DB UPDATE trigger 拒绝 stamp 变更（包括历史 NULL 回填）。TTL、旧准备完成、恢复或重投均无 stamp 写入口。
- `dispatch.queue_step` 原生分支和 `execution._frozen` 共用 `require_build_authorization_stamp`。前者不匹配即 LOCAL_FAILED；后者在原 SDK send_scope 的物理发送事务内再次比较。原 step/Submission 排他锁、User→Tenant→Membership→账户等行锁和 HARD_LIMIT 期限不变；权限锁已持有时的重读不会释放锁。撤权 UPDATE 与代数更新同事务，先撤权则拒绝、先准入发送则撤权等待本次有界请求退出。
- 升级已有权限行获得当前代数，**已有 Submission 不回填**；NULL stamp 的原生任务不再获准新写，已创建 ID、历史 body/digest、manifest、预算和结果查询保持原文。历史冻结但未提交的预览，在当前合法操作者明确提交时才产生新批准。已提交预览永远重放原 Submission，不自动复活。需要继续新投放时按既有唯一性合同新建工作区/草稿并重新准备、预览、明确提交；同草稿已覆盖范围仍受 `COVERED` 约束，不能仅换 request key 绕过。已有对象须由操作者考虑其消耗与占用；没有第二 ENABLE 或自动重建。
- 本次只围住原生发送；旧 Mini 执行合同未改。UNKNOWN 保持原只读恢复流程，不能用 stamp 更新或自动重发解锁。JB-17 须消费原 Submission stamp，区分只读证据恢复与新写授权，不能把当前代数回填成旧批准。

### 本地运行证据

命令均从本树 backend，以 `python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py` 前缀运行。Python 3.14、专用 M2 PG/Redis；没有读取/输出私有 env，没有清库或更换共享软链。`python -m alembic current` 实际读回 `actor_authorization_stamp (head)`；迁移 fixture 亦从空 schema 完整升级通过，未改历史迁移。前端/API schema 未变，复用上一轮前端构建。

- RED：`python -m pytest tests/modules/builds/test_native_batch_execution.py -k 'fences and user_regrant and queue' -q --tb=short`，`jb16-stamp-red.log`：旧实现在 User 管理 owner 停用再启用后多发送一次 SDK CTA 请求，1 failed。
- GREEN：`-k 'fences and regrant'`，`jb16-stamp-green.log`：4 passed / 31.13s，User 与成员管理 owner 的 revoke/regrant 在 queue/send 两个时点均零新增写、成功兄弟保持原 ID。
- scope：`-k 'fences and (recreate or tenant_regrant or superuser_regrant or unrelated or fresh_approval)'`，`jb16-stamp-scopes.log`：10 passed / 72.49s。覆盖成员删除重建、Tenant 管理 owner 停用恢复、User superuser 恢复、无关用户及其他租户成员、新明确批准后的实际 SDK 发送。
- 历史 NULL：真实历史形状 fixture 经过 expand/queue/get_submission，拒绝排队、回执仍可读、digest/config 不变，DB 拒绝回填 stamp。`jb16-stamp-regression.log` 中该例通过；另一 send-lock 检查因新增第二个锁结果后断言仍预期单元素失败，实测两次撤权都被锁阻止；断言已按两种独立撤权的预期修正。
- 近似错误：临时将 canonical stamp 比较条件改为永不拒绝；`-k 'fences and user_regrant'` 同时选中 user/superuser 的 queue/send，`jb16-stamp-probe.log` **4 failed / 34.55s**，均多发实际 SDK 请求。仓外备份 finally 恢复并字节比较，另 `cmp` 通过。探针与业务复验串行，无污染。
- 最终命令（一次拼错测试节点仅 collection exit 4，修正后执行）：

```sh
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest tests/modules/builds/test_native_batch_execution.py::test_batch_fences tests/modules/builds/test_native_batch_execution.py::test_legacy_approval_readable_but_cannot_send 'tests/modules/builds/test_native_batch_execution.py::test_batch_submit_and_execute[send-lock-source_env0]' 'tests/modules/builds/test_native_execution.py::test_native_expired_evidence_continues_frozen_plan[scene]' tests/modules/tenants/test_permissions.py tests/modules/tenants/test_management.py -q --tb=short
```

结果追加下方。该命令定向复验最终接线、当前权限 owner、5.2 秒 SDK 请求期间 User 代数 UPDATE 被锁、M1 纯 TTL 的原 stamp/revision/digest/父级 body/attempt 保持。其余未改场景复用上一轮证据；合成数据、SDK transport double、有界 worker 宿主检查替身的限制不变，不代表生产或真实 TikTok 验证。

### 下一位形式 worker

不得把旧草模当运行 AC。对应位置：`actor_authorization_stamp.upgrade`（原事实行代数递增和 stamp write-once）、`tenants.permissions.build_authorization_stamp/require_build_authorization_stamp`（相同权限事实读取与比较）、`submissions.submit_preview`（明确批准且同事务唯一 INSERT）、`dispatch.queue_step`（入队拒绝）、`execution._frozen` 与既有 `send_scope/lock_send_authority`（物理发送期间持锁比较）。模型须包含撤权恢复、成员删除重建、历史 NULL、旧消息不换 stamp、当前批准的新 Submission 可发送、无关 scope 不失效；抽象 sequence 只要求新代数不复用。数据库发送锁并非远端写与本地回执的原子事务，UNKNOWN 不能重发。形式映射、根审查与最终验收仍待完成；**JB-16 不标 Done，JB-17 未放行**。

最终结果：`jb16-stamp-final.log` **59 passed / 2 failed / 191.64s**。24 个 queue/send 围栏、历史读取、SDK 双锁均通过；两个失败分别为 TTL 测试函数后部局部 Submission import 遮蔽新增断言，以及 Tenant response_model 直接序列化模型泄露新内部字段。后者通过在 generation 字段设置 `exclude=True` 保持原 API 响应，未放宽 API 断言；前者把 Submission import 统一到模块顶部，清除函数内遮蔽（首次只移除条件分支 import 仍失败，随后移除函数后部同名 import）。

修复后 `<runner> python -m pytest 'tests/modules/builds/test_native_execution.py::test_native_expired_evidence_continues_frozen_plan[scene]' tests/modules/tenants/test_permissions.py tests/modules/tenants/test_management.py -q --tb=short`：`jb16-stamp-final-fix.log` 权限/管理 **34 passed**，当时 TTL 尚有上述 import 遮蔽失败；清除后单独同节点 `jb16-stamp-ttl-final.log` **1 passed / 12.56s**，断言原 stamp、revision、digest、父级 body/attempt 不变。最初误从仓库根启动该复验仅 collection exit 4，已从 backend 修正执行。最终 10 个变更 Python 文件 Ruff check / format --check 均通过，git diff --check 通过。验证按受影响范围复用，没有将初次失败记录改写成全套绿。API 表面保持原形，未新增前端构建。新增迁移尚未生产部署；根审查和形式验收仍未完成。
