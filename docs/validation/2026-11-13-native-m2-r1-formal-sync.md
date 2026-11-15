# Native Series M2 R1 独立形式同步（2026-11-13）

状态：**R1 live-sibling 聚合模型已独立同步，正常有界模型通过，预期错误/可达性反例命中；产品未发现新反例，M2 未自行放行，待 root Spec R2。**

- 起点及本树 tip：`0d9c8846fe1d2ba7318d28c159e891a275018c62`，branch `feat/jb13-native-series-m2`。
- 前模型 sync：`5d6706d1594fb5395e3e2b2e7c9ca3661f531ed9`；本轮 sync：`0d9c8846fe1d2ba7318d28c159e891a275018c62`。
- 工作树：`/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb13-native-series-m2`；保留既有 `.venv`、`node_modules`，没有另建或切换树。
- 产品源码只读。无 push/PR/deploy/Linear/合 main、平台或生产请求；无迁移、依赖重装、FLUSHDB。

## 1. 源码与终态合同核对

两个 sync 之间唯一产品改动为 `backend/app/modules/builds/native_completion.py` 新增 live-sibling 分支：在 UNKNOWN/MISMATCH 和取消判断之后、失败终态之前，将失败后代闭包外的非 READBACK PENDING/QUEUED/RUNNING/RETRYABLE 保持为 RUNNING。其余产品 owner 未改，不重新大审。

Spec、完整 R1 修复记录与真实日志的合同是：每计划缺一 Ad MATCH 则计划 PARTIAL、批次 FAILED；批次 PARTIAL 至少需要完整 MATCH 的计划，或已有单计划 PARTIAL 兼容分支。不能采用旧摘要“两条新 Ad 创建后最终 PARTIAL”来改变该语义。本轮正常模型没有产生产品反例。

## 2. 为什么旧 PASS 不足，最小模型是什么

修改模型前按原入口运行原相关默认、recovery、historical、historical-witness、cancel 分支，其既定正常/反例检查均通过。旧 `Submission.tla` 只有完整计划、UNKNOWN、失败集合等粗粒度事实，没有失败父级未发送后代的传递闭包，也没有“失败 Ad＋仍可执行兄弟 Ad”的中间态，所以 PASS 不覆盖 R1。

新增独立 `.agents/skills/formal-verify-native-series-batch-submission/Completion.tla`，不扩大旧模型的全调度维度：

- `N=1,K=4`：一个计划 Campaign→AdGroup→两个 Ad；`N=2,K=3`：两计划各一个 Ad。另有一个代表性非 READBACK 准备步骤；既有其他模型 bounds 不削减。
- 创建、对象 readback、Ad creative MATCH 分别表示；真实完整 MATCH 才允许 COMPLETED。
- `FailedClosure` 由失败/取消父级传递关闭未发送 pending 后代；`RecordBlocked` 表示调度器稍后持久化的 dependency failure，但聚合无需等待该写入。
- `pending` 是 live guard 的 PENDING/QUEUED/RUNNING/RETRYABLE 抽象，避免四等价状态笛卡尔放大；真实 HTTP 四态回归区分这些状态。READBACK 单独建模，不能充当 live sibling。
- 聚合优先级依次保留完整 MATCH、UNKNOWN/MISMATCH、完全取消、live sibling、失败派生。独立 `TerminalContract` 检查 UNKNOWN/MISMATCH、取消、RUNNING、完整 MATCH、单计划 PARTIAL 兼容和多计划 FAILED；不是把被测 `PlanResult` 复制成自己的预期。

## 3. TLC 实测

从仓库根运行（原 `models.py` 默认分支也保留，不因新增 focused 分支偷减既有 bounds）：

```sh
FORMAL_ONLY=completion \
TLA_JAVA=/tmp/tkada-native-series-20261112/m2-implementation/formal-tools/jdk-21.0.13+9-jre/Contents/Home/bin/java \
TLA_JAR=/tmp/tkada-native-series-20261112/m2-implementation/formal-tools/tla2tools.jar \
FORMAL_OUT=/tmp/tkada-native-series-20261112/m2-implementation/m2-r1-formal-sync/completion-final-clean \
bash .agents/skills/formal-verify-native-series-batch-submission/run.sh
```

| 用例 | 实际结果 | 含义 |
|---|---|---|
| normal N=1,K=4 | 6941 generated / 2082 distinct；PASS | 检查 Completion、LiveSiblingSafety |
| normal N=2,K=3 | 327889 / 69696；PASS | 两计划优先级与终态 |
| live-sibling N=1,K=4 | 53 / 45；`Invariant LiveSiblingSafety is violated` | 一 Ad FAILED、兄弟 Ad pending，错误实现提前 PARTIAL |
| live-sibling N=2,K=3 | 23 / 23；`Invariant Completion is violated` | 同一错误在两计划终态合同中命中 |
| normal blocked witness N=2,K=3 | 2362 / 1066；`Invariant NoBlockedDescendantTerminal is violated` | 预期可达性 witness，不是产品安全失败 |

blocked witness 中两计划准备及 Campaign 成功、AdGroup 失败、未记录 Ad 后代 pending。闭包排除这些不可执行后代，计划 PARTIAL、批次 FAILED，不无限 RUNNING。`models.py` 的 `expect_error=True` 明确要求此 invariant violation；其他正常分支仍要求 TLC exit 0。

完整日志：`/tmp/tkada-native-series-20261112/m2-implementation/m2-r1-formal-sync/completion-final-clean.log` 及同目录 `completion-final-clean/`。

## 4. 真实源码探针与受影响回归

复跑正式 `source_checks.py` 新增的 `live-sibling-completion`，而不是另造产品状态。它只将 `s.id not in failed` 替换为 `False`，目标为真实 `native-ad-PENDING` 场景：

- broken：RED，实际 `assert 'FAILED' == 'RUNNING'`；
- finally 恢复、cmp 字节一致后 restored：GREEN，`1 passed, 47 deselected in 14.69s`；
- 产品文件前后 SHA-256 一致，最终 `git diff` 无产品改动。

受影响 completion/receipt 检查经指定 runner、backend cwd 执行：

```sh
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest \
  tests/modules/builds/test_native_batch_execution.py -k \
  'native-ad or partial or zero or unknown or recovery or cancel'
```

结果：`16 passed, 32 deselected, 2 warnings in 231.56s`，日志 `/tmp/tkada-native-series-20261112/m2-implementation/m2-r1-formal-sync/affected-receipts.log`。

复用 R1 修复已有精确真实日志：四状态 `/tmp/native-r1-four-states-final.log` 为 `4 passed, 44 deselected in 78.59s`；blocked 对照 `/tmp/native-r1-blocked-control6.log` 为 `4 passed, 44 deselected in 56.09s`；先前受影响代表 `/tmp/native-r1-affected-final.log` 为 `13 passed, 35 deselected, 2 warnings in 204.98s`。其他未变权限、金额、冻结等 12 个探针复用，不重跑全 13 个或全仓套件。

## 5. 边界与交接

本轮仅证明上述小边界内的聚合安全性，不是 full refinement/composition、liveness、调度推进或任意规模证明。抽象的准备步骤、MATCH 事实及 pending 合并由对应真实测试接线；不证明真实锁/进程调度、creative 多重集合比较器、平台或生产行为。模型正常通过不等于可以合并/发布；后续 Spec R2 与 root 验收仍独立进行。
