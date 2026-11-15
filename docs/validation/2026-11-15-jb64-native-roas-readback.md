# JB-64 正常目标 ROAS：本地候选

## 边界与合同

唯一产品树为 root-owned `feat-m5c-native-roas-validation`，起点 `26ea29fd374848d687b35a3c086838202aff533a`。按批准的 JB-64/root 修正版 scope 实现；没有新建 checkout、迁移、依赖升级、服务停止、真实平台调用、Linear 写、push/PR、main 合并或部署。JB-65 未启动，整个 M5c 未宣称完成。

沿现有工作区/参数表单直接 202、异步准备、通用管理 Worker、官方 SDK 与目录 staging/publication。没有原生执行器、队列、全局确认门槛或经营限制。更新只发送 advertiser_id/adgroup_id/roas_bid；不回写创建配置、预算、排期、定向。现存正数有限输入规则保留。

官方一手证据（2026-11-15 重新读取）：

- [官方 SDK 更新模型](https://raw.githubusercontent.com/tiktok/tiktok-business-api-sdk/main/python_sdk/docs/SmartPlusAdgroupUpdateBody.md)：两个 ID 必填、roas_bid 可选；不是创建字段 bundle。固定环境 SDK `AdgroupApi.smart_plus_adgroup_update` 使用原 `/open_api/v1.3/smart_plus/adgroup/update/`。模型只能证明字段/路由，不证明真实账户资格。
- [官方预算策略帮助页](https://ads.tiktok.com/resources/help/article/about-budget-strategy-for-your-upgraded-smart-experience)：CBO bid adjustments 影响全部广告组。
- [官方 Native 创建说明](https://business-api.tiktok.com/portal/docs/create-an-upgraded-smart-minis-campaign/v1.3)：创建的模式/日预算组合与 CBO 一致性，仅作创建依据，不变成更新硬门槛。
- 更新 API 文档 ID1843314894279682 返回无可读正文；Native 具体账户更新适用性、传播/失败语义仍 Unknown。ABO 独立 owner 更新是按原单 adgroup_id 端点与预算策略作本地实现，不把 wire 替身当真实平台证明。

准备读取已发布同连接目录，冻结 `roas_facts` 于既有 capability JSON：budget_strategy、mode、campaign_ref、parent_configuration、sources、affected_refs。原/目标比例仍在 original_value/final_value，发送 route/actor/tenant/授权代数与 attempt 合同不变。当前目标 ROAS 模式用已存在 `deep_bid_type=VO_MIN_ROAS`，父预算策略必须有明确 boolean；缺证据对象 UNSUPPORTED，CBO 任一影响成员未核实则该范围零写。没有要求创建时 optimization_event/vbo_window/billing_event 等字段重新齐全。

无 schema migration；公共 DTO 增加可空 roas_facts，前端类型同步。历史缺项保持空白，不从今日目录补造；历史冻结命令缺 CBO 证据不再凭 parent_ref 猜系列联动，不全局取消或重新计算其 intent。

## 逐 AC 证据

| AC | 亲跑本地结果与独立预期 | 继承/限制 |
| --- | --- | --- |
| 1 原工作区/组 owner | CBO/ABO 两条 Playwright 真页面：广告选择→原 ROAS 表单→202→原 Worker→详情→目录发布；HTTP 覆盖系列/组/广告。SDK POST 仅原 adgroup/update、三个字段 | 认证和账户数据合成，非真实 BC |
| 2 支持/零写 | mode/strategy/peer_mode/grant 逐项零 POST；创建字段缺失的正常链可写 | 必需证据来自当前目录，不能证明实时平台资格；缺失时明确未核实 |
| 3 CBO/ABO/跨块 | CBO 2 成员1 POST；ABO 同父两种原值各发独立目标；all 选择三来源、chunk=1 仍准确合并来源与去重 | 影响成员是冻结选择时刻之前已发布的目录集合，无法证明平台不存在未同步成员 |
| 4 准确比例 | 手算 1.20×1.10=1.32；ABO 1.50×1.10=1.65；发送前将当前文本改1.2000仍接受；SDK JSON wire断言无额外字段 | 不声称无界数值形式证明 |
| 5 冻结展示 | 真实详情显示 owner、原/目标比例、模式、CBO/ABO、来源与全部影响 ID；轮询事实不变。原已有预览 DTO 同源映射 | 历史不补造；JB65 恢复预览另票 |
| 6 受理/逐成员观察 | execution ACCEPTED 保留 request receipt；本次发送后每个成员各自匹配才 observation ACCEPTED，UI 为“已观察目标”；missing/stale/different/grant/connection 均不能假绿；重复任务请求、Worker、refresh无新增写 | 缺失/陈旧保持 REFRESH_PENDING 并有明确原因；值不同/撤权 NEEDS_REVIEW。没有同步来源沿既有不可用语义 |
| 7 真实本地/错误探针 | 真实 HTTP/PG17/Redis、原 Worker函数与持久 payload、目录 staging/publication，仅 SDK transport 替身；原两条浏览器通过 | 原 runner 非独立 broker/Celery 常驻进程交付验收；未跑全仓 |
| 8 Unknown | 官方资料/静态 SDK/本地动态证据分列 | 真实账户、真实 TikTok 接受/生效、生产与真实授权均 Unknown |

## 验证记录

命令均由 root 准备的 guarded runner 执行，不打印私有配置；套件/探针串行，不共享库并发。Python3.14/现有 Bun，无安装。root 的环境基线6 passed按已成立证据继承，不重跑。

- RED：新增 `test_roas_chain[campaign-True]` 在起点失败，等值表示导致 roas_series_conflict，未进入正常 PENDING。独立预期来自批准票的十进制等价和手算比例。
- 初次实现：6 chain passed /49.84s。
- 原工作区：2 workspace passed /27.71s（CBO/ABO）。真实页面响应没有 mock；时间/认证/SDK输入合成。
- 当前定向集合：86 passed /13.62s（connection、all-True/all-False、原 previews、原 management_batches）。更新旧 CBO fixture 显式补预算策略/模式/冻结 CBO 事实，保留其原独立断言。
- 其余新增边界：mode/strategy/peer_mode/grant、missing/stale/different 已各通过；撤权后详情受权限限制，改以原持久项核对 receipt/NEEDS_REVIEW。
- 最终 ROAS 专项：`python -m pytest tests/modules/ad_management/test_native_roas.py -q`，**20 passed /72.16s**，包含两条真实浏览器，非与前述次数累加。CBO 原值已经是目标的成员也取得本次独立受理/观察。
- 实际源码探针：去掉CBO-only合批门禁→ABO chain拒错；去掉ROAS Decimal比较→等价原值发送拒错；去掉发送后时间检查→stale成员假绿被拒；去掉CBO已等目标兄弟纳入本次回读→already_target拒错。四次均外部备份、finally复制还原、`cmp`一致；前三次恢复后的最终20项绿跑，第四次后定向绿跑见收口结果。探针日志在 root 外部 m5c-implementation/probe-{dedup,ratio,freshness,nochange}.log。
- TypeScript：`node_modules/.bin/tsc -p frontend/tsconfig.build.json --noEmit` 通过。
- 开发中测试夹具错误：BCAccountAccess四列主键漏 connection；错把撤权后的403当详情读取；null连接违反目录来源约束。均修正夹具，非产品RED。首次组合收集还暴露重复configuration关键字，已修正。几次 tsc 路径未找到后使用现有 root共享路径，无安装。

收口：第四个探针还原后，`test_roas_each_member_observed[already_target]` + `test_management_adapters.py` + `test_old_roas_commands_do_not_invent_cbo` + `test_native_budget_chain`：**30 passed /27.05s**。Ruff check/format、TypeScript和差异检查通过。以上重叠场景不累加为独立覆盖数量。

M5a/M5b 接受记录按匹配源码继承，只覆盖原启停/预算，不能据此称 ROAS 已证明。已知 direct10748 SQL26>25 未修改也未重跑；旧间歇状态 stale 根因仍 Unknown。未做全仓/生产验收。

## JB-65 消费交接

本候选供 root 阶段验收。JB65 消费 capability.roas_facts 与 original/final/route/request attempts；Decimal 比较和 CBO-only planning 为共用 owner。正常 CBO 每成员仍有独立 attempt/观察，共享一次物理请求 receipt。ACCEPTED 与 observation 完全分离，UNKNOWN 不因 normal readback 变为 ACCEPTED。

JB65 仍需实现/验收 UNKNOWN 有界只读轮次、显式反向 restore/current 原值/目标/第三方值、完整 CBO 成员冲突、freshness/current权限双门禁和恢复预览UI。不得把本次事实DTO等同于恢复资格；原 sent UNKNOWN 可按已批准合同在新鲜证据足够时准备独立反向意图，不能自动重放。没有在本票扩展恢复行为或开启下一票。

部署提示：虽然没有数据库结构迁移，持久任务 capability/公共 DTO 新增冻结事实，属于语义合同变化。后续发布必须按仓库部署合同审查具体方案；本票没有发布授权。ADS_SYNC_ENABLED 仍是原开关，没有新增开关或扩大开关语义。


## Root 阶段验收与来源归属修复（2026-11-15）

候选 `43b21ea9d82299a22c835a5a351b6ff7e9443fa7` 经 root 源审，亲跑原 suite 的 all-chain、unverified-zero-write、逐成员观察共 12 passed /37.31s。其余 UI、SDK wire、回归和构建证据继承前述匹配源码结果，不计为 root 亲跑。

发现并修复真实来源映射缺陷：同一准备块选择两个 ABO 组时，每个 owner 的冻结 `roas_facts.sources` 都携带两组来源；应只保留其自身来源。现按父系列与组 owner 建立来源索引：CBO 共享该系列的选择来源，ABO 除显式选中系列外按各组（或所选广告的父组）独立归属，不混入其它 owner。没有改变物理发送、预算、排期、模式门禁或历史冻结内容。

新增公开 HTTP→真实 PG/异步准备→任务详情检查 `test_roas_sources_follow_actual_owner`（CBO/ABO）。独立预期来自已批准 CBO 联动/ABO 独立及任务来源审计合同。初始误用 DTO 字段 object_id 的 KeyError 已改为现有 remote_id，不算行为 RED；随后真实 RED 为 ABO 多出兄弟来源（1 failed,1 passed）。修复后来源与全部八条正常链 10 passed /52.17s。近似错误探针删除 CBO 来源共享分支，实际业务断言 RED（1 failed,1 passed），仓外备份 finally/cmp 恢复后 2 passed /0.56s。日志在外部 m5c-implementation/jb64-sources-{red,green,probe,restored}.log。

静态检查使用项目固定 venv Ruff 通过、diff check 通过。一次误调用系统旧 Ruff 对 fixture 重名及格式报错，改用固定 venv 后通过；未改 fixture 或弱化检查。无前端变更，不重复 UI build。上述验证彼此重叠，不累加计数。

JB64 阶段接受，释放同树 fresh Codex gpt-6.1-sol/high JB65；不提前 Done，不代表 M5c 整片放行。JB65 继承原始/目标比例、按实际范围归属的 sources、CBO/ABO、affected_refs 及逐成员新鲜观察合同。整片仍须 UNKNOWN/显式恢复、fresh Standards/Spec 审查与本地集成。真实 Native 平台适用性、生效与生产仍 Unknown；旧 SQL26>25 和旧间歇 UI 根因边界保留。无 provider 写、push、PR 或 deploy。
