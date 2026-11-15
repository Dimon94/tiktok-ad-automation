# JB18 候选：原生批量取消与 canonical consumer 收尾

状态：**实现候选，未放行 M2，不标 Done**。起点 `f80201b0654a3c9b39144fd07e3b9d0b05d8f639`；唯一树 `/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb13-native-series-m2`，分支 `feat/jb13-native-series-m2`。接续原有 dirty 修改，没有 reset/stash/切树/派下级代理；原未跟踪 `.venv`、`node_modules` 保留。未调用真实 TikTok，未 push/PR/deploy/Linear，未新增迁移。

## 实现范围

- `submissions.cancel_submission` 沿用原 Submission，关闭 `local_cancelled` 发送入口；以 `skip_locked` 仅取消 PENDING/QUEUED、无 request body/remote ID、非 READBACK 的空闲步骤，追加 LOCAL_CANCELLED evidence。取消持 Submission 锁，不反向等待发送器的 step 锁；已准入请求仍由原执行器与回执 owner 完成。
- queue、process 和最终 `require_local_send_open` 保留三道发送关闭检查；不新增执行器或影子取消状态。取消重放返回同一 Submission 当前真实聚合。
- 全未发送→整批/计划 CANCELLED，EXCLUDED 继续可见；MATCH 加其他取消/失败→PARTIAL；多计划无 MATCH 且全部明确失败/取消→FAILED；任何 UNKNOWN/MISMATCH→NEEDS_REVIEW。已发送及 UNKNOWN 的 body、digest、attempt、remote ID 和迟到事实保持不变。
- 新旧原生 preview 都经 `native_preview.read_batch` 读取。新单计划和多项共享 plan-set manifest/当前 Unit；历史平铺 native_request 在读取边界包装为一项 INCLUDED，校验原 digest/唯一 Unit/请求，不回填。新写仍只有一份冻结请求。
- 删除 `single_native_selection` 和“尚未开放”死码。`native_execution.validate` 按当前 Unit 对应计划复核 Draft revision、route、selection、冻结 native_request 与授权；submit 对全部 INCLUDED 使用同一资格 owner，不再只验第一项。旧授权 stamp、TTL、恢复 owner 不变。
- 前端已有真实取消入口，未改生产前端；补真实浏览器测试宿主。仅测试 fixture 修复旧迁移播种对当前 ORM 新列的依赖，历史 migration 字节未改。

## 真实运行证据

在唯一树执行：

```sh
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest \
  backend/tests/modules/builds/test_native_batch_execution.py \
  backend/tests/modules/builds/test_native_execution.py \
  backend/tests/modules/builds/test_native_batch_preview.py \
  backend/tests/modules/builds/test_native_plan_set.py \
  backend/tests/modules/builds/test_native_series_migration.py -q --tb=short
```

结果：**111 passed，2 warnings，998.65s**。日志 `/tmp/tkada-native-series-20261112/m2-implementation/jb18-final-verified.log`。使用隔离 PG/Redis、真实 API/Submission/Unit/Step/queue/worker；唯一外部业务替身是 SDK transport。认证、账户和 bounded-worker 元数据为隔离 fixture，不冒充完整 Celery prefork 或真实平台。

取消新增四条纵切：浏览器取消未发送后 queue/worker 零 SDK 写且 EXCLUDED 可见；一计划完整 MATCH、另一计划取消→PARTIAL；两计划各保留 CTA/Campaign、其余取消→整体 FAILED；一计划部分创建、另一计划 Campaign Timeout UNKNOWN 后取消→NEEDS_REVIEW。已发步骤快照在后续 worker/回读后逐项不变。原提交并发重放保持同 Submission。

M1 `cancel_inflight` 在真实 Campaign SDK 请求中由**独立 PG Session/Thread** 调用取消；随后每步结束显式 join(timeout=10)、断言线程退出且无异常。保留 CTA/Campaign remote ID，后续未发送对象不再创建。`unknown_late` 同样独立会话取消并过期 Campaign lease，迟到回执仍保存 RESULT_UNKNOWN/LATE_CREATED；`revision_after_cta` 的独立 revision 修改不再使 CTA 回执丢失。同步在 SDK transport 内调用取消会等待本发送者 Submission 锁，不是合格并发竞态夹具。

旧迁移字节用例最终 `1 passed/2.35s`，日志 `/tmp/tkada-native-series-20261112/m2-implementation/jb18-migration6.log`。

## 过程失败与探针边界

- 接手初始 batch 基线 40 passed；统一 consumer 后出现过 canonical fixture 的 `KeyError: selection/state`、旧 `expand_plan(batch=True)`、等待/READBACK 掩盖 FAILED、素材准备 source_mode 和旧迁移播种失败。分别修正读取/夹具及统一派生，未改旧 migration 或伪造 SDK 事实。
- 中间完整套件曾 89 passed/22 failed、106 passed/5 failed、110 passed/1 failed；最终上面 111 项全部通过。`cancel_inflight` 曾因测试误把取消分支并入 unknown_late 而多触发取消，已分离两个分支；最终两项并发定向 `2 passed/37.10s`。
- 仓外错误探针脚本为 `/tmp/tkada-native-series-20261112/m2-implementation/jb18-cancel-probe.py`。尝试分别破坏 queue、process、取消选择器，以及三入口同时破坏，测试仍绿，**没有得到有效红→恢复绿反证，不宣称取消 owner 已完成源码反证**。各次均仓外备份/finally/字节恢复，后续 `cmp` 通过；最后一次恢复用例 `1 passed/27.81s`，日志 `/tmp/tkada-native-series-20261112/m2-implementation/jb18-cancel-probe-complete.log`。早期脚本锚点/备份名错误和探针未红是过程失败，不计业务行为缺陷。最终全套在上述探针前完成；探针后仅做字节恢复和同一用例复跑。

静态检查：`git diff --check` 与修改后 Ruff 检查通过。产品前端未改，复用 JB16/17 正式/demo build，不重新构建。

## 尚未闭合，交下一 fresh context / root

- 本轮是聚焦取消与 consumer 收尾，不是 M2 完整验收。未运行/扩展最终 R11 模型、全 20T/13R 反证矩阵、最终浏览器全集；取消 owner 的有效源码红→恢复绿反证仍缺。
- 历史 frozen-read 的集中实现保留，但临时 FROZEN 数据库夹具受不可变约束撤回；本轮没有新增独立持久旧 native_request 纵切证明，不能声称所有历史格式都经真实旧库回放。
- T18 仍只允许原明确拒绝的历史 UNKNOWN；Timeout/空 GET 不得清 UNKNOWN，JB17 所记官方负证据边界不变。
- 未做真实平台、生产、完整 worker prefork、root 独立 Standards/Spec review 或 main 集成；后续由 root 安排最终验证和集成，不由此候选自行放行。
