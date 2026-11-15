# JB-66：CBO ROAS 完整发送范围

## 本地结果与边界

从 `13ee57901e912f42625d9bd81b4fe9bd9ceb714e` 建立独立分支 `fix/jb66-cbo-roas-scope`。修复正常 CBO 冻结 A/B 后，B 原值或来源变化却仍裁剪 B、只给 A 发送系列更新的问题。无依赖升级、迁移、开关或新执行器；没有推送、合并 main、修改 Linear 或部署。

复用 `execution.py` 的范围 owner，原恢复全成员规则保留。正常发送按当前目录与 `roas_facts.affected_refs` 核对完整集合，再用每个冻结 `ManagementTaskItem` 的原值、来源、模式与既有 `_can_run` 资格判断。批量和单项的既有发送围栏均消费此 owner，不以剩余队列重新定义影响范围。

`_can_run` 仅在 CBO 发送围栏明确提供同一 claim token 时，允许核对本次已共同领取的成员；普通重复投递仍使用默认拒绝规则。ABO 不进入系列范围检查，CBO 仍去重为一次 SDK 更新；准备时已等于目标的兄弟保留自己的原值和审计。

兄弟尚未到重试时间时，整组等待且不消耗新 attempt，全部到期后共同发送并分别记回执。兄弟已终结时，可运行项落为 CONFLICT，避免沿旧 retryable attempt 永久 RUNNING。原 task 汇总合同仍生效：含 NO_CHANGE 联动提示时可为 PARTIAL，其他情况可为 FAILED；不把 PARTIAL 等同于曾向平台发送。

详情沿原 `receiptLabel` 增加范围冲突与等待原因的中文提示，没有新增交互流程。

## 复现与验证

真实本地 FastAPI HTTP → 专用 PG17/Redis → 原准备/管理 Worker → 官方 SDK；唯一平台替身位于 `urllib3.PoolManager.request`。使用合成租户、账户、授权和广告目录，测试没有调用真实 TikTok。领取后变化通过包裹真实 Redis eval、在实际续租后发布合成目录变更触发，没有替换业务 owner。

- 原始反例：B 的原值变为 1.25 或切换来源连接，均实际产生 1 次错误 POST；`2 failed in 2.80s`。首次修复后同输入零 POST，`2 passed in 1.08s`。
- 审查发现部分重试到期和终结 NOT_SENT 仍可裁剪成员。持久真实 attempt 输入复现两例，各错误 1 次 POST：`2 failed, 11 deselected in 1.35s`。接入既有资格 owner 后通过。
- 终结任务读回实测 `RUNNING / NOT_SENT / roas_scope_conflict`。补永久冲突收口后结束，重复投递不新增发送或 attempt；等待用例仍可在兄弟到期后一次成功。验收要求是合理终态；夹具包含原 NO_CHANGE 系列提示，因此不强制所有任务均为 FAILED。
- 最终专项：`test_roas_send_scope.py` **14 passed / 3.62s**。覆盖原值、来源、模式、成员增删、领取后变化、只剩单项、ABO 独立，以及等待/终结/已排除兄弟的重试和任务状态。
- 既有正常/恢复非浏览器回归：**61 passed, 4 deselected / 194.65s**，在最终资格判断补充之前执行。资格判断补充后的定向集合 **30 passed, 21 deselected / 71.08s**，包含原专项 13 项、正常 CBO/ABO、已等目标兄弟、反向恢复、普通重复领取/UNKNOWN/过期预请求/已知未发重试。末次终结状态修改由最终 14 项覆盖，不声称重跑了这两个集合。
- Ruff check/format、Python 语法、TypeScript noEmit 与 diff check 均通过。前端只增加原因映射，不新增浏览器测试或宣称页面动态验收。

上述集合重叠，不相加为独立覆盖数量。运行入口为仓库外 guarded runner：

```sh
# 从本任务树 backend/ 运行，专用本地服务运行时可复现。
python3 /tmp/tkada-jb66-20261115/run-test.py python -m pytest tests/modules/ad_management/test_roas_send_scope.py -q
```

测试夹具与完整断言保存在仓库；私有环境、原始日志和 probe 脚本留在 `/tmp/tkada-jb66-20261115/`，不提交凭据或临时数据库。

## 拒错证据与复用记录

实际修改生产源码后运行测试，均由业务断言拒绝，而非导入/语法错误；每次 finally 还原并逐字节核对：

- 去掉正常完整范围 gate：9 个相关反例失败。
- 去掉批量最终观察围栏：2 个领取后变化反例失败。
- 错把 ABO 送入系列 gate：2 个独立 owner 反例失败。
- 去掉永久范围冲突的终态映射：终结/已排除兄弟反例失败。

前三个探针还原后原 13 项通过；末次探针还原后完整 14 项通过。原值与来源反例、实际 probe 日志、终结状态读回分别保存在 task-red.log、probe-*.log 与 terminal-readback.log。

DRY record：范围是正常 CBO 发送门禁；搜索了同目录 expansion、preparation、dispatch_work、batch_execution、execution、actions 及管理 SDK 合批。复用原恢复范围 owner、`_can_run`、Decimal 比较、mutation lease、任务汇总和原 SDK 发送链。没有新增并行资格 owner。剩余逐成员本地观察检查保留在既有发送围栏。

## 审查与限制

初始范围两轴通过；Spec 指出的基线重试裁剪已动态复现并纳入修复。扩展资格范围的两轴独立只读审查通过；随后定点核对发现新增终结状态停滞，已以上述实际读回、拒错和终态测试修复。最终终结状态差异的 fresh Standards / Spec 定点独立审查均 PASS，P0/P1/P2/P3 均为 0；审查者只读静态核对，没有亲跑动态测试。末次源码探针还原后的完整专项为 14 passed / 3.42s。

本地门禁只比较已发布目录，不能证明平台不存在尚未同步的成员，也不能覆盖最后检查之后的远端变化。真实 TikTok 传播/接受/生效、真实授权与生产仍 Unknown。没有独立 broker/Celery 常驻宿主交付或真实浏览器验收；不以 Worker 函数和 SDK 替身证明这些事项。


## 前置基线重放（2026-11-15）

原提交 `1d67d28d4744474e42f554e62c423d1675ea9cea` 的单个修复已从 M6 基准 `13ee5790` 重放到前置集成 `79ed56279182a9bbf95aff55e02ae21f6a43ec7c`。只重放 JB-66，不携带 M6；产品补丁自动合入，进度文档合并保留两侧记录。一处可空 JSON 列使用已有 `col(ManagementTaskItem.parent_ref)`，SQL 语义保持，符合上游 ty 门禁。

新基线上的完整专项 **14 passed / 3.33s**；Ruff、ty 与 TypeScript noEmit 通过。独立保留性检查 PASS：产品/测试补丁除一处 `col()` 类型表达外与原修复一致，上游观察保护和类型修复完整保留；仅原六文件差异，无额外 M6 代码。原资格/范围/最终发送围栏/恢复与 ABO 的已验证行为继续复用，不重复未改变的广泛验证。此前置集成的迁移、AI、Native/调度和构建证据见[前置集成验证](2026-11-15-native-m5c-upstream-integration.md)。

前置合并请求 [upstream PR #9](https://github.com/yaotingfeng/tiktok-ad-automation/pull/9) 已发表；其 GitHub CI 独立运行，不以本轮本地检查替代。只有上游维护者将前置基线合入 main 后，JB-66 才能对 upstream/main 形成六文件窄差异。源分支可先发表至现有 fork，不将大范围 upstream PR 或 fork 内部 PR 当成已完成窄 PR。无生产部署、真实 TikTok 写入或 Linear 状态修改。
