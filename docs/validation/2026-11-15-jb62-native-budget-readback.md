# JB-62 Native 预算归属、冻结事实与回读候选

2026-11-15，本地候选，等待 root 独立验收；JB-63 未启动，M5b 未完成。

## 授权与源码

唯一产品树 `projects/tiktok-ad-automation.wt/feat-m5b-native-budget-validation`，分支 `feat/m5b-native-budget-validation`。START `0e7cf885018922bc111c28de49a99f4b163c9f3e`；源码提交 84db9974b69af5eb97ca82a66f30590803db350b。输入为 `/tmp/tkada-native-series-20261112/m5b-implementation/{JB62-brief.md,approved-scope.md,JB-62.md}` 和父目录 source-notes/root-review；没有补造 Spec/T/R。执行模型 Codex gpt-6.1-sol/high，无子代理。

保持现有直接受理、异步准备、通用 owner/能力/金额校验、Worker、官方 Python SDK 和原队列。没有迁移、依赖更新、新 executor、预览门禁或经营限制。

## 变化与未改设计探针

- START 的 CBO/组预算 owner、Decimal 运算、布尔优化开关缺失时 budget_mode 回退、缺父/分组证据阻断、合批与排期 payload 已存在，本票复用。实际 HTTP 测试展示多个选择归并一个 owner，准备块大小设为 1 验证跨块来源合并；原执行项去重仍生效。
- `capability.budget_facts` 冻结 owner 自己账户的 currency、sources（ref/reason）、affected_refs。原/目标金额仍用既有 original_value/final_value。affected_refs 指实际被修改的预算对象，sources 指本次选择如何归并或展开；不把未选择的全部后代猜作冻结对象。
- 准备跨块只追加独立来源，不覆盖首次冻结的金额/币种。公开 DTO 从持久快照读取，不在轮询时查询后来账户元数据。旧任务没有 budget_facts 时显示空白，不补造历史币种。
- 原任务页显示预算归属、账户、原值→目标值、币种、来源及映射原因、影响对象，沿用执行/送达/核对/attempts 列。广告列表补齐既有预算操作入口，复用同一表单和后端路径。
- 实际发现：START 预算在 SDK 接受后即标 observation ACCEPTED，只有状态操作等待新目录。本票扩展原回读 owner：预算先 REFRESH_PENDING，按冻结路由/actor/账户定向发布；验证当前授权和代次，只比较发送后同连接目录的实际 budget。新观察不同则 NEEDS_REVIEW / budget_observed_different。无观察来源/撤权保留受理事实且需复核。重复回读不重发预算。
- 官方 SDK payload 没有改动。测试精确比较系列单写/Smart+ 组 budget 批请求和 JSON number，禁止携带排期、起止时间或 targeting；输入配置明确含这些字段。SDK 接受、目录尚旧、目录新观察三个阶段分别断言。

## 本人运行的证据

所有 Python 命令从本树运行，前缀 `python3 /tmp/tkada-native-series-20261112/m5b-implementation/run-test.py python`。专用 loopback PG17 / Redis，使用 root 已提供的 Python3.14 环境及共享模块链接；未输出私有配置、未重跑环境 baseline 6 passed。仅 `urllib3.PoolManager.request` 替换官方 SDK 传输；账户/授权/时间为标注的 synthetic 数据。没有 mock 业务 service、目录发布或浏览器 API 响应。浏览器使用现有 `run_native_workspace`/Playwright workspace runner、真实 HTTP、原 Worker body 和 durable outbox 目录 payload；测试控制端点只推进 worker，不伪造业务返回。沿 M5a fixture 在线程退出后清理本租户调度资格，不停止服务、不 FLUSHDB。

| AC | 动态证据与范围 |
| --- | --- |
| CBO 归属、去重 | `test_native_budget_chain`：系列/组/广告同时选择，子组自带 budget；跨块只有一个 owner、一次 POST，全部 sources 冻结；finite 组合分别选三层。 |
| 组预算与展开 | chain 验证系列/组/广告归并组及实际组回读；`test_native_multi_account_currency_and_frozen_children` 两账户各两个已观察组展开，late 组排除；原 `test_previews.py` 缺父、回退、冻结观察门禁回归。多组批端点另由现有 SDK 合同测试验证。 |
| 金额、币种与范围 | 60 个有限组合：2 owner 策略 × 3 选择层 × USD/JPY × set/add/subtract/increase/decrease；独立手算常量，例如 100.25×1.10=110.275、50.50×1.10=55.55，保留精度。多账户同 remote ID 使用各自币种。chain 在准备后改账户币种为 JPY，任务仍为 USD。 |
| 原工作区体验 | `test_native_budget_workspace` 真浏览器选择系列或广告→原预算表单→HTTP 202→自动执行→原任务页原/目标值、来源、币种→定向回读。此前两个系列选择的 CBO/组预算浏览器用例也已绿。没有额外确认步骤。 |
| SDK/排期 | 精确预算请求字段/数值/一次写断言；系列 update 与 Smart+ 组 budget/update；输入排期、起止时间、定向未进入任何写 payload。通用 SDK adapter/batch 套件回归。 |
| 接受后回读 | chain 的匹配/不匹配四场景：写后旧目录仍 REFRESH_PENDING，新目录才 ACCEPTED 或 NEEDS_REVIEW；再次刷新无新增 POST。`test_native_budget_readback_revocation` 在目录发布后撤权或增授权代次，拒绝复用已读事实，无新平台调用。 |
| 范围与通用回归 | 真实 scoped fixture / frozen route；多账户同 ID 不串币种；现有非 Native preview、execution、direct submission、SDK tests 和 Native status HTTP 用例。没有借 Native 测试声称所有历史时序已修复。 |
| 证据与 Unknown | 本文及可复现测试源；真实 TikTok、真实授权、生产、平台预算政策接受均为 Unknown。 |

最终命令与结果：

- `-m pytest backend/tests/modules/ad_management/test_native_budget.py backend/tests/modules/ad_management/test_native_status.py -k 'native_budget or native_status_http_worker_readback' -q`：75 passed、18 deselected（78.32s）（含恢复探针后的 GREEN）。
- `-m pytest backend/tests/modules/ad_management/test_previews.py backend/tests/modules/ad_management/test_execution.py backend/tests/modules/ad_management/test_direct_submission.py backend/tests/integrations/tiktok/test_management_adapters.py backend/tests/integrations/tiktok/test_management_batches.py -k 'not direct_10748' -q`：146 passed、1 failed、1 deselected。唯一失败是旧测试将预算刷新请求排队当作已观察，已按本票合同修正该断言，精确重跑 `test_execution.py -k targeted_refresh_failure -q`：1 passed、26 deselected。其余未受此测试断言修改影响的通过证据复用。
- `node_modules/.bin/tsc -p frontend/tsconfig.build.json --noEmit`：通过；`bun run --cwd frontend build`：正式及 demo 构建通过（广告入口单行补齐前，入口补齐后 tsc/真实浏览器验证）。
- `-m ruff check backend/app/modules/ad_management/{execution,expansion,preparation,previews,schemas}.py backend/tests/modules/ad_management/test_native_budget.py`、`git diff --check`：通过。Biome formatter 尝试因仓库已有 nested root configuration 报错，未迁移配置。
- 早期测试夹具少导入 committed_management_env 导致 setup error，补上后不计 RED；早期少传 advertiser_id / batch success IDs 的 SDK synthetic response 已按原合同补齐，不修改业务解析器迁就替身。

## RED、错误探针与恢复

初始新增 chain 在公开回执 `budget_facts` 缺失处 RED（`/tmp/jb62-red.log`）；实现后 4 passed。之后对目录尚未刷新却 observation=ACCEPTED 的断言 RED，修复原 execution owner。广告浏览器在原列表缺“修改预算”按钮处 RED（`/tmp/jb62-ad-ui-red.log`），补入口后通过。

`/tmp/jb62-probes.py` 真改源码运行五个近似错误，均在具体业务断言拒绝：冻结币种改 EUR；目标金额加 0.01；跳过跨块已有 owner 去重；跳过发送后观察时间要求；把目标金额当实际观察值。每个文件先复制到仓外临时目录，`finally` 还原，`cmp` 字节一致；最后再跑相关 GREEN。日志 `/tmp/jb62-probe-{currency,amount,duplicate,freshness,false-observation}.log`，总记录 `/tmp/jb62-probes.log`。有限金额/owner 组合不代表无界形式证明；没有使用 Build TLC 充当管理证明。

## 已知失败、继承证据与交付边界

root 已成立的环境 6 passed 和 START direct10748 SQL 26>25 仅继承，本人未无故重跑或放宽阈值。新的 147 组回归明确排除此已知失败；不宣称全历史套件全绿。原 M5a UNKNOWN/恢复仅作为既有实现和下一票消费路径，没有把状态验收称为预算专项验收。

无 main/其它树修改、checkout/reset/stash/fetch/rebase、tracker 写、push/PR/部署、真实平台/auth/R2/广告操作。`.venv` 与 `node_modules` 原共享链接保留；服务最终清理由 root 负责。

## JB-63 消费合同

root 独立验收本候选后才释放 JB-63。消费本树源码提交 84db9974b69af5eb97ca82a66f30590803db350b，无需另起 executor 或重新设计预算入口。预算原/目标值和预算事实冻结在原任务项，恢复路径现有 capability 复制继续保留 currency/source；不能拿后来账户元数据改历史含义。DTO `budget_facts` 可空（旧任务），reason 枚举为 owner_selected / campaign_budget_owner / adgroup_budget_owner / campaign_adgroups_expanded。不要为展示复制执行项。

JB-63 专项覆盖预算 mixed results、UNKNOWN 只读新鲜证据、原事实不变、显式反向恢复、当前预算冲突和 grant gates；不得自动 UNKNOWN 重发。现有 `test_native_status` 的 fixture、`drive_management`、`drive_directory`、`run_native_workspace` 可继续复用。预算新观察不匹配可出现 NEEDS_REVIEW，不等于明确写失败；SDK ACCEPTED 和 observed ACCEPTED 仍是两份事实。root 保留阶段验收、whole-scope Standards/Spec、集成和票据关闭权。

## Root 独立阶段接受

Root在精确候选959bc8198aec326c579701ddc4f7120b9988ef4e核对实际预算展开/跨块冻结/公开DTO/任务页/读取授权与新鲜观察差异，亲跑chain、multi_account、readback_revocation：7 passed、62 deselected/25.58s。Ruff覆盖五个产品owner及Native预算检查，diff通过。其余75检查、146普通通过及旧刷新断言的定向复验、浏览器/五探针/build/tsc按匹配源码继承，未冒称亲跑。affected_refs仅标识实际预算修改对象，不声称穷尽未选择后代。共享读取owner的非预算调用方继续纳入整片审查。

结论：JB62阶段接受，可在同一集成树释放JB63；两票保持未关闭，直到whole-M5b审查与本地合入。不推送/部署/真实平台操作；旧SQL计数失败与平台未知边界保留。
