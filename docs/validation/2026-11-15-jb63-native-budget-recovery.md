# JB-63 Native 预算混合结果、UNKNOWN 与恢复

2026-11-15，本地实现候选，交 root 整个 M5b Standards/Spec 两轴审查；不是整个 M5b 完成或生产验收。

## 范围与源码

唯一写树 `projects/tiktok-ad-automation.wt/feat-m5b-native-budget-validation`，分支 `feat/m5b-native-budget-validation`。START `34b1e0cf5b4fa7670995390d99504a0ca1684c48`；本票产品源码提交 `208ef0302872ed9e365656cc961b67337fbf6abe`，继承产品源码 `84db9974b69af5eb97ca82a66f30590803db350b` 与 JB62 root 阶段接受。fresh Codex gpt-6.1-sol/high，唯一 writer，无子代理、checkout 或模型切换。

输入完整读取 `/tmp/tkada-native-series-20261112/m5b-implementation/{JB63-brief.md,approved-scope.md,JB-63.md,jb62-root-acceptance.md}` 及产品 JB62 验证记录。按 tdd、行为测试原则和本地 AGENTS/CONTRIBUTING 执行。

## 实际缺口与修复

- 原恢复预览 DTO 没有原金额或 budget_facts，页面只展示计数。现在恢复预览与原任务详情共用预算展示，读取持久化冻结 owner、原目标→原原值、账户币种、sources 和 affected_refs，不查询今日账户元数据。
- 预览和发送前原来用字符串比金额。复用通用执行 owner 的 Decimal 数值相等函数，`120.1250` 与 `120.125` 等额；状态与名称继续文本比较。ROAS 等其它操作保持原有比较行为，不扩展本票范围。
- 原预算恢复缺少原写之后目录证据门禁。现存请求 attempt 时间随反向意图冻结为 `capability.restore_after`；预览与发送前复用原 `_same_frozen_value` 校验金额、父级、来源通道/连接和发送后观察时间。没有凭有效回执直接认为预算生效，也没有发明目录 TTL 或经营限制。
- 恢复保留 ACCEPTED 和 UNKNOWN 两种已发送来源；反向目标从冻结金额交换得到，是用户确认的新任务。原 UNKNOWN、attempt 和回执不变。恢复本身 UNKNOWN 同样只读核查、拒绝普通重试。
- 另一个实际缺口：管理权限复核集合漏掉 UNKNOWN 来源项，读权限仍有效但管理许可失效时，恢复预览曾返回 PENDING。将 UNKNOWN 纳入既有 `_scoped_task` 的管理权限复核；官方 submit/Worker 门禁保持。单例原源码 RED 与修复 GREEN 单独留证。
- SDK、目录发布、队列、直接预算受理、原官方权限/金额校验及 CBO owner 去重均沿用；无 Native executor、全局预览要求、迁移或依赖更新。

## 七项 AC 与亲跑证据

所有新增检查在 `backend/tests/modules/ad_management/test_native_budget_recovery.py`；金额预期独立来自票据的反向金额合同与 Decimal 常量，不从产品字符串比较推导。

| AC | 动态行为与断言 |
| --- | --- |
| 1 混合结果 | `test_budget_mixed_owners_exact_once`：同任务 CBO ACCEPTED、明确 REJECTED、组预算 UNKNOWN、缺父对象 UNSUPPORTED；成功项独立目录回读，UNKNOWN 定向观察匹配，冻结 owner/金额/USD/来源/影响项保留。CBO 系列及子组重复选择，只有一次系列写；3 个物理预算 POST、各一次 attempt。原 linked_impact NO_CHANGE 行保留，不把它当预算执行项；计数与完整列表解释同批对象。 |
| 2 幂等与 UNKNOWN | 重复直接受理返回同 task，重复 Worker/核查 payload 不增 POST，UNKNOWN 普通 retry 返回409；8个反向组合覆盖 CBO/组预算 × 原 ACCEPTED/UNKNOWN × 恢复 ACCEPTED/UNKNOWN。原项完整公开事实保持相等，恢复 UNKNOWN 仍一次 attempt，不盲重发。继承 JB62 跨准备块 owner 去重证据。 |
| 3 只读核查 | `test_budget_unknown_read_only`：120.12500 等额、120.126 不等、缺实体、旧观察、返回账户失配。匹配/不同只改 observation；其余最多三轮后 RECONCILE_FAILED。原金额、budget_facts、execution_result、attempts 原样；重复末轮无新增读，始终只有原先一次预算 POST。 |
| 4 正常恢复 | `test_budget_reverse_frozen_preview`：冻结120.125→100.25（CBO）或50.50（组），目录以120.1250返回仍可恢复；接受后先 REFRESH_PENDING，新目录100.2500或50.500才 ACCEPTED。preview.source_task_id 形成原任务关系。`test_budget_recovery_workspace` 两种 owner 真工作区→预算直接受理→UNKNOWN→人工只读核查→目录发布→恢复预览→明确提交→原 Worker→新鲜回读。 |
| 5 冲突与授权 | `test_budget_restore_current_gate` 的16个有限组合：预览前/提交后分别改变金额、缺预算、旧目录、目录连接、账户授权、账户管理许可、授权代次、actor权限，全部冲突/拒绝且无新增平台调用。连接使用真实同租户另一连接，保留数据库外键。继承 JB62 同remote ID跨账户币种隔离及原 Native 六类授权边界证据，并亲跑原 Native 恢复回归。 |
| 6 原体验与回归 | 预算入口仍直接202，不增加确认；只有既有恢复预览要确认。详情/核查/恢复入口真浏览器可用，恢复详情共享冻结事实展示。普通 `test_api_actions`、`test_unknown_reconciliation`、`test_execution` 与 Native `test_native_recovery` 定向回归。 |
| 7 动态与未知 | 专用 PG17/Redis、真实 HTTP、实际准备/Worker body、durable dispatch、目录 staging/publication、原 SDK。只有 `urllib3.PoolManager.request` 为 SDK 传输替身；合成账户、授权和 stale 时间明确标注。浏览器业务响应不 mock，测试推进端点仅推进现有 Worker。未证明真实平台/生产/独立 broker 投递成功。 |

## RED、源码探针与恢复

首轮 `/tmp/jb63-red.log`：4 failed，公开恢复预览 `budget_facts=None`，不是环境失败。最小修复后同4例 `/tmp/jb63-green.log`：4 passed/20.55s。

`python3 /tmp/jb63-probes.py` 真改源码七次，每项均在目标断言 RED，随后 finally 复制回仓外备份，`cmp` 字节一致：

1. frozen-display：预算恢复 DTO 去掉冻结事实，反向预览检查拒绝。
2. money-text：金额改回文本比较，等额不同表示的恢复被误判，检查拒绝。
3. stale-evidence：跳过发送后时间边界，旧目录预览检查拒绝。
4. changed-budget：跳过发送前原值比较，提交后改预算的零写断言拒绝。
5. unknown-meaning：颠倒预算观察匹配，UNKNOWN 公开核查状态断言拒绝。
6. workspace-display：移除预算展示，真实工作区金额断言拒绝。
7. unknown-manage：恢复原先遗漏 UNKNOWN 的权限集合，管理许可已失效的恢复预览错误返回 PENDING，公开冲突/拒绝断言 RED（1 failed/2.33s）；finally/cmp恢复。

日志 `/tmp/jb63-probes.log`、`/tmp/jb63-probe-{frozen-display,money-text,stale-evidence,changed-budget,unknown-meaning,workspace-display,unknown-manage}.log`。最终源码恢复后跑 GREEN。这些是有界行为拒错证据，不是无界形式验证；未使用 Build TLC。

## 运行命令与结果

Python前缀（从本产品树运行）：

```bash
python3 /tmp/tkada-native-series-20261112/m5b-implementation/run-test.py python
```

沿用 root guarded runner / Python3.14 / shared Bun 模块和专用 UTF8 PG17、Redis；未重装、打印私有配置、停止服务或触碰旧 M5a/shared 服务。fixture退出只释放自身合成租户目录调度。

本轮实际结果（最后新增权限门禁前后的证据分开）：

- `-m pytest backend/tests/modules/ad_management/test_native_budget_recovery.py backend/tests/modules/ad_management/test_api_actions.py backend/tests/modules/ad_management/test_unknown_reconciliation.py backend/tests/modules/ad_management/test_execution.py backend/tests/modules/ad_management/test_native_recovery.py -q`：122 passed、1 failed/234.65s，唯一失败是原 `test_native_unknown_workspace_restore[stale]` 的恢复后 `ads.targeted_step` FAILED（日志 `/tmp/jb63-final-green.log`）。预算专项30项全部通过。
- 原 stale 浏览器单例隔离重跑：1 passed（`/tmp/jb63-final-targeted.log`，同命令另两条新增管理许可夹具使用非法状态 UNVERIFIED 而失败，不计产品RED；之后改用数据库允许的 UNKNOWN）。原失败发生时另一个共享库 runner 同时运行，存在目录槽干扰可能；运行记录后来 COMPLETE/error_code=None。根因仍 Unknown，一次单例绿跑不证明旧时序问题已修复。
- 最终 `-m pytest backend/tests/modules/ad_management/test_native_budget_recovery.py -q`：**32 passed**/127.03s（含两个真实工作区预算恢复场景、UNKNOWN管理许可门禁、恢复本身UNKNOWN）；日志 `/tmp/jb63-budget-final.log`。这次串行跑取代并发时受干扰的预算失败结果。
- 最终 `-m pytest backend/tests/modules/ad_management/test_api_actions.py -q`：**10 passed**/1.74s，日志 `/tmp/jb63-permission-regression.log`。最新 UNKNOWN 管理权限集合改动后重新验证，其余未变 owner 的122通过证据按影响范围复用，不累加当作独立用例数。
- `node_modules/.bin/tsc -p frontend/tsconfig.build.json --noEmit`：通过。
- `bun run --cwd frontend build`：正式与demo构建通过，日志 `/tmp/jb63-build.log`。
- `-m ruff check backend/app/modules/ad_management/{actions,execution}.py backend/tests/modules/ad_management/test_native_budget_recovery.py`（两产品Python owner及新增预算检查）、`git diff --check`：通过。

早期夹具失败如实保留：随机不存在 connection 导致外键错误，改为创建有效同租户连接；SDK目录替身曾把父ID当子ID返回，被真实 SDK身份校验拒绝；混合列表原有 linked_impact 行应保留，断言改为区分映射行与实际 owner。浏览器人工核查最初未等待HTTP完成，补上 response 等待后两个场景通过（`/tmp/jb63-follow.log`）。这些不计产品 RED，不放宽 SDK 身份或数据库约束。

## 继承、限制与 root 交接

继承 [JB62验证](2026-11-15-jb62-native-budget-readback.md)：root7通过、匹配源码75检查、60个手算金额/owner组合、多账户冻结币种、直接工作区、五源码探针、build/tsc等；未把继承说成亲跑。本轮共享 owner 改动的调用方由上面定向回归覆盖，仍须 root whole-M5b 两轴审查。

旧 direct10748 SQL count26>25 已知排除失败保留，未改阈值或声称修复。有限组合不穷尽平台预算政策；真实TikTok接受、授权、真实广告写、生产恢复与独立broker投递均 Unknown。无 tracker 写、push、PR、main合并、部署或真实平台操作；`.venv` / `node_modules` 原共享链接原样保留。

JB62/JB63 不提前关闭；root负责整个M5b独立 Standards/Spec 审查、本地集成与收尾，无自动后续票。

## 本地提交与状态

产品/测试提交 `208ef0302872ed9e365656cc961b67337fbf6abe`。本文与 implementation-progress 作为后续聚焦文档提交；最终 tip 由交付消息提供，避免文档自引用 commit hash。提交前 `git status --short --branch` 显示仅本票五个产品/测试文件、本文和原 `.venv` / `node_modules` 未跟踪共享链接；明确路径暂存，未吸收链接。工作树与分支保持原样，无远程或main变更。
