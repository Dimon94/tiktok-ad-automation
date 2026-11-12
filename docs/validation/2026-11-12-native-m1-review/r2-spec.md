# Spec 独立审查 R2 / 3

**Spec verdict：PASS。未解决 P0=0 / P1=0 / P2=0 / P3=0。**

仅审 R1 修复 diff 及受影响行为；其他部分沿用 R1，不新挖 P2，不对 Standards 轴表态。R1 三个 P1 均关闭，原 TTL P2 也已修复。

## 固定范围与来源

工作树：`/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb5-native-series-m1`。

使用指定命令：`git diff 2dce3bc7d19f132209a6d3c6a8ba1bbd87619af2...d39f6bbe7f8886fccb942c4de9abc02345f4e9ec`。已程序化核对 `review-r2-bundle.json`：HEAD、repair fixed point、repair merge-base、原始 merge-base `da190b59d66bc08b1e67890e31a250af50a5a7d2`、有序 20 个 changed paths、唯一提交 `d39f6bbe fix(builds): repair native Series M1 review findings` 全部相符。没有另选基线；原 untracked `node_modules` 保留。

已读完整 `review-r1-spec.md`、canonical Spec 的目的地/策略、R01–R20、主要验收 Seam、素材、冻结、执行、恢复、回读和相关测试条款、六票 JB-7～12 的全部验收，以及 `docs/validation/2026-11-12-native-m1-review-r1-fixes.md`。按用户已定范围：既有通用策略、单账户已有 Series、直接视频、CBO Target ROAS；不新增 5+1 网格或剧目全局身份约束。官方字段路径以提供的本地快照核对，未调用真实平台。

## R1 逐项关闭

### P1-1：原生预览无法提交、恢复受理和返回任务 —— 已关闭

Spec 220 行：“主要验收入口只有一条：现有创建工作区的‘保存草稿 → 准备资产/素材 → 查看并冻结预览 → 提交 → 查看任务结果’用户流程。”42 行要求用户确认具体冻结预览后直接 ENABLE。

`frontend/src/features/builds/PreviewWorkspace.tsx:134` 保留共用 `usePreviewSubmission`，原生分支现位于正文（393 行），之后仍渲染共用 `PreviewSummaryBar`（489 行）。该 bar 的 FROZEN / route / 写权限 / 无既有受理结果门禁及提交动作（588～618 行）均可到达。共用 ledger、响应丢失查询（332～345 行）、服务端 submission_id/本页回执只读态（142～144 行）、任务导航（211～220 行）和退出围栏（323 行）不再被 early return 旁路。`NativeFrozenPreview` 中“执行尚未开放”已删除。

审读 `backend/tests/modules/builds/native_browser_http.py:20` 和 `frontend/tests/build-preview.spec.ts:1135` 起的 `@native-live`：生产 preview/submission 路由使用真实 session；浏览器请求转发到临时宿主，首个真实 202 已落库之后才丢浏览器响应，恢复与任务详情随后读取真实接口。`test_native_execution.py:97` 起串接该浏览器、真实库唯一 Submission、真实 expansion/execution，并通过独立回读进入 COMPLETED。这个 harness 支撑报告所述本地提交闭环，不能被当作平台真实接受。

### P1-2：独立封面/重复位置被旧围栏拒绝 —— 已关闭

Spec 389 行：“返回数量、顺序和 material ID 必须与选中集一致”；426 行：“显式复制的创意单元不按内容摘要误合并”；R06/R15 要求每条实际视频及封面来自冻结正证据。

`backend/app/modules/builds/cover_execution.py:156` 将原生输入送到原生围栏，Mini 分支继续保留。`native_execution.py:221` 先以提交操作者验证当前 unit route，再复用 `validate`；该 validate 依次核对 FROZEN/hash、当前 draft revision、完整 selection、route/授权、账户元数据、当前 checkpoint、逐位置映射、地区/CTA 与占用（19～131 行）。现有 `native_materials.read_native_materials` 的 scope/revision/route、目标账户、证据、VID/封面校验没有被旁路。最终 `creative_list` 按选中广告的顺序逐项精确比较 video_id、image_id、展示身份和格式（246～263 行），不再对 material_id 做 SQL 去重计数，也不把独立 C 与视频默认 D 比较。

两处实际围栏仍消费同一分支：`execution_state.arm_request` 和 `execution.py:1131` 的物理发送前复查。`execution.py:957` 的 MATERIAL 分支在 `_frozen` 已核实当前原生素材之后消费原映射，不再启动无关默认封面准备；`cover_execution.py:104` 的广告重试也仅接续默认封面位置，显式图片仍由 IMAGE owner 管理。

新增 `test_native_selected_cover_execution` 覆盖独立 C≠D、无默认 D、同视频不同封面位置；`test_native_final_material_fence` 覆盖撤权、错 route、变映射、实际 wire 换封面/丢位置、准入后撤权。审读后未见靠替换业务 readiness/提交结果使这些场景假绿。

### P1-3：实际禁用字段投影丢失，错误对象仍 MATCH/可认领 —— 已关闭

Spec 600 行：“Adgroup：……以及直接模式无分销链接。”512 行要求直接模式省略落地链接、分销、静态 CTA 与附加项；R18/R20 要求 UNKNOWN 唯一精确匹配和全部冻结事实核验。

官方快照 `create-an-upgraded-smart-ad-group.md:445`～446 的实际路径 `drama_selling_setting.distribution_link` 现由 `readback_compare.py:333` 保留；同类 top-level 直链、Mini、静态 CTA、附加项由 323～332 行保留至 `BuildRecord.observed_direct_fields`，没有用请求值补观察。`compare_record:438` 在一般缺字段分支之前对明确禁用观察返回 MISMATCH；`record_differences:535` 保留其精确路径。

该 record 及比较由共用 `reconciliation_scan.py:110`、128～136 行进入精确回读和 UNKNOWN 扫描，原始事实随 candidate 持久化；未形成另一套认领比较器。`native_completion.all_native_matched` 仍要求全部源对象与 READBACK 为 MATCH，实际差异不会在普通或内部聚合中被置 completed。新增真实库时间线分别覆盖 nested 分销、静态 CTA、附加项的 NEEDS_REVIEW，以及 UNKNOWN 的组分销/广告附加项不能认领。

本轮另独立执行 **15 项无 DB/Redis/网络探针**：对 nested 分销、静态 CTA、附加项、落地链接、旧 Mini ID，分别验证完整观察与缺 operation_status 时均 MISMATCH，实际值/差异路径保留；相同 oracle 仅缺状态而没有禁用观察时为 INCOMPLETE。全部通过。

### P2-1：TTL 到期不能同 revision 刷新 —— 已关闭

Spec R10（181 行）：“可恢复失败有界重试”；JB-10 要求“证据过期可按既有合同刷新，实际 Series、身份、地区或冻结意图变化必须停止并要求新预览”。

`native_execution.py:67` 把单纯 Scene/账户能力过期及 PENDING 引向 `scene_refresh_required`；`refreshing=True` 只允许依赖准备提前返回，正常发送仍要求 READY、素材一致、地区/CTA 一致和当前授权。`execution.py:760` 的刷新保留冻结 selection，交给原 SceneJob/CapabilityJob owner，等待受原 Scene TTL 上界约束（765～779 行）。`capabilities.py:239` 排除过期 COMPLETE 的复用，避免刷新只读回同一过期能力。

刷新不改 preview/config/digest，也不清空或重新构造已冻结 request_body。已有正文的 claim 继续使用既有安全重试规则；尚未 arm 的本地 claim 仍按原 owner 记录本地尝试，不将其误报为“所有本地 attempt 计数完全不变”。现有已建父级不进入重新 create。新增时间线实际断言父级 remote_id/body/body_digest/attempt/attempt_id 及 draft revision/content_digest 全不变，Scene/能力刷新后续跑，CTA 变化和身份撤权停止，等待超限 FAILED。没有发现把“刷新”用于改变冻结内容或绕过撤权的新增发送路径。

## 验证与缺口

本轮仅运行上述 15 项纯生产回读探针和只读 Git/bundle 核对，未运行共享 PG/Redis、浏览器或集成测试。实现者日志中 185 项回归、真实浏览器和 TTL 时间线通过属于已提供证据；本报告核对了对应测试真实接线与断言，不把它们表述为审查者本轮重跑。协调者独立集成验证与本报告分开记录。

浏览器壳的租户/BC 导航和认证来自 fixture；草稿保存、prepare、freeze 是同一测试中的真实后端前置，未由浏览器逐字段录入；浏览器在异步执行前已读取真实任务页，后续 completed 由同一测试的真实后台链验证。这是明确的浏览器覆盖边界，不是新的已证实产品 P1。

本修复范围未发现需要按两种可观察读法处理的新增 Spec 缺口。真实 OAuth、平台接受/审核/经营、active 全枚举、跨账户/BC覆盖及未证实身份组合仍是原有 Unknown；未做形式验证、真实广告、推送或部署。

**最终独立 Spec verdict：PASS；未解决 P0=0 / P1=0 / P2=0 / P3=0。**
