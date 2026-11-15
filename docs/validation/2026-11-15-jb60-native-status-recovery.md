# JB-60 / M5A-02 本地候选与整片审阅包

依据：用户批准的 `approved-scope.md`、实际 JB-59/JB-60 和 `jb59-root-acceptance.md`（本机 `/tmp/tkada-native-series-20261112/m5a-implementation/`）；C01–C10 原文来自 `m5a-ticket-map.json` / `m5a-ticket-plan.md`。旧 NOT_READY Kimi 强制预览草案不作为合同。JB59 已由 root 在 START `2e3a52b6fb278fb924b3f21cbc0fe31e01243662` 接受；本票保留 JB59→JB60 依赖和 InProgress，由 root 作最终阶段接受。

执行者：用户明确指定的 fresh Codex gpt-6.1-sol/high，自行实施，无 children。仅指定串行树 `projects/tiktok-ad-automation.wt/feat-m5a-native-status-validation`、分支 `feat/m5a-native-status-validation`。不新建/切换/重置树，不吸收 origin 或其它小时报表分支。依赖链接保留。

## 实际必要修复

`actions.py:prepare_restore` 的真实 HTTP/PG/Worker 验收复现：原 ENABLE → DISABLE 的任务，当前 DISABLE 时旧实现错误地产生 NO_CHANGE；当前已恢复 ENABLE 时旧实现错误地产生 CONFLICT。原逻辑把 DISABLE 写死，未按恢复方向检查。

仅修复这个原 owner：当前等于源任务冻结 original_value 时 NO_CHANGE；当前等于源任务 final_value 时保留 PENDING，经既有提交和发送前门禁恢复；缺失、空值或其它已变化状态为 CONFLICT。反向目标仍只来自冻结原值，未修改原任务。没有新 executor、qualifier、flag framework、迁移或直接受理政策。普通数值恢复路径保持原逻辑。没有产品预算/ROAS/改名变更、父子级联或新增确认。

`test_native_status.py` 仅提取已有真实服务器/原查询缓存 Worker 生命周期给本票复用。没有 business service/repository/browser response mock。SDK urllib3 wire 使用合成响应，认证和用于 stale 场景的读取时钟为合成夹具。

## JB-60 AC 逐项证据

| AC | 本票证据及事实边界 |
|---|---|
| 1 混合接受、拒绝、无需变更、发送后 UNKNOWN | `test_native_mixed_results_continue` 同任务：campaign 明确 40002 / reject-id；adgroup SDK 发送后 TimeoutError / 无 request ID；ad accepted；另一 campaign 已在目标 / 无 attempt。逐项原值、终值、原因、执行、观察断言；后续有效 sibling 实际发送。共享父锁竞争可由原 Worker 后续消息继续，不新建执行器。 |
| 2 UNKNOWN 禁止盲重放 | 同测试、`test_native_unknown_rounds`：真正进入官方 SDK PoolManager.request，记录 observer 后丢失响应；原 Worker 再投递、HTTP retry/refresh、自动/人工核查后写调用数不增。不是预植 UNKNOWN 数据。 |
| 3 四类只读观察 | `test_native_unknown_rounds` 覆盖 match/different/missing/stale 与三次耗尽；官方 Smart+ get 精确对象；四种浏览器场景经真实任务页面展示现有观察文案。主观察页与补素材页是不同读调用，三次上限按原核查 attempt 计算。 |
| 4 观察不伪造受理 | UNKNOWN、原值/目标及原 attempt.request_id=None 保持；当前 match 是观察事实。人工轮次可不同，原请求不改为 ACCEPTED。 |
| 5 有界、重复、晚到、新轮次 | 原持久 outbox payload 交原 reconcile task；缺失/过期三次停，重复 payload 不再读取；在 SDK 读取在途时通过真实 HTTP 创建新人工 round，旧返回不能覆盖新 canonical round；再投旧自动 payload 无副作用。SQL/Redis 是真实服务；不宣称全部无界交错已证明。 |
| 6 accepted refresh 独立 owner | 混合任务 accepted 为 REFRESH_PENDING，实际 directory staging/publication 后才观察 ACCEPTED；UNKNOWN 原观察独立。JB59 失败刷新重试只读测试源未变，复用其 root 已接受证据。 |
| 7 现有恢复预览与提交 | `test_native_restore_current_gate` 双向 + ACCEPTED/UNKNOWN 源任务，先真实 SDK 发送/目录发布，再 HTTP restore preview / submit / 原 Worker；成功恢复精确冻结原值，实际回读后观察 ACCEPTED。浏览器从工作区点击、任务页核查、恢复预览确认提交走成功反向恢复。 |
| 8 冲突与 UNKNOWN 恢复 | 20 组合：2 原值 × 2 源结果 × 5 当前值（原写目标、已恢复原值、缺失、空值、DELETE）。后3项 CONFLICT、已符合 NO_CHANGE，提交仍不产生 SDK 写。UNKNOWN 的当前目录来自独立原目录同步，核查不是目录发布，也不是平台受理证据。干扰只修改当前目录，绝不预植 accepted/reconciled/restore 终态。 |
| 9 授权域与旧代次 | `test_native_recovery_revocation` read/restore × grant、connection、actor、authorization revision、account、BC binding，共12场景，已进入 UNKNOWN 后撤销，旧消息无任何额外 SDK 调用。跨 tenant/BC 同平台 ID、原发送门禁与错误 actor 复用 JB59 及既有回归；未替换/回退连接。 |
| 10 最高纵缝 | 真实浏览器工作区→HTTP→真实 PG17/Redis→原管理 Worker→官方 SDK transport double；原核查任务、独立目录请求→staging/publication、恢复预览与提交、原 Worker/任务结果。调度工装消费原 durable payload/body，非独立业务实现；没有真实 broker 常驻 Celery 进程或真实平台。 |
| 11 五类错误源码 | 外部备份、finally 逐字恢复/cmp、实际业务 RED、各自 targeted GREEN；详见下表。空返回探针另列。 |
| 12 范围与平台 | 完全合成、loopback；无真实 TikTok/R2/production/auth/ad write，无 Linear 写入、push/PR/deploy/main 合入。真实 provider/生产恢复未验。 |

## 整片 C01–C10 审阅矩阵

| 承诺 | owner / 本票与继承证据 |
|---|---|
| C01 三层精确 ENABLE/DISABLE | JB59：三层双向 6 UI + 6 HTTP；本票复跑一个既有代表 UI，产品路由未改。 |
| C02 直接受理与冻结意图 | JB59：点击后直接 202、异步准备冻结；本票 UNKNOWN 工作区同入口，没有新增预览；反向恢复保持已有显式预览。 |
| C03 通用管理/SDK/授权边界 | JB59 原发送域隔离/撤权；JB60 增加核查、恢复12撤权场景，复用原 owner。 |
| C04 无隐式父子级联 | JB59 精确调用/父级提示与源探针；本票只反向原对象，无 parent auto enable。 |
| C05 受理和当前观察分离 | JB59 accepted 后实际目录发布与失败刷新；JB60 混合 accepted/UNKNOWN 不混用 refresh/reconciliation 事实。 |
| C06 重投不重复物理写 | JB59 同 key + 两线程真实 PGRedis；JB60 UNKNOWN 原消息、retry、refresh、重复/旧轮次只读，不重放。 |
| C07 混合结果 | JB60 同任务四结果逐项真实、有效 sibling 继续，失败阻断探针拒错。 |
| C08 UNKNOWN 只读与不盲重放 | JB60 官方 send 后未知、四观察、耗尽、新旧轮次、无虚构受理/request ID；源码反例。 |
| C09 冻结原值恢复与冲突 | JB60 实际必要修复、20有界组合、真实成功反向恢复/冲突零写；独立有限验证技能。 |
| C10 最高纵缝无真实平台 | JB59 六UI继承；JB60 四观察UI及实际恢复；全部 SDK 传输双身，真实 PGRedis 与原 owner。 |

## 自有检查、拒错与限制

提交：产品/验收源码 `c6694ca837650c77efc0f21db2d8b51dddd10ccc`；独立有界验证说明 `f4c8fd50dd58c2e67f49cb707f53870e12723df5`；撤权夹具增强 `24548ccf75a5243710f9ac9eb927b16527eb15c4`。本记录/进度提交在这些提交之后。

| 本轮自有检查 | 实际结果 |
|---|---|
| native recovery + existing API actions/unknown reconciliation + 一个原UI | `jb60-validation.log`：64 passed /50.22s，含20个恢复状态组合；这是最终四UI展开前的检查集，不把次数相加当唯一用例数。 |
| 最终四观察UI成功恢复 + 原DISABLE/ad UI | `jb60-workspace-final.log`：5 passed /44.69s；无 browser response mock，当前目录与任务结果分别读回，源任务逐项保持不变。 |
| 加严 queued restore 撤权矩阵 | `jb60-restore-grants-green.log`：12 passed /7.11s；恢复先实际目录发布并确认 PENDING、提交202，再撤销边界，原 Worker 无额外 SDK 调用。 |
| 空执行 / sibling阻断 | 分别1条业务断言 RED；恢复/cmp 后各1 passed。 |
| observed match 假受理 | 4条业务断言 RED（含 ACCEPTED != UNKNOWN）；恢复/cmp 后4 passed。 |
| 恢复忽略 unsafe current conflict | 4条 PENDING != CONFLICT 的业务 RED；恢复/cmp 后4 passed。 |
| 只读核查误调用写端点 | 最终4条物理 POST=2 !=1 的业务 RED；恢复/cmp 后4 passed。 |
| UNKNOWN自动重放 | 最终混合任务物理 POST=4 !=3 的业务 RED；恢复/cmp 后1 passed。 |
| 静态检查 | 3个Python文件 Ruff、compileall、git diff check 通过；未改变前端产品代码，仅新增真实浏览器测试。 |

所有测试仅通过指定 guard 运行。可从集成根执行：

```sh
python3 /tmp/tkada-native-series-20261112/m5a-implementation/run-test.py python -m pytest backend/tests/modules/ad_management/test_native_recovery.py backend/tests/modules/ad_management/test_api_actions.py backend/tests/modules/ad_management/test_unknown_reconciliation.py 'backend/tests/modules/ad_management/test_native_status.py::test_native_status_workspace[DISABLE-ad]' -q
python3 /tmp/tkada-native-series-20261112/m5a-implementation/run-test.py python -m pytest backend/tests/modules/ad_management/test_native_recovery.py -q -k workspace 'backend/tests/modules/ad_management/test_native_status.py::test_native_status_workspace[DISABLE-ad]'
```

实际源码反例脚本 `jb60-probes.py` 在集成根运行，逐项在外部备份，finally 恢复并 `cmp`，每项再跑 GREEN；最后两项在强化物理写次数断言后重新实际执行。原始日志位于本机 `/tmp/tkada-native-series-20261112/m5a-implementation/jb60-*.log` 和 `jb60-probes/`，不把私有环境、数据库或日志原文提交。

早期失败与限制保留：最初夹具缺少显式 fixture import、outbox 用了不存在的 created_at 字段、regular directory probe 被测试 wire 错拒绝、adgroup 合成主键被父级字段覆盖，以及页面点击未等待真实 reconcile HTTP 完成，均为测试工装错误，已改，不作为产品 RED/探针证据。产品 RED 仅上述恢复方向错误，见 `jb60-red2.log` 的 sent-ENABLE / original-ENABLE。

此外，早期混合任务曾在一次目录 pump 后仍 REFRESH_PENDING；加严撤权首跑一次在发送前的目录 setup 仍得到 NO_CHANGE。最终64检查/5UI/12撤权及探针 GREEN 均通过，但这两次异步目录时序的原因尚未定位，未声称已修复其根因或证明稳定无间歇。没有修改产品调度来掩盖这点。撤权首跑对撤权后历史 GET 期待200也不正确：原 owner 返回404隐藏无权资源；最终检查验证原已排队 PENDING 意图的零 SDK 副作用，不绕过历史权限。Root 应结合原始时序记录判断接受与后续稳定性。

继承而未重复：JB59 root HTTP 12 passed/31.35s；对应 source 六 UI /119 regression/六 source probes，见 JB59 validation 和 root acceptance。START 已存在的 `test_direct_10748_submission_does_not_expand_directory` SQL26≤25 失败在 baseline/candidate 均复现，本票不改阈值、不隐藏、不声称全套全绿。未跑巨型 mypy 或完整管理全套；没有 Build TLC 冒充管理证明。

专用 guarded runner、Python3.14、M5a PG17 与 Redis appDB0/testDB1 保持；没有 FLUSHDB、共享安装、停 PG16、停专用集群或清理其它 actor。页面服务器仅 loopback 动态端口，finally 结束本测试线程/服务并恢复认证 overrides。实际生产门禁由原 owner 执行。

Root 后续负责：JB60 阶段接受、fresh Standards/Spec 整个 M5a、local main 集成与闭票。这里仅交付本地 reviewable candidate，不能宣称整个 M5a Done。
