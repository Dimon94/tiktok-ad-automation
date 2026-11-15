# JB-59 / M5A-01 本地启停与回读候选

范围依据：`/tmp/tkada-native-series-20261112/m5a-implementation/approved-scope.md`、完整 JB-59 / JB-60；未使用未批准的强制预览草案。仅指定集成树、`feat/m5a-native-status-validation`，START `9d51cc9b37faf1b6a830fa025cbd377ace716048`。源码/测试提交 `20709eb550c07d4fc2e67eec7f7d6863594c73a6`。实现者 Codex gpt-6.1-sol/high，自己执行，无子代理。本文不是 Root 阶段验收或 Done。

## 结果与必要修复

工作区报表行详情的启用/暂停按钮直接冻结选择并 HTTP 受理，没有新增预览或确认。真实 loopback HTTP、PostgreSQL、Redis、原异步准备/管理 Worker、官方 SDK、原定向目录采集/发布、任务页与当前工作区已走通 Campaign / Adgroup / Ad × ENABLE / DISABLE。SDK 的 urllib3 传输使用合成响应；认证、失败恢复时钟是测试夹具。没有真实平台调用。

实际 RED 暴露并在唯一原 owner `backend/app/modules/ad_management/execution.py` 修复：

1. 数据库保存了原始 ENABLE/DISABLE，但公开任务项把原值转数字后返回 null。文本启停原值现在原样返回，素材启停和改名同样保留文本；数值操作继续原转换。
2. 写入受理、回读仅排队时，观察已标 ACCEPTED。启停现在保持既有 REFRESH_PENDING，当前目录观察必须晚于发送、来自同连接/通道且符合目标，原 Worker 才结束等待。冻结原值、目标、回执均不改。等待刷新保留 RUNNING；UNKNOWN 汇总优先级保持原规则。单项正常排队等待复用原时间桶唤醒；原刷新异常的重试入口和其它字段行为保留。批量直接受理已有准备 watchdog 的未来消息可继续原 Worker，不新增执行器或调度框架。

本轮没有迁移、Native 资格门槛、HTTP 网关、BROAS 依赖、全局 UX 改动。目录展开仍在异步准备中。

## AC 矩阵与证据归属

| JB-59 AC | 实际证据 / 复用证据 | 结论 |
|---|---|---|
| 1 三层双向、直接受理、任务返回 | `test_native_status_workspace` 六个 Chrome 场景；真实按钮→选择 HTTP→202→原 Worker→任务页→目录发布→当前工作区；另六个 HTTP 场景 | 本地通过 |
| 2 Smart+ 官方路由及冻结对象/账户/状态 | 六组合逐次断言 SDK status/update body，官方 Smart+ get 实际读回；组包含 NATIVE_SERIES / Series / identity，目录类型 SMART_PLUS | 本地通过 |
| 3 仅选中层级、停用父级仅提示 | 精确物理 POST 只有所选 ID；`test_native_disabled_parent_is_only_a_hint` 保留父级 NO_CHANGE / parent_disabled，仍仅一条子级写 | 本地通过 |
| 4 冻结意图、异步原值、目录变化不回写 | 202 为 PREPARING；受理后改原选择为三层，任务仍只写原选对象；发布后原值/目标不变；既有 direct preparation 并发/检查点回归 | 本地通过；不重复证明未变模型全部属性 |
| 5 最高 Seam | 主浏览器没有 page.route/mock response。API 使用真实应用；仅替换用户认证。查询缓存由原 plan_due/refresh_query_page 构建；管理和目录消费持久 outbox 原 payload，调用原 Worker body | 本地通过，非真实 provider |
| 6 接受回执与新观察分离 | Worker 后 GET 任务为 ACCEPTED + REFRESH_PENDING，详情仍是原状态；实际 directory staging/publication 后 Worker 回读任务为 ACCEPTED 观察、当前详情为目标，冻结原值不变 | 本地通过 |
| 7 失败/等待回读不冒充完成、不重写 | `test_native_failed_readback_retry_is_read_only`：SDK 读拒绝后观察不变、受理保留；HTTP refresh → 原读取租约恢复 → 目标观察；整个过程一条状态 POST | 本地通过；错误读取租约用合成时钟推进三分钟 |
| 8 同 key、并发/重复消息一写一回执 | 同 key HTTP 返回同 task；两个线程真实 PG 独立连接与 Redis 竞争同 Worker 消息，再重复投递；SDK POST=1、attempt=1、receipt=1 | 有界真实并发通过，不是无界证明 |
| 9 tenant/BC/连接/账户/actor/权限 | 相同平台账户/对象 ID 的第二租户目录保持原值，跨租户读拒绝；跨 BC 选择提交拒绝；Native grant/connection/actor/authorization revision 撤销四场景无 SDK 请求；账户/素材身份、冻结路由等复用通用回归 | 本地通过；未声称双浏览器跨租户全流程 |
| 10 普通/素材/直接受理保持 | 受影响既有管理、权限、路由、SDK batch 回归；未修改直接受理/准备 owner | 功能回归通过；原 SQL 数量阈值失败见下文，整组不得报全绿 |
| 11 针对错误实现的真探针 | 空执行、额外父级 SDK 写、双 SDK 写、错误 regular 路由、接受冒充观察、刷新覆盖原值 | 六种均 RED → finally/cmp → GREEN |
| 12 离线边界 | 所有平台流量均由严格 SDK transport 接管，返回合成 request_id；真实 PG/Redis 只使用 M5a 本地环境 | 无真实 TikTok / 广告写入 |

## 环境、初始失败与验证命令

所有测试从本集成根通过以下 guarded runner 执行。Python 3.14 复用现有安装，`.venv` / `node_modules` 原链接未改，未安装依赖。

初始专用数据库在 PG16，原目录 Worker 的 `SET LOCAL transaction_timeout` 不可用。这是环境错误，不改产品绕过。使用本机已安装 PG17，新建 `/tmp/tkada-native-series-20261112/m5a-implementation/pg17` 独占 loopback 临时端口，数据库继续使用 `tkada_m5a_*_test`。首次空库 SQL_ASCII 不适配驱动，重建该空测试库为 UTF8 后迁移到当前 head。原 PG16 与其它工作树未动。专用 Redis app DB0/test DB1 保持原分配，没有 FLUSHDB。私有 test-env.json 与外部备份均 0600，不入库、不打印。

其它初始失败属于夹具/宿主：缺 redirect URI、SDK 页大小与请求不符、没有驱动查询缓存 Worker、非数字 BC 被前端过滤、从只读目录详情寻找操作按钮、第二租户目录缺来源连接。纠正夹具/入口后验证，未以此扩大产品需求。调试期间曾让一个浏览器用例与一个 HTTP 套件重叠，违反串行约束；之后全部验证与探针串行，最终证据来自串行运行，未使用共享队列或其它业务库。

```sh
R=/tmp/tkada-native-series-20261112/m5a-implementation/run-test.py
python3 "$R" python -m pytest backend/tests/modules/ad_management/test_native_status.py -q --tb=short
# 18 passed / 71.05s：含六个真实工作区场景。
# 后续仅加厚 HTTP 第二租户同 ID、跨 BC、receipt/未来消息断言，独立复跑：
python3 "$R" python -m pytest backend/tests/modules/ad_management/test_native_status.py -k 'not workspace' -xq --tb=short
# 12 passed, 6 deselected / 27.33s；UI 场景及产品源码不因这些夹具变更而改变。

python3 "$R" python -m pytest backend/tests/modules/ad_management/test_direct_submission.py backend/tests/modules/ad_management/test_direct_concurrency.py backend/tests/modules/ad_management/test_execution.py backend/tests/modules/ad_management/test_management_permission.py backend/tests/modules/ad_management/test_api_actions.py backend/tests/integrations/tiktok/test_management_batches.py -k 'not test_direct_10748_submission_does_not_expand_directory' -q --tb=short
# 119 passed, 1 deselected / 8.46s；唯一排除项是已单独复现的 START 基线阈值失败。

python3 "$R" python /tmp/tkada-native-series-20261112/m5a-implementation/jb59-probes.py
# 最终源码/加厚夹具：六种均 RED、cmp 一致、GREEN。
python3 "$R" python -m ruff check backend/app/modules/ad_management/execution.py backend/tests/modules/ad_management/test_native_status.py
git diff --check
```

最初组合回归为 130 passed / 2 failed / 6 deselected：一项是编辑时遗漏 enqueue import，已修复并单独 1 passed / 0.61s；另一项是下述基线。加厚第二租户夹具时一轮 123 passed / 8 setup failures，修正来源约束及误用的 BC GET 查询参数后，上述最终 12 个 HTTP 场景通过。任务 GET 的 BC 来自保存的 task，不接受测试臆造的 BC 参数；跨 BC 拒绝现在从真实提交合同验证。

### 保留的基线失败

`test_direct_10748_submission_does_not_expand_directory` 的 SQL 数量断言 `len(statements) <= 25` 实得 26。树外备份候选 execution.py、替换为 START 版本、真跑同一检查仍 `26 <= 25` 失败，finally 恢复并 cmp 一致。详细日志 `/tmp/tkada-native-series-20261112/m5a-implementation/query-budget-baseline.log`。未修改旧测试阈值，未把它记为 GREEN，未将目录查询移回 intake；Root 需裁定该既有预算断言。本票不宣称整个受影响套件无失败。

### 错误实现探针

驱动和每个源码字节备份、RED/GREEN 日志在 `/tmp/tkada-native-series-20261112/m5a-implementation/jb59-probes/`，不提交。每次改变当前树的真实源码，运行同一真实 HTTP/PG/Redis/Worker 检查，再 finally 恢复外部备份、`cmp`、复跑 GREEN。

| 探针 | 拒绝原因 |
|---|---|
| empty | 任务未 ACCEPTED / 无外部状态效果 |
| unselected-parent | 额外官方 Campaign SDK 状态写违反选中层级/ID；所选项无法伪报接受 |
| duplicate-sdk | 物理 POST 次数由 1 变 2 |
| regular-route | 普通端点不能满足 Smart+ 请求与观察断言 |
| fake-observation | 未读回即 ACCEPTED，REFRESH_PENDING 断言失败 |
| overwrite-frozen | 目录更新后冻结原值被目标覆盖，原值断言失败 |

最终产品 owner Git blob：`f1868139cec9d74e3e5d3f4883129db933e7dbe2`。源码提交见 implementation-progress；探针只证明这些错误会被拒绝，不证明所有实现正确。

## 限制与 JB-60 消费合同

没有运行、借用或宣称 Native Build TLC 为管理启停证明。现有形式技能覆盖搭建、报表、导出或操作日志，不覆盖管理 physical request/attempt/receipt。此处复用真实 PG/Redis 双 Worker/重复消息、通用发送围栏检查并加源码探针；无新的管理形式模型或形式提交。若 Root 要推进管理形式覆盖，需要窄模型涵盖 task/item claim、Redis lease、发送前持久尝试、发送/回执丢失、重复消息、刷新与 UNKNOWN 的交错，并绑定 `execution.py`、`batch_execution.py`、`tasks.py` 及本票真实 transport 探针；目前不作无界并发或 liveness 承诺。

JB-60 从本候选继承 `native_status_env`、`install_wire`、`drive_management` / `drive_directory` 与 `@native-management-live`，继续使用本专用环境和已批准直接受理合同。增加同批 ACCEPTED/REJECTED/NO_CHANGE/发送后 UNKNOWN、只读核查四类观察、核查有界/晚到消息、原值恢复及当前值冲突；不得把 UNKNOWN 改成 ACCEPTED，不盲重放原写，不新增 Native executor/状态框架。失败读取的已领取目录需按既有租约恢复，不手工改最终状态。Root 拥有 JB-59 阶段验收、JB-60 启动、fresh Standards/Spec 全范围审查、本地集成；本轮不写 tracker、不启动 JB-60。

真实 TikTok、生产、部署、平台授权结果均 Unknown/未执行。没有 main 合并、fetch、rebase、push、PR、生产迁移或业务写操作。产品前端源码未改；浏览器已真实编译测试新增 TS，没有为测试文件变更重复执行全站 build。测试宿主与其页面/查询线程均在 finally 退出；本轮新建 PG17 在收口时停止，复跑使用本机 `/usr/local/opt/postgresql@17/bin/pg_ctl -D /tmp/tkada-native-series-20261112/m5a-implementation/pg17 -o "-h 127.0.0.1 -p <私有 test-env.json 中的端口>" -w start`，凭据仍由私有 runner 注入。

## Root 独立阶段验收

Root在精确候选5f4b01668431d09b2b8512622d6a74b7b1500a96审阅execution原值投影、发送后同路由观察判断、REFRESH_PENDING聚合与原时间桶唤醒。独立重启本任务拥有的loopback PG17（原共享PG16未动），复跑native_status非workspace子集：12 passed/31.35s，含真实HTTP/PGRedis/原Worker和目录发布。改动Python Ruff及diff check通过。六个浏览器场景、119既有回归和六种实际源码拒错继承候选的匹配源码证据，未冒称再跑。

SQL数量26/旧阈值25在START同输入已复现，保留为既有检查失败；本票未改变直接受理路径，不调整旧阈值，不声称相关所有检查全绿。管理域尚无专用形式模型，未借Build TLC作证明；当前接受证据限真实有界并发、重复消息与源码拒错。

结论：JB59本地阶段接受，放行fresh Sol/high JB60继续混合结果、UNKNOWN只读核查与恢复。两票整体仍待独立Standards/Spec审查和本地集成；不提前关闭票。PG17暂保持运行供下一票使用，仅在整个任务收尾时停止；原环境、其它报告工作树不动。未push/deploy/真实平台操作。
