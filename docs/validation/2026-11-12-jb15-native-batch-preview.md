# JB-15 原生批量预览与冻结

## 范围与起点

唯一树 `projects/tiktok-ad-automation.wt/feat-jb13-native-series-m2`，分支 `feat/jb13-native-series-m2`，起点 `9066dc2274d7cb29e1ed66b27ba864679eaa828c`。原 `.venv` / `node_modules` 未跟踪软链保留。Astra/high 单实现者，无下级代理、分支切换、远端或真实业务写。Nowledge 定向搜索没有相关命中；以本树 canonical Spec、完整 JB-15 票及 JB-14 运行证据为依据。

## 实现与 JB-16 消费合同

- `native_preview.expand_plan` 从 M1 提取，继续调用原 `plan_structure`、资格、素材、CTA、目录观察 owner。批量 seed 为 draft/revision/plan_key；位置素材身份也包含计划键，计划顺序不影响该计划分配。
- `conflicting_plan_keys` 仅判断同账户、同精确 Series、地区明确相交；所有受影响项排除。当前目录 ACTIVE 只排除对应计划；PARTIAL/STALE/UNKNOWN 与跨账户/BC 范围限制随 manifest 保留，不增加全局禁投。
- 新批次 `BuildPreview.config.native_batch` 的 revision 为 `native-series-batch-v1`，含 draft_revision、冻结 route、共享 strategy、Decimal 字符串 daily_budget_sum 和有序 plans。每项为判别联合：共用 plan_key、selection、readiness、occupancy、reason_codes；INCLUDED 另外保存 unit_id、request_digest；EXCLUDED 不允许 Unit、摘要或执行请求。
- 完整 `FrozenRequestRevision` 唯一持久化在 INCLUDED `BuildUnit.scene_snapshot.native_request`；同快照保存 plan_key。继续生成既有 PlannedGroup、PlannedAd 和逐创意素材快照。整批摘要绑定有序 manifest，manifest 通过 request_digest 绑定完整请求。`read_batch` 校验整批摘要、Unit 集合、计划键、selection 和请求摘要，再为 HTTP 展示派生 `plans[].request`，不持久化第二份批次完整请求。
- 冻结在 Workspace 原事务内使用 savepoint；证据复读及请求/摘要校验失败时无半套预览或 Unit。无 INCLUDED 时不生成成功预览，返回逐计划排除码。输入修改继续由原 revision trigger 标为 OBSOLETE。
- `native_batch_execution_pending` 明确拒绝新批次提交，UI 按相同 revision 禁用提交，不生成 Submission。JB-16 须从同一 manifest/Unit 消费批准范围，迁移执行侧单项适配后解除此限制；不能仅去掉入口拒绝。
- 历史 M1 native_request payload 和摘要读取不改写，原单项预览/执行机制继续保留，后续统一清理归 JB-18。
- 新向前迁移 `native_batch_preview`（父 `native_preview_freeze`）扩展现有无版权方引用约束识别 manifest；不改写旧迁移，无表结构增加。集成/部署需要应用该迁移，未部署。

## 真实检查与失败记录

所有 Python 命令在本树 backend，前缀固定为：

```sh
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py
```

使用指定 Python 3.14 环境、独立 M2 PG/Redis。没有读取 `.env`、打印私有测试配置、清空共享 Redis 或修改共享数据库。Workspace HTTP 使用正式 router、授权和持久事务；prepare 由真实 continuation/Scene/素材 owner 推进，外部调用仅官方 SDK transport double。合成账户目录和已上传映射是输入，不是伪造 READY；未启动外部 Celery 消费进程。

开始前实际运行：

```sh
<runner> python -m pytest tests/modules/builds/test_native_plan_set.py -k independent_preparation -q --tb=short
<runner> python -m pytest tests/modules/builds/test_native_preview.py -k allocations_and_obsolete -q --tb=short
```

分别 1 passed / 5 deselected / 7.95s 与 3 passed / 13 deselected / 12.71s，证明真实混合 READY/BLOCKED、同 revision 回执保留和旧预览 OBSOLETE。JB-14 双授权、单项 TTL、旧 dispatch、跨账户/tenant/route 及双 PGsession 反序共享素材证据沿用 `9066dc` 的报告；相关 owner 未变。

红：新增双账户完整冻结检查在旧 `native_batch_preview_pending` 失败（7.22s）。实现后真实 PG 因 `ordinary preview requires provider references` 拒绝快照，补向前迁移后同检查 1 passed / 18.40s。M1 单项同时定向通过。HTTP 金额最初误期待固定字符串小数位，改为 Decimal 比较：PG Numeric 返回 `200.500000000000`，合同金额仍为 `200.50`；未改业务金额规则。

浏览器最初使用非数字测试 BC，页面按既有输入合同要求选择有效 BC；改用现有 numeric BC 夹具。之后错误地把两个基础广告当成同编号的四个文案版本，修正断言为基础广告 1/2 共四项。不是修改展开器迎合断言。另全排除 HTTP 错误正文原先被通用错误处理器隐藏，新增限定的 `NativeBatchUnavailable`，只向用户显示应用生成的计划键和排除码。

聚焦运行：

```sh
<runner> python -m pytest tests/modules/builds/test_native_batch_preview.py tests/modules/builds/test_native_plan_set.py -k 'not http_two and not scope_and and not browser_plan and not concurrent_retry and not plan_set_preserves' -q --tb=short
```

9 passed / 5 deselected / 54.88s，日志 `/tmp/tkada-native-series-20261112/m2-implementation/jb15-focused.log`。包含正式 HTTP freeze/readback、双账户完整独立 Unit、总预算 200.50、混合 INCLUDED/EXCLUDED/作废、批内矩阵、稳定分配、ACTIVE/UNKNOWN、全冲突拒绝以及 PG 故障回滚。PG 故障通过临时 BEFORE INSERT trigger 将 Unit 请求预算改成 999，真实摘要读回拒绝，断言预览与 Unit 均为空；未 mock preview service。

真实浏览器使用现有 `native_browser_http` 测试宿主和 Playwright，正式预览 API 返回真实 PG 冻结内容；应用壳/登录仍沿用现有测试夹具。验证账户、精确 Series、四项广告、总预算、禁用提交，并保存完整截图 `/tmp/tkada-native-series-20261112/m2-implementation/jb15-preview.png`。字体共享软链导致 Vite 字体资源 allow-list 警告，页面功能通过，未修复共享软链。

近似错误探针：

```sh
<runner> python /tmp/tkada-native-series-20261112/m2-implementation/jb15-probes.py
```

漏排冲突右侧、总预算乘 2、摘要忽略 manifest 三项均 exit 1。日志 `jb15-probe-{conflicts,budget,digest}.log`，外部 original 备份及 finally 恢复、字节比较和 `cmp` 全部成功。探针用于证明保护有效，不是平台验收。

前端 `bun run build` 实际通过（TypeScript + Vite 正式及 demo 构建）。正式源码无直接 HTTP TikTok gateway、SDK pin/策略参数不变。

## 最终复验与边界

恢复源码后的命令：

```sh
<runner> python -m pytest tests/modules/builds/test_native_batch_preview.py -k 'not browser' tests/modules/builds/test_native_preview.py -q --tb=short
<runner> python -m pytest tests/modules/builds/test_native_batch_preview.py -k 'partial_coverage or budget_pure or two_prepared' 'tests/modules/builds/test_native_execution.py::test_native_six_creates[None]' -q --tb=short
<runner> python -m pytest tests/modules/builds/test_native_batch_preview.py 'tests/modules/builds/test_native_execution.py::test_native_six_creates[None]' -k 'two_prepared or concurrent_batch or native_six' -q --tb=short
```

第一轮 23 passed / 3 failed / 1 deselected / 156.28s。三项失败原因是新用例采用 `100.100000000001`，被既有 USD Campaign `0.01` 精度资格约束拒绝；没有弱化业务规则。调整真实 API 用例为合法 `100.10`，两项精确合计 `200.20`；纯函数 `native_batch_budget` 另验证 `100.100000000001 + 100.200000000002 = 200.300000000003` 且不计广告数。第二轮 5 passed / 8 deselected / 26.49s，覆盖 PARTIAL/STALE/UNKNOWN；该命令的 `-k` 未选中 M1 执行，不能用它声称执行已通过。

新增 Unit 读取断言真实 red：每 Unit 读取到整批预算（`jb15-unit-red.log`，1 failed / 9.70s）。修复 `get_preview_units` 与 `load_frozen_unit` 从各 Unit 请求取 Campaign 预算和完整请求后，第三轮 3 passed / 11 deselected / 27.71s（`jb15-unit-final.log`），含双账户 Unit 逐项预算、两个 PG session 并发冻结只产生一个预览，以及 M1 `test_native_six_creates[None]` 的真实创建/回读链。并发检查还对身份、地区、素材、策略、广告、预算和覆盖限制变异，逐项请求摘要均变化。

构建二次通过，日志 `jb15-build.log`；Ruff check、format --check 及 git diff --check 通过。仅修受影响读取，没有重复未变化整仓测试。

单项执行兼容：旧规范化 LEGACY_PLAN_KEY 保留 M1 原 seed/完整请求/摘要算法；新显式 UUID 单项采用键派生 seed，但暂存原单项冻结形状以维持既有提交可用，JB-16 迁移执行后可统一到 manifest，JB-18 清理。预览侧不再调用 `single_native_selection`。没有真实平台创建、生产、部署或形式证明；形式模型属于 JB-16。已验证顺序交换的确定性及真实双 session 并发冻结；多计划提交/执行交错仍属 JB-16。新迁移及跨 owner 消费合同需要 root 审查；不将本报告当成 Linear Done。

最后一轮：

```sh
<runner> python /tmp/tkada-native-series-20261112/m2-implementation/jb15-float-probe.py
<runner> python -m pytest tests/modules/builds/test_native_batch_preview.py -k 'browser or budget_pure' -q --tb=short
```

float 近似错误实际 exit 1，源码备份/恢复/cmp 通过；恢复后 2 passed / 11 deselected / 12.26s（`jb15-browser-final.log`）。浏览器本轮已升级为两项 INCLUDED 加一项未授权 EXCLUDED；真实 PATCH/prepare/freeze 后验证全部三个选择、排除原因、四项广告、独立 VID/封面、总额和禁用提交。截图同路径更新。未新增服务 mock；壳/认证测试边界同前。

实施结果为可交 root 审查的本地候选，未改 Linear。关键已覆盖路径没有未修复失败；原始失败和修复后证据均保留。未声称每个范围/地区层级/跨 BC/崩溃时序穷举完成；当前 ACTIVE 状态全集、地区层级、跨 BC 唯一性仍 Unknown，原观察限制不变。
