# JB-12 原生 UNKNOWN 恢复与本地取消

起点 `c21f05493fbcd6975a1188503c6fa97582b09ce3`，指定集成树 `feat/jb5-native-series-m1`，唯一写者、没有下级代理。本节所在提交为交付 tip。未更新 Linear、推送、创建 PR、部署或调用真实 TikTok/授权/版权方接口。保留原 untracked node_modules 软链；无迁移、依赖修改、数据库降级或部署开关。官方 SDK pin 仍为 `f809c396520df2d7b201a9ccc5378d822b728ed3`。

## 用户入口与状态语义

现有任务详情 `/tenants/$tenantId/build-tasks/$submissionId` 的恢复区域保留“重试失败步骤”和“核查待核实项”。新增“取消未发送步骤”，对应 `POST /tenants/{tenant_id}/submissions/{submission_id}/cancel`。响应丢失后刷新同一结果即可；重复取消幂等。按钮采用现有 shadcn 中性样式。

- 取消入口仅支持原生、当前租户原提交操作者；需要当前租户读取权限，不需要平台写权限。跨租户/其他操作者拒绝。Mini 不开放取消。
- 取消在现有 Submission.progress 保存单向 local_cancelled 和时间，扩展任务游标更新保留该事实。结果 API 返回 local_cancelled/can_cancel。它不是远端取消状态，既有 FAILED/PARTIAL/NEEDS_REVIEW 与实际 ID、证据仍独立展示。
- 队列和旧消息在原执行器把未发送步骤收尾为 FAILED/local_submission_cancelled，并递增 dispatch revision；REQUEST_ARMED、UNKNOWN、已成功步骤不被改写。自动/人工重试不能重新开放取消后的发送入口。
- arm_request 和每次官方 SDK 物理请求前检查共享提交行锁。取消只锁 Submission，不跨网络持锁、不倒序锁步骤；在最终发送准入前观察到取消即禁止发送。已经通过最终准入的请求属于在途，其实际回执照常记录；取消不代表能阻断网络中已有请求。
- 取消后的 UNKNOWN 仍沿原 RECONCILE 查询和保存实际事实，不发送下一层。无 delete/disable、第二次 ENABLE、远端停投或删除证据。UI 明确说明已创建广告保持现状。

## 恢复接线

原 `request_recovery → process_recovery → queue_step → process_reconciliation → SDK read_page` 负责原生恢复，没有新恢复器或 HTTP Gateway。原授权、账户、BC、route/绑定代数、attempt、原 body/digest 校验保持。恢复中撤权后保留 UNKNOWN 和读取证据，不认领或重发。

新原生冻结名称包含草稿 UUID 请求标记（Series/revision 后的 `-R<32hex>`），使当前范围内的请求能被定位；名称只是定位条件。旧冻结名称不改写，缺少此标记且无精确 ID 时保持未决。组与广告查询显式限定精确父级。候选必须完整分页、唯一、全部冻结字段 MATCH，包括账户、父级、Series、双身份、地区/排期、预算/ROAS、视频/封面、文案、CTA 和 ENABLE；同名错父、缺字段、差异、多义、空页不能认领。没有放松精确 ID READBACK；原生事件映射保持 JB-11 范围。CTA 缺已知 ID 时仍不假造查询支持。

认领后保存真实 RECONCILED 回执并复用从 record_created 提取的 ensure_native_readback，继续安排独立精确 ID READBACK；全量完成仍经过 native_completion.all_native_matched。父级认领后仅原计划下一层可续建。现有 NOT_SENT 有界预算/退避、原 body/digest/request identity、confirmed_absent/safely_unsent 及迟到回执 owner 保持；空读从来不直接许可再次创建，明确拒绝不因错误文本重试。取消额外封闭 RETRY 候选。

## 验证方法与结果

全部后端命令从本树 backend 执行，共同前缀：

```sh
python3 /tmp/tkada-native-series-20261112/implementation/run-test.py python -m pytest
```

runner 私下加载专用本机 PostgreSQL/Redis 与锁定 venv；未打印私有配置。新增时间线从已有 prepared_native_workspace → preview freeze → 并发 submit → expand → Execution/Recovery 公开入口出发，平台只替换官方 SDK urllib3 传输。租约到期只推进数据库时钟，不直接写 MATCH 或恢复成功状态。复用 JB-11 独立响应 oracle，仅绑定运行时名称、排期与抽选文案。

- 取消首红：`tests/modules/builds/test_native_execution.py -k cancel_before -xq --tb=short`，缺少 cancel_submission 公共操作而失败；实现后取消三个时间线 **3 passed / 18 deselected / 14.04s**。`jb12-cancel-red.log`、`jb12-cancel-green.log`。
- UNKNOWN 首次检查存在测试签名错误，未算有效红；修正后在起点扫描器回放 `tests/modules/builds/test_native_recovery_scan.py -k unique -q --tb=short`，唯一对象被拒绝，**1 failed**；实现后七项定位/完整匹配负向通过。`jb12-unknown-red.log`。
- 完整 Campaign UNKNOWN 唯一恢复、多义未决、取消后只读恢复：`tests/modules/builds/test_native_execution.py -k 'unknown_unique or unknown_ambiguous or unknown_cancel' -xq --tb=short`，**3 passed / 21 deselected / 14.11s**，`jb12-recovery-timeline.log`。
- 大回归：`tests/modules/builds/test_native_execution.py tests/modules/builds/test_native_recovery_scan.py tests/modules/builds/test_native_readback.py tests/modules/builds/test_execution_state.py tests/modules/builds/test_channel_recovery.py tests/modules/builds/test_readback_empty_retry.py tests/modules/builds/test_recovery_models.py -q --tb=short`，**173 passed / 1 failed / 4 errors / 180.51s**，`jb12-final-regression.log`。失败是新增 Ad 恢复辅助检查误选所有 Ad（MultipleResultsFound），已收紧为 UNKNOWN 那条并补跑通过；4 errors 是既有 recovery_models 夹具在 identity_catalog_unavailable 前置失败，无恢复行为运行。
- 补跑：`tests/modules/builds/test_native_execution.py -k 'unknown_ad or unknown_late or unknown_revoke or cancel_before' -xq --tb=short`，**4 passed / 24 deselected / 36.64s**，`jb12-final-timelines.log`。覆盖 Ad 恢复、取消 HTTP 两个并发请求/跨租户/错误操作者、在途租约到期后的真实迟到 ID、恢复 GET 中撤权及撤权后本地取消。原始 RESULT_UNKNOWN/LATE_CREATED 证据与 body/digest 均保留。
- 初次广回归尝试包含 test_safe_not_sent_recovery/test_recovery/test_recovery_api/test_reconciliation；前13项连续在既有身份目录夹具失败后中止，`jb12-regression.log`。这不是产品恢复失败或全绿证据。交接已有 JB-10 identity_catalog_unavailable 起点动态复现、JB-11 Mini UNKNOWN 起点失败证据继续有效；未改业务规则迎合旧夹具，也不声称全部 Mini 恢复/confirmed-absent 已重新通过。本轮通过的 execution_state/channel_recovery/empty_readback 覆盖既有边界、预算和无重发；confirmed-absent 核心合同没有修改。
- 错误探针实际运行：临时放过非完整差异，wrong_parent/different 两项均失败（2 failed / 0.62s）；临时丢弃取消后的真实创建回执，cancel_inflight 失败（1 failed / 13.41s）。`jb12-scan-probe.log`、`jb12-cancel-probe.log`。两文件从仓库外备份恢复并 cmp 字节一致。恢复后补跑结果见下方。
- 前端本树 frontend：`bun run generate-client`，按项目脚本归一化生成文件末尾空白；`bun run build`，TypeScript、正式与 demo Vite 均通过，`jb12-frontend.log`。没有 preview 工具自验。
- 修改 Python 文件 Ruff check/format、git diff --check、明确路径暂存与暂存 diff 检查。

日志统一位于 `/tmp/tkada-native-series-20261112/implementation/`，不提交私有环境或运行输出。

## 边界与协调者交接

没有运行形式验证器，不声称形式证明或真实平台接受。本轮实际覆盖三级响应丢失、父子续跑、部分创建取消、取消在途响应、迟到 ID、恢复中撤权与 HTTP 重复竞争；没有逐对象类型穷举所有进程硬退出/锁交错组合，不能把这些有限时间线称为完整并发证明。既有 Mini 旧夹具仍限制相关完整回归。没有测试生产、真实授权、远端审核或经营结果。

整个 M1 仍需协调者独立 code review、必要修复、本地 main 集成及 Linear 闭环。本票提交不宣称 M1 完成或关闭工单。

### 探针恢复后的最终结果

- `tests/modules/builds/test_native_execution.py -k 'unknown_unique or unknown_group or cancel_inflight' tests/modules/builds/test_native_recovery_scan.py -q --tb=short`：**3 passed / 32 deselected / 20.27s**，`jb12-probe-restored.log`。`-k` 同时筛掉了扫描器用例，因此另跑扫描器全文件：**7 passed / 0.25s**，`jb12-scan-restored.log`。两条唯一恢复时间线另实际发送原计划下一层，断言父级精确为认领 ID、仅一个 POST，无父级重建。
- 前述大回归中 Ad 辅助检查修复后已在 `jb12-final-timelines.log` 独立通过；没有重跑无变化的173项。最后产品改动仅中文说明与类型注解，Ruff check/format 与 diff 检查通过。
