# JB-11 原生三级及逐创意回读

起点 `9f0537b68d5a21e4f08c37c14e69f356596e6146`；唯一写树为既有 `feat/jb5-native-series-m1`。本节所在提交为交付 tip。没有新建/切换工作树、派下级代理、更新 Linear、推送、PR、部署或真实 TikTok/授权/版权方读写。保留原 untracked node_modules 软链。SDK pin 保持 `f809c396520df2d7b201a9ccc5378d822b728ed3`，无迁移/数据库降级。

## 完成合同与接线

- 原 `record_created` 在同一事务给原生 Campaign/Adgroup/Ad 生成稳定 UUID5 的既有 READBACK 步骤；Mini 的成功回执省略历史自动核查语义不变。原生读取仍经 `process_reconciliation → admitted_build_call → gateway.builds.read_page → ApiBuildOperations`，没有新执行器或 HTTP Gateway。
- 新增 `NativeAdGroupObservedFacts` / `NativeAdObservedFacts`，只解码实际响应。请求仅提供必需观察路径；缺字段、非法类型不会以默认值填成 MATCH。组层身份与创意身份独立，实际视频/封面按多重集合比较；重复视频但不同封面不能互相借值。每个 Ad 只有一条文案，CTA ID、父级和远端 ID 精确相等。原生直接模式没有 Mini 落地链接要求。
- 原生组单独允许创建 `AD_REVENUE_VALUE` 对应观察 `IMPRESSION_LEVEL_AD_REVENUE`；保留观察枚举，不允许 ACTIVE_PAY。ZERO_DAY、VALUE、VO_MIN_ROAS、ROAS、地区、版位、排期分别比较。原有 Mini/标准事件集合没有新增别名。官方合同来源为用户提供的 `/tmp/tkada-research-identity-roi2-20261112/create-an-upgraded-smart-ad-group.md`（响应事件注记）与固定 SDK 快照。
- 原生 READBACK 必须持有精确 ID；名称扫描不能认领。返回额外 ID、重复 ID、分页矛盾、缺对象/字段、失败查询均不能完成。保留实际 candidate.record、missing_fields、comparison、differences、分页总数与匹配数；证据同时保存原查询、digest、attempt/路由授权上下文。沿用 15/30/60 秒的有界空读预算；只调度读，不触发 create。
- `native_completion.all_native_matched` 是普通 Submission 与撤权后内部收尾共用的门禁：计划对象数精确、ID 不重复，每个创建对象 SUCCEEDED/ENABLE、checked_at、无 mismatch，并有对应独立 READBACK 的精确 ID 和 MATCH。回执本身不能完成。审核拒绝/停用、字段差异进入 NEEDS_REVIEW；部分创建沿既有聚合为 PARTIAL。审核运行/经营收益不是本票的成功含义。
- 沿现有 `StepPublic → enrich_steps → PlatformState` 展示真实状态和缺失/差异路径；不公开整个原始平台响应或授权事实，不加新结果页面。SDK读取字段显式包含 secondary_status。

## 验证

命令都从本树 backend 运行；统一前缀：

```sh
python3 /tmp/tkada-native-series-20261112/implementation/run-test.py python -m pytest
```

runner 加载本机私有测试环境（未打印），使用锁定 venv、真实隔离 PostgreSQL/Redis；唯一平台替身位于官方 SDK 的 urllib3 传输边界。没有 mock 完整 business service。

- 关键行为红：将新检查放到起点源码版本执行 `tests/modules/builds/test_native_readback.py::test_native_event_mapping_and_creative_multiset -q --tb=short`，`INCOMPLETE != MATCH`，1 failed。日志 `jb11-red.log`。这是实现后回放缺失行为，不冒称最初落笔即先红；完整提交接线检查也实际以缺少 READBACK 步骤先红，再补接线。
- 字段消融/确定性：`tests/modules/builds/test_native_readback.py -q --tb=short`，86项，覆盖金额、账户/父级、Series/两层身份、地区/排期、ROAS/事件/窗口/竞价、视频/封面/格式、文案/CTA/状态逐项缺失/改变，以及排列/重复素材、非法分页/重复ID/空对象。独立响应 oracle 显式列出平台事实，不按 request 自动构造全部 response。
- SDK/真实 DB 时间线：`tests/modules/builds/test_native_execution.py -k readback_timeline -q --tb=short`，从 JB-10 同一 prepare/freeze/submit/并发/创建链出发，再真实读回，覆盖全 MATCH、视频差异、审核拒绝、精确 ID 空读和部分创建。常量观察（账户、Series、地区、身份、金额/ROAS、素材/封面/CTA/状态/事件）独立编写；只有运行时名称、时间及 seed 抽选文案作动态绑定。实际 `process_unit` 排空及 `finalize_submission` 保存 Submission，读取结果 API 核对差异路径；正常链另验证撤权后内部收尾仍保持完成。
- 相关回归：`tests/modules/builds/test_native_readback.py tests/modules/builds/test_reconciliation.py tests/modules/builds/test_readback_provider_numbers.py tests/modules/builds/test_readback_empty_retry.py tests/modules/builds/test_submission_queries.py tests/modules/builds/test_native_execution.py -q --tb=short`，**161 passed / 1 failed / 171.39s**，`jb11-regression.log`。失败为既有 `test_unknown_campaign_remote_effect_is_recovered_without_create_or_database_lock`（UNKNOWN != SUCCEEDED）。在未改动起点用 git archive 导出独立源码，运行同一单项同样失败，**1 failed / 14.53s**，`jb11-baseline-mini.log`；未放松产品合同迎合夹具。不声称整个 Mini 回归全绿。
- 近似错误探针：临时让所有完整原生观察直接 MATCH，事件与重复封面检查均失败，**2 failed / 0.12s**，`jb11-probe.log`；finally恢复并与仓库外备份逐字节比较相同。恢复后 **85 passed / 0.08s**，`jb11-restored.log`（之后增加排期/地区额外观察检查为86项）。
- 前端 `bun run build`：TypeScript、正式 Vite、demo Vite 均通过，`jb11-frontend.log`。API 类型由现有 openapi-ts 生成器刷新，标准脚本同样的空白归一化。没有用 preview 工具自验。
- 改动脚本 Ruff check/format 与 git diff --check；暂存前检查所有聚焦路径。日志统一位于 `/tmp/tkada-native-series-20261112/implementation/`，不提交私有环境或输出。

最终补跑结果见末尾记录。

## JB-12 交接与边界

本票只读回验收，不自动重建、启用、修改或停用远端。原生 UNKNOWN 无精确回执时保持未决；完整恢复/唯一认领/本地取消归 JB-12。复用当前 `_Claim.native_series`、source request/digest、append-only evidence 与 READBACK 调度，不另建执行链。首片真实平台接受、审核枚举覆盖及经营结果 Unknown；没有运行形式验证器，不声称形式证明。整个 M1 尚未完成，仍待协调者独立验收及 JB-12。

### 最终补跑

- `tests/modules/builds/test_native_execution.py -k readback_timeline -xq --tb=short`：**5 passed / 13 deselected / 52.13s**，`jb11-timeline-accepted.log`。实际数据库持久化 COMPLETED、PARTIAL、NEEDS_REVIEW 已读回；差异路径通过现有结果 API 返回。用例排空真实 unit dispatch revision，未直接写 MATCH 或终态。先前测试直接 finalize 而未排空 unit 的 RUNNING 断言已修正为驱动既有调度。
- `tests/modules/builds/test_native_readback.py tests/modules/builds/test_readback_provider_numbers.py tests/modules/builds/test_readback_empty_retry.py -q --tb=short`：**101 passed / 19.98s**，`jb11-final-compare-mini.log`。覆盖最后的原查询证据和场景标识接线。其后新增审核 DENY/REJECT/DISAPPROV 与非法状态类型五项检查，全部字段专项最终 **91 passed / 0.12s**，`jb11-final-fields.log`。
- 最终 Ruff check 全部通过，git diff --check 通过。前端没有在构建通过后追加变更。相关既有 Mini 失败有起点动态证据；已知 JB-9/JB-10 旧夹具失败未重复运行，也未宣称通过。
