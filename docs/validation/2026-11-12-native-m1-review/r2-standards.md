# Standards 审查 R2 / 3

**Verdict: PASS。未解 P0=0，P1=0，P2=0，P3=0。** 本报告只评价 Standards 轴；未给出 Spec 轴结论。

## 固定范围与规范

- 修复比较：`git diff 2dce3bc7d19f132209a6d3c6a8ba1bbd87619af2...d39f6bbe7f8886fccb942c4de9abc02345f4e9ec`；原整体基线为 `da190b59d66bc08b1e67890e31a250af50a5a7d2`。
- 实际核验 bundle 的 fixed point、HEAD、merge-base（等于 repair base）、唯一修复提交及 **20 条精确路径**，全部一致。源树只有原有 untracked `node_modules`，审查结束 HEAD 未变。
- 已读 R1 Standards 报告、`docs/validation/2026-11-12-native-m1-review-r1-fixes.md`、AGENTS.md、CONTRIBUTING.md、development.md、backend/pyproject.toml 和 frontend/biome.json。沿用现有树、Sol 串行授权；不报告工具负责的格式问题。
- 按 R2 合同只审修复及其影响行为，其他沿用 R1，不另找 P2。

## R1 Finding 关闭状态

### P1-1 原生网页提交、未知受理恢复与任务导航：关闭

`frontend/src/features/builds/PreviewWorkspace.tsx:173` 不再为原生页面提前返回；原生正文现在位于共用正文分支（`:393`），其后必经 `PreviewSummaryBar`（`:489`）。共用 `usePreviewSubmission`（`:134`）、退出围栏（`:323`）、原请求恢复（`:332`）与已提交任务导航（`:211`）全部可达。`NativeFrozenPreview.tsx` 删除了执行未开放提示，未另建提交协议。

真实可达反例已消除：原生 FROZEN 预览能够沿同一提交按钮调用既有 hook；响应未知时仍以 sessionStorage 中原 request_id 回查；服务端 submission_id 或本页 receipt 会进入只读态并提供任务入口。与 `AGENTS.md:18` 的具体预览提交合同、`:59-60` 的共用机制迁移要求一致。

### P1-2 独立封面与重复视频被旧最终围栏拒绝：关闭

`backend/app/modules/builds/cover_execution.py:156` 将原生委托给原生校验，Mini 继续原路径。`native_execution.py:229-239` 先以原提交 actor 和当前 unit route 进入 `validate`；该函数仍核验冻结摘要、draft revision、selection、当前授权/账户元数据、scope 对应的 native checkpoint、素材映射和冻结地区/CTA（`:19-131`）。这不是直接绕过旧校验。

`native_execution.py:246-263` 逐位置比较**实际发送 body**的 creative_list，包含身份、VID、封面、顺序和数量。既有 `execution_state.py:174-177` 在 arm 同事务调用此围栏，`execution.py:1129-1137` 在物理发送回调再次检查。显式图片不要求默认视频封面；`execution.py:957-969` 的 MATERIAL 分支只在 `_frozen` 完成当前正证据检查后复用冻结映射，编译广告仍消费各位置的完整冻结内容。`cover_execution.py:104-117` 的广告重试仅接续默认封面位置，避免对显式图片触发无关视频封面任务。

局部执行探针调用真实 `validate_ad_assets`，仅替换 session 的只读行与 scope validator：同 VID 两个不同显式封面通过；实际换封面、删除位置、交换位置均以 `execution_intent_changed` 拒绝。scope 授权部分由静态链路核对，未将该局部探针表述为数据库或完整业务验收。

### P2-1 TTL 过期后无同 revision 刷新：关闭

`native_execution.py:67-78` 仅把纯过期原因或 PENDING 转成 `scene_refresh_required`；`refreshing=True` 只用于依赖准备（`execution.py:760`），正常发送仍要求 READY、materials_ready 和冻结意图相同。`execution.py:781-810` 把冻结 native_selection 传给既有 Scene owner，不修改 draft revision；`:765-779` 为等待设置 Scene TTL 上界。Scene 新证据改变身份资格、素材、地区或 CTA 时，下一次正常 `_frozen` 不能继续发送。

`scene_jobs.py:885-892` 区分过期正能力证据与明确不具资格；`accounts/capabilities.py:236-243` 排除过期 COMPLETE 复用，使既有 owner 能产生新的能力代次，保留 PENDING 复用与已有连接锁/唯一 pending basis。Mini 调用参数默认值与既有分支均保留；未发现修复引入的权限放宽或远端请求跨数据库事务。

## 共用 owner 的新增 P0/P1

**未发现有足够证据的新 P0/P1。** 已沿最终素材围栏、显式封面/重复位置、MATERIAL 复用与广告重试、过期证据恢复、当前授权与刷新后意图、arm/before_request 发送事务以及 Mini 调用边界检查。

回读投影修复影响亦已核对：新增 `BuildRecord.observed_direct_fields` 默认空字典；`readback_compare.py:320-337` 只为原生记录采集实际非空禁用字段，`:438-439` 在缺少其他字段时仍优先判已知 MISMATCH；Mini 默认行为不变，差异路径与观察事实共同保留。这是 Standards 的实际外部事实保存与共用合同影响检查，不代替 Spec 验收。

## 验证与 Unknown

- 本审查实际运行了无数据库纯函数探针：原生组/广告独立响应 oracle 的正常 MATCH；嵌套分销链接、静态 CTA、交互附加项三种实际字段均 MISMATCH；上述四种原生 wire 正负向检查通过。
- 首次 wire 探针因直接导入次序遇到 ORM 循环导入；按应用正常顺序先载入 `app.models` 后重跑通过。未修改源文件，也未把这一导入宿主限制记为产品 finding。
- 未创建 SQL engine/session，未连接 PostgreSQL/Redis，未调用 SDK/HTTP/浏览器、未执行真实广告或授权操作。锁序、发送前新鲜授权和数据库并发仅静态审查；真实库/浏览器完整时间线由协调者独立验收，本报告不声称已复跑。
- 已阅读实现者修复验收及新增测试断言；其真实库、浏览器、185 项回归、构建结果属于提供的证据，本审查不冒认独立执行。保留 R1 历史 Mini 夹具与 UI 动态断言的基线边界，未据此制造新 P1。
- 已做一次针对 JB-5/native Series 的 Nowledge 检索，无直接相关结果；本结论以固定源树和本次探针为依据。
- 全程未改产品、测试、配置或 Linear；唯一写入为本报告。

**Standards：PASS；P0=0 / P1=0 / P2=0 / P3=0。** 本轴 R1 三项均关闭；不因验证缺口改变 verdict。
