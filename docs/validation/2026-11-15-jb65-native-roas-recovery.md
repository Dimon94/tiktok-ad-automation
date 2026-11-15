# JB-65 目标 ROAS 核查与显式恢复：候选验收

START：`5ce82f647d3a0fc8a83aec6e2f2e65feec6241d2`，分支 `feat/m5c-native-roas-validation`。用户批准同一集成树串行写例外；没有 checkout/setup、其他树/main 修改、子代理、tracker 写、push/PR/deploy、安装、FLUSH 或服务停止。source/TIP 见收口记录；root 负责阶段接受、整片独立 Standards/Spec 审查和本地集成。

实现沿原 `prepare_restore`、`_same_frozen_value`、原 Worker 的领取及物理发送前围栏、官方 SDK 和原目录发布。没有 Native executor、队列、新强制预览或经营限制。UNKNOWN 核查 owner 无生产代码改动；沿既有三轮上限、人工 round 和晚到隔离。恢复是另外明确确认的新意图，原 UNKNOWN/receipt/requestID/attempt/冻结值不改写、不重发。

ROAS 恢复复用预算的发送后目录证据边界；没有引入额外时间窗口。每个来源必须是有效 ACCEPTED 或实际发送的 UNKNOWN。当前等于最后目标可准备；十进制等于历史原值是 NO_CHANGE；缺值、旧于发送的目录、连接/权限/actor/授权代数失效阻断。CBO 冻结每成员原值、目标、发送资格与发送时间；恢复预览和发送前均核对完整当前成员集合、每个成员来源/模式/观察时间、冻结原/目标一致及当前方向一致。ABO 各自恢复原比例，不按共同父系列合并。

原恢复预览现在返回 ROAS facts 与本次读取的当前值、观察时间、连接、原任务 ID，并显示冲突原因。该观察 DTO 仅描述生成预览时的目录，不伪造执行回执；确认后仍重新核验。历史缺必需 ROAS 范围事实时明确冲突，不猜 CBO、不取消已有历史任务。

## 逐 AC

| AC | 本地实际证据 | 限制 |
| --- | --- | --- |
| 1 真发送丢响应与逐成员核查 | 官方 SDK transport 抛 TimeoutError；UNKNOWN 每成员有独立 attempt。精确等额、不同、缺失、陈旧、不完整、撤权均通过原只读 worker 核查；原执行/attempt 不变 | 输入/时间/账户/认证合成；无真实平台 |
| 2 有界/人工/晚到/混合 | ROAS 三轮后 RECONCILE_FAILED，人工新 round、读内另起 round、旧 auto 重投隔离，重复刷新零 POST；ABO ACCEPTED 兄弟观察不被 UNKNOWN/撤权阻断 | 重复驱动原 Worker 处理被同父协调锁延后的 PENDING；不是重发 UNKNOWN |
| 3 原预览和两种来源 | 8 组合：CBO/ABO × 原 ACCEPTED/UNKNOWN × 恢复 ACCEPTED/UNKNOWN；反向 original/final、原 facts、审计 source_task_id；浏览器展示原任务/owner/比例/来源/影响/当前证据 | 没有新建独立恢复入口 |
| 4 当前值与权限双门禁 | 原值 1.20/1.2000 等价 NO_CHANGE；第三方值、缺值、陈旧、来源连接、grant/manage、route revision、actor 在 preview/send 阻断、零写 | 新鲜度沿原发送后证据合同，不声称实时平台同步 |
| 5 CBO 全范围/ABO | 增加/删除成员、部分已经原值、冻结原值不一致、反向目标不一致均不能部分发送；正常恢复一次物理 CBO POST、逐成员独立观察；ABO 原值 1.20/1.50 分别恢复 | 完整集合指当前已发布目录；真实平台未同步成员仍未知 |
| 6 恢复回执与观察分离 | 恢复 UNKNOWN 仅 OBSERVED_MATCH，attempt 一次；ACCEPTED 后逐成员新鲜目录才 observation ACCEPTED；原任务公开事实不变；恢复 preview 持久审计关联 source_task_id | 不是平台已接受证明 |
| 7 实际 seam / 探针 | 真 Playwright 工作区→HTTP→自有 PG17/Redis→原管理/目录 Worker body→staging/publication；唯一平台替身为 SDK transport；scope/Decimal/UNKNOWN 三个真实错误源码探针被拒并 finally/cmp 还原；相关 M5a/M5b 回归 | 原 Worker body 消费实际持久 payload；未验收独立 broker/Celery 常驻进程、真实 OAuth/平台/生产 |

独立预期来自批准 JB-65：1.20×1.10=1.32，1.50×1.10=1.65；反向精确回到 1.20/1.50；CBO 一次物理写、ABO 两次；UNKNOWN 即使匹配也不等于 ACCEPTED。断言公开 HTTP 结果、持久审计和 SDK wire，不以实现计算预期。

## 验证与失败记录

所有测试从指定树使用 `python3 /tmp/tkada-native-series-20261112/m5c-implementation/run-test.py ...` 串行运行，无套件/探针重叠。

- 行为 RED：恢复 preview 的 `roas_facts` 为 null；另一个 RED 是缺少当前证据 DTO。外部日志 `jb65-red.log`、`jb65-evidence-red.log`。
- 开发失败：最初把 linked campaign 也算作两个实际 ROAS owner，修正为公开 kind=adgroup；误给 DTO 传 parent_ref 导致 schema 拒绝，已移除；ABO 同父锁延后项在首次 Worker 后仍 PENDING，夹具提前发布目标导致原值冲突，改为沿原 Worker 驱动待处理项；复制状态测试遗留 DISABLE 文本，改为独立 Decimal 预期。上述均非产品 RED。
- 29 passed 后一个夹具错误：只修改一项 capability 来模拟不一致历史，未代表真实外部变化；改为构造 CBO 反向目标不一致，由原规划 owner 整组拒绝。未弱化零写要求。
- 边界/浏览器定向：9 passed /43.60s；NO_CHANGE 与 mixed/revoked：3 passed /6.52s。与最终集合重叠，不累加。
- 三个源码探针：移除发送前完整成员 gate、ROAS 退回字符串比较、核查匹配改写 execution ACCEPTED；目标检查均真实失败。源码仓外备份、finally 恢复并 `cmp` 一致。日志 `jb65-probe-{scope,ratio,unknown}.log`。
- 最终集合、静态检查、source 提交见收口记录。

SQL26>25（direct10748）原基线未更改、未重跑；旧间歇状态 UI stale 根因仍 Unknown。没有用单次绿跑抹去它们。JB64 正常链及官方公开/固定 SDK 合同沿 root 阶段接受证据继承，不重复充当 JB65 平台验收。真实账户适用性、具体 Native 传播/失败语义、真实授权、TikTok 接受/生效及生产均 Unknown。

无数据库迁移；公共 DTO 与恢复 capability 持久语义有变化，后续发布方案须由 root 按部署合同审查。ADS_SYNC_ENABLED 等既有开关语义未改。

## 收口记录

产品与测试 source：`4832fcd15e9311524ddfec2dbb51d1e1f810ab9f`。本记录随后的提交仅更新文档；最终 handoff TIP 由该文档提交标识，无自引用哈希。START 至 source 为 JB65 唯一产品增量，JB64 来源保留在 START。

组合回归曾出现 **1 failed /50 passed /267.25s**：`unknown_rounds[1.32000-OBSERVED_MATCH]` 最后仍为 RECONCILE_REQUESTED。定位为夹具只消费“最新一个”人工 round，而原接口每成员分别生成 round；修正为在读取前捕获每成员最新的持久请求集合，再驱动原 Worker，读内产生的新 round 留给下一次驱动。没有修改生产 reconciliation。该失败已由最终源的全部五条 ROAS round 检查替代；不把最初 50 项计作最终 50 项。

另补强 CBO 校验为每成员自己的最新 attempt 发送时间，不能借本项时间证明兄弟新鲜；相关 SQL 一次取各项最后 attempt。第四个真实源码探针将每成员时间错误换成本项时间，公开 preview “全组冲突”断言拒错，finally/cmp 恢复。

最终源的分段命令（均置于相同 guarded runner 后）：

```text
python -m pytest backend/tests/modules/ad_management/test_native_roas_recovery.py -k 'not workspace and not restore_current_gate and not mixed' -xq
# 15 passed, 27 deselected /92.87s（新增 own_send_time 用下一段闭合）
python -m pytest backend/tests/modules/ad_management/test_native_roas_recovery.py -k 'workspace or member_own_send_time or restore_current_gate' -q
# 27 passed, 16 deselected /100.14s，含 CBO/ABO 真浏览器、24 个 preview/send 冲突门禁、成员发送时间
python -m pytest backend/tests/modules/ad_management/test_native_roas_recovery.py::test_roas_mixed_and_revoked_unknown backend/tests/modules/ad_management/test_native_recovery.py::test_native_mixed_results_continue 'backend/tests/modules/ad_management/test_native_budget_recovery.py::test_budget_reverse_frozen_preview[False-False-True]' -q
# 3 passed /13.17s：JB65 mixed 1、M5a/M5b 代表回归各1
node_modules/.bin/tsc -p frontend/tsconfig.build.json --noEmit
# exit 0
/tmp/tkada-native-series-20261112/implementation/venv/bin/ruff check <四个本轮 Python 文件>
/tmp/tkada-native-series-20261112/implementation/venv/bin/ruff format --check <四个本轮 Python 文件>
# All checks passed，4 files already formatted
```

JB65 最终 43 个参数化检查分段全部通过，另 2 项原通用行为代表回归；分段/早期结果互相重叠，不累加。真实浏览器日志有既有 Python3.14 asyncio 弃用告警。静态 diff check 初次发现测试文件 EOF 空行，已删除，最终差异/暂存差异检查通过。无依赖/环境迁移或真实平台行为。

提交前状态仅七个本轮代码/测试文件和本验证文档；`.venv`、`node_modules` 原链接保持未跟踪。whole-M5c handoff：root 以本候选做阶段接受与 fresh Standards/Spec 全片审查；未提前关闭 JB64/JB65 或 Epic，未合 main。后续必须继续分列公开一手合同、SDK wire、当前本地证据与真实平台 Unknown。
