# wholeM5a Spec R1 独立审查

结论：**PASS**。P0=0，P1=0，P2=0，P3=0；未发现可按批准合同确证的未解决缺陷。此为 Spec 单轴、round 1/max 3 的结论，不是 wholeM5a 发布、生产或真实平台批准。

执行者：Codex gpt-6.1-sol/high，独立自行审阅，无子代理。产品源码只读；只写本外部报告，未读取或改写 peer report，未写 tracker/parent。

## 精确范围与合同

树：`/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-m5a-native-status-validation`；分支 `feat/m5a-native-status-validation`。

已一次核对 HEAD=`d1bcbe60e46380ad5abf79b1792975120af4b62a`，base 与 merge-base 均为 `9d51cc9b37faf1b6a830fa025cbd377ace716048`。工作树仅有既存未跟踪 `.venv`、`node_modules` 链接，指向原产品树依赖；未替换。九条差异路径及八个提交与 `review-r1-bundle.json` 一致。

审阅使用原命令：

```sh
git diff 9d51cc9b37faf1b6a830fa025cbd377ace716048...d1bcbe60e46380ad5abf79b1792975120af4b62a
```

提交：20709eb5、5f4b0166、2e3a52b6、c6694ca8、f4c8fd50、24548ccf、0ddc58c2、d1bcbe60。覆盖 JB-59 与 JB-60 整片，非仅最后恢复修补。

权威合同：`/tmp/tkada-native-series-20261112/m5a-implementation/approved-scope.md:3-7`、同目录 `JB-59.md:19-30` / `JB-60.md:19-30`，以及上一级 `/tmp/tkada-native-series-20261112/m5a-ticket-map.json` 和 `m5a-ticket-plan.md` 的 C01–C10。后两文件位于上一级，非 implementation 子目录。规划产物中的早期草案授权字段按后续 approved-scope/实际票解释；未批准的旧强制预览 Spec 未作为验收要求。

已完整读取 `/Users/dimon/.agents/skills/code-review/SKILL.md`；按本次明确指令只做 Spec 轴，不派生其他审阅代理。

## Findings

无。没有把未验证、间歇现象、测试覆盖边界或未批准的预览政策当作缺陷。没有候选 P0/P1 需要进行基线降级；本结论不等于证明所有交错或配置均无缺陷。

## C01–C10 源码与证据核对

| 承诺 / 批准依据 | 独立源码核对与证据判断 |
| --- | --- |
| C01 精确三层双向；JB-59:19–20 | 原 preparation → dispatch_work → batch_execution/execute_item → 官方 SDK 路由保留；状态不新增 Native executor/qualifier。新增 `test_native_status_http_worker_readback` 与 workspace 参数化覆盖 Campaign/Adgroup/Ad × ENABLE/DISABLE。 |
| C02 直接受理、冻结；JB-59:19,22 | 产品前端与直接受理/准备 owner 未改。测试从 HTTP 202/PREPARING 开始，后改选择不扩入已受理任务；原值由异步准备冻结。`execution.py:140–154` 只修正状态原值的文本投影，不改持久原值或预算数值转换。 |
| C03 授权和原 SDK owner；JB-59:27，JB-60:27 | 追踪 `_scoped_task`/`readable_history`、恢复提交 `_check_preview`、preparation、单项与批量 before-request 的 verify_route/current-original 门禁。历史读取按当前授权核验原冻结路由；当前合法操作者的新恢复任务与原任务 actor/回执没有混写。UNKNOWN 核查还在读取前及发布观察前复核当前权限。未发现回退连接或跨域新调用。 |
| C04 无隐式级联；JB-59:21 | 原 expansion 的父级提示与物理写成员不同；发送只消费明确 task items。测试检查未选父对象不写、父禁用提示为 NO_CHANGE。恢复复制原 ref/parent_ref/capability，不自行加入父子写入。 |
| C05 受理与观察；JB-59:24–25，JB-60:24 | `execution.py:645–655` 将启停受理后观察保留 REFRESH_PENDING；`:718–734` 检查同对象、发送尝试之后、同连接/通道、目标状态；`:737–815` 只读刷新与任务汇总分离。批量成功路径先登记目录，既有 claim 唤醒可触发后续刷新；刷新仍未观察时按时间桶再唤醒，不能把排队直接当成观察。原始 attempt、receipt、original/final 不被目录事实替换。 |
| C06 幂等和并发；JB-59:26 | 原提交幂等、SQL claim/pre-attempt、Redis lease、发送前围栏与终态不重放路径保留。新增测试同 key、并发 Worker、再次投递后检查一次物理 POST/attempt/receipt；没有新增重复发送入口。 |
| C07 混合 siblings；JB-60:19 | 原批量预检逐成员隔离，dispatch_work 调度有效成员继续。`test_native_mixed_results_continue` 在同任务保留 rejected/unknown/accepted/nochange 的原值、终值、原因、独立观察；没有用整任务结果覆盖项结果的新变更。 |
| C08 UNKNOWN；JB-60:20–24 | 原 reconciliation 的当前 round/attempt、去重 receipt、三次上限、读证据时间界限及权限复核保留；观察匹配不会修改 UNKNOWN 或虚构原请求 ID。新增 rounds 测试包含 match/different/missing/stale、耗尽、新人工轮次与在途旧返回；retry/refresh 路径不能把 UNKNOWN 加回可写队列。 |
| C09 显式反向意图；JB-60:25–27 | `actions.py:646–710` 使用冻结 original 作为反向目标：当前等于 original 为 NO_CHANGE，当前等于旧 final 才待执行，其余缺失/空值/不同为 CONFLICT。原提交与发送 owner 再校验权限和冻结当前值。20 组合覆盖两方向 × ACCEPTED/UNKNOWN × 五类当前值；queued restore 撤权测试先形成真正可写 PENDING 再撤权。预算/ROAS 分支保持原比较，rename 恢复仍明确不支持。 |
| C10 纵链与平台边界；JB-59:23,30，JB-60:28,30 | 阅读新增 Python/Playwright harness：真实页面/HTTP/PG/Redis、原 Worker 函数与目录持久 payload；SDK urllib3 wire 替身、合成认证及特定时钟夹具。不是纯函数替代纵链，但不能称为独立 broker/常驻 Worker 或真实 provider 验收。 |

通用直接 status 的异步准备及既有显式 restore preview 是两种不同入口，批准合同允许并要求保留；没有据旧草案要求所有 status 先预览。产品差异只有 execution/actions 两个 owner；预算、ROAS、改名没有新增用户行为要求或跨范围产品改动。

## 自有验证与继承证据

本轮自有（无服务、无 fixture、无产品 import）：

- 原 `.venv/bin/python -m ruff check --no-cache`：actions.py、execution.py、test_native_status.py、test_native_recovery.py，全部通过。
- `git diff --check 9d51cc9b37faf1b6a830fa025cbd377ace716048...d1bcbe60e46380ad5abf79b1792975120af4b62a`：通过。
- 原 `.venv` Python 3.14.2 `ast.parse` 同四文件：通过。首次误用系统 python3=3.11，因项目使用 3.14 的 except 语法而失败；改用原依赖运行时后通过，未改源码或依赖，不作为产品失败。
- 定向 Nowledge 搜索 `M5a JB-59 JB-60 native status approved` 无命中；本结论以本次合同和固定源码为据。

继承而未重跑：两份 `docs/validation/2026-11-15-jb59-native-status-readback.md` / `jb60-native-status-recovery.md` 记录的 Root JB59 12 HTTP、Root JB60 33 mixed/restore/revocation、六/五 UI、其余 UNKNOWN/晚到与针对性源码拒错证据。已核对相应测试源码与产品 owner，未将这些执行记作本审阅者运行，也未累加重叠集合。

## 限制与 Unknown

遵守本轮只读/并发审阅边界：没有运行数据库/Redis fixture、迁移、服务、浏览器、平台调用、源码变异或共享状态测试；没有安装依赖、fetch/rebase、stage/commit、push/PR/main 合并、deploy、tracker 写入。故本轮对高纵缝的判断为源码审阅加匹配的继承证据，并非独立动态复现。

START 的 direct10748 SQL count26>25 由既有材料记录 baseline/candidate 复现；本轮未重现，不记绿跑或本次新缺陷。早期两次目录时序失败原因仍 Unknown，最终通过不证明根因修复或稳定性。`formal-verify-management-status` 是实际 owner 的20输入有限枚举及绑定回归，不是数学、TLC 或无界并发/liveness 证明。

真实 TikTok/生产/平台资格、独立 broker 进程、部署后行为均未验证。当前未发现需要以两种合同读法裁决实现对错的具体 finding；上述运行证据缺口保留，不扩写未批准需求。

最终计数：**Spec PASS — P0 0 / P1 0 / P2 0 / P3 0**。仅返回该轴结论供 Root 消费，不代替 Standards 轴或 wholeM5a 集成/发布裁决。
