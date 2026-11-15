# Native Series M2 R1 P1 修复验证（2026-11-13）

状态：**R1 Spec P1 已在本树修复并有真实红绿证据；M2 未宣称完成，未进行独立模型同步、root 两轴审查或集成放行。**

- 起点：`4743ccbfe2f7db9c2960f339610f440b52a2b3ec`。
- 工作树：`/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb13-native-series-m2`，分支 `feat/jb13-native-series-m2`。
- 仅修改 `backend/app/modules/builds/native_completion.py`、`backend/tests/modules/builds/test_native_batch_execution.py`、formal skill 的 `source_checks.py`（新增源码错误探针，未改模型），以及本文和 `docs/implementation-progress.md`。
- 保留原 `.venv`、`node_modules` 软链；无 push、PR、deploy、Linear、合 main；无真实 TikTok/生产访问，无迁移、预算/权限/发送合同变化。
- shell 没有 `apply_patch`，工具检索也未发现该能力；明确报告后采用 harness 的精确文本 `edit`，没有用 shell 写文件绕过。

## 根因与产品修复

独立 Spec R1 指出：`native_completion.batch_results` 一旦观察到失败对象，就会用 `PARTIAL/FAILED` 覆盖仍可执行的兄弟步骤。两 INCLUDED 计划各一 Ad 失败、另一 Ad 未处理，Campaign/AdGroup 已成功且 READBACK MATCH 时，批次显示 FAILED，但两个兄弟 Ad 仍可真实发送。

根因位于 `backend/app/modules/builds/native_completion.py` 的原有失败终态分支。本次只在同一 owner 的 UNKNOWN/MISMATCH、整计划取消判断之后、失败终态派生之前增加一个守卫：若任一非 READBACK 步骤不属于 `failed` 的未发送后代闭包，且状态为 PENDING/QUEUED/RUNNING/RETRYABLE，则状态为 RUNNING。

不新增平行状态、影子 owner 或第二套聚合器。失败父级的未发送后代仍由真实调度器置为 `FAILED/dependency_failed`，不属于可执行兄弟，不会使批次无限 RUNNING。真实 UNKNOWN/MISMATCH 仍为 NEEDS_REVIEW；取消、单计划历史、READBACK 等待/阻断、MATCH 和原 ID/发送合同未改变。

## 真实 RED → GREEN

所有 pytest 均从 `backend` 使用：

```bash
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest ...
```

场景在既有 `test_batch_submit_and_execute` 中扩展为同一参数化用例的四个状态，不是另造调度执行器。两 INCLUDED 计划通过真实本地 HTTP 准备、冻结、提交；真实 PG/Redis、展开、queue/claim、执行器和 READBACK 保持，只有 SDK transport 是业务替身。每计划首个 Ad 得到 SDK 明确拒绝（code 40002），同计划另一 Ad 保持未处理。

### RED

未修复源码下，`/tmp/native-r1-strict-red3.log` 记录了实际 R1 反例：

- `AD:FAILED=2`；`AD:PENDING=2`；`READBACK:SUCCEEDED=4`。
- HTTP GET `/tenants/{tenant_id}/submissions/{submission_id}` 实际返回批次 `FAILED`，按 Spec T12/T13/R13 应为 `RUNNING`。
- 原断言位置：`backend/tests/modules/builds/test_native_batch_execution.py` 当时第 780 行。

此前有多次夹具、导入、revision、READBACK 关系和计数错误；它们均未作为有效 RED，也没有通过修改产品掩盖。

### GREEN 与后续执行

`/tmp/native-r1-four-states-final.log`：

```text
4 passed, 44 deselected in 78.59s (0:01:18)
```

四个状态均验证：

- 真实 HTTP 读回为 RUNNING，且两失败计划不会误终止可执行兄弟。
- 中间 READBACK 已完成，因此不是把合法 READBACK 等待误认为本 P1 反例。
- 随后通过原 queue/process 执行两个保留的 Ad；RUNNING 使用原恢复路径，不把拒绝结果伪装成可重试。
- 实际 READBACK MATCH；原 ID、原冻结请求边界保留，重复 worker 没有新增 SDK 写。
- 每计划保留 Campaign/AdGroup/Ad 三个真实远端 ID；两计划均缺一 Ad MATCH，按 R13 整批终态仍为 FAILED、计划 PARTIAL，不将中间 RUNNING 延伸成虚假完成。

`/tmp/native-r1-blocked-control6.log` 对 `partial`/`zero` 的 blocked-descendant 对照通过：先调用真实 `dispatch.process_unit`，失败父级未发送 Ads 置为 `FAILED/dependency_failed`，无 request body/远端 ID；partial 因另一计划的合法兄弟继续 RUNNING，zero 为 FAILED。

```text
4 passed, 44 deselected in 56.09s
```

## 受影响回归与错误实现探针

最终合并受影响回归 `/tmp/native-r1-affected-final.log`（含 native-ad 四状态、partial、zero、unknown、recovery，以及命名匹配的取消代表）：

```text
13 passed, 35 deselected, 2 warnings in 204.98s (0:03:24)
```

先前独立取消/结果/恢复代表日志 `/tmp/native-r1-results-cancel-recovery-final.log` 为：

```text
11 passed, 37 deselected, 2 warnings in 260.49s (0:04:20)
```

该组合工具调用在 270 秒被终止，但日志已完成 11 passed，并确认没有残留 pytest；未因工具超时重复执行。沿用已有未受影响结果，不运行全套约 16 分钟回归。

在 formal skill 的 `source_checks.py` 增加 `live-sibling-completion` 错误实现探针，仅把 guard 的 `s.id not in failed` 改为 `False`，目标为真实 native-ad-PENDING 场景。`/tmp/native-r1-probe.log` 显示：

- broken：RED，实际批次 FAILED、期望 RUNNING；
- finally 按字节恢复并 cmp；
- restored：GREEN。

探针失败必须来自指定行为断言，环境错误不算反证。Python `py_compile` 与 `git diff --check` 通过。

## 下一 fresh Kimi 的模型同步点（本轮模型只读）

模型文件 `.agents/skills/formal-verify-native-series-batch-submission/Submission.tla` 本轮未修改。现有 `Publish` 只有完整计划、UNKNOWN、含失败计划集合，无法表达源码的“失败父级未发送后代”闭包及“闭包外仍可执行兄弟”。

下一 fresh Kimi 应独立同步，不弱化 Spec/模型迎合实现：

1. 为对象增加真实层级的父关系；增加由 FAILED/CANCELLED 父级传播出的未发送 blocked-descendant 集合。对应源码 `dispatch._dependency` 与 `native_completion` 的 `failed` 闭包。
2. 为可执行兄弟区分 PENDING/QUEUED/RUNNING（模型 claimed）及实现支持的 RETRYABLE；READBACK 不属于本守卫。对应 `batch_results` 新增 live-sibling guard。
3. `Publish` 应先保留 UNKNOWN/MISMATCH→NEEDS_REVIEW、整计划取消，再使“闭包外仍有可执行非 READBACK 兄弟”得到 RUNNING，之后才派生 FAILED/PARTIAL。
4. 增加 `Bug = "live-sibling"` 或等价错误变体：忽略闭包外可执行兄弟，构造两计划各失败一 Ad、另一 Ad pending、其他非 READBACK 对象 MATCH 的反例。正常模型应禁止 FAILED/PARTIAL 提前出现，错误变体应命中。
5. 增加 blocked-descendant 反例：所有未发送后代均闭包内，不能被误当 RUNNING； cancelled/no-send 和 READBACK 等待原语义不变。
6. 让源码检查继续引用当前真实 HTTP 行为断言，不把模型结论当作真实执行证明；有界状态不是任意规模、真实锁或 liveness 证明。

本轮没有宣称模型已同步、M2 完成或可以发布。
