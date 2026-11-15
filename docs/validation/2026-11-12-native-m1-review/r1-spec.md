# Spec 独立审查 R1 / 3

**Spec verdict：BLOCK。P0=0，P1=3，P2=1，P3=0。**

范围为完整 M1 diff，只审 Spec 轴。HEAD `2dce3bc7d19f132209a6d3c6a8ba1bbd87619af2`；fixed point / merge-base 均为 `da190b59d66bc08b1e67890e31a250af50a5a7d2`。实际核验 HEAD、两 ref、merge-base、9 个提交、80 条 changed paths；路径顺序与 `review-r1-bundle.json` 完全相同。采用指定三点 diff，没有另选基线。

已读取完整 Canonical Spec、ticket-source.json 中 JB-7～12 正文及验收、六份 validation 记录、官方 SOP 基线、身份/ROI2 研究报告和对应官方字段快照。未修改源码、测试、配置、平台或 Linear，未运行共享 PostgreSQL/Redis 测试。原有 untracked node_modules 保留。

## P1-1：原生冻结预览绕开提交确认，真实 UI 主路径无法提交

- 位置：`frontend/src/features/builds/PreviewWorkspace.tsx:173`～175；`frontend/src/features/builds/NativeFrozenPreview.tsx:39`～154，特别是149～150。
- Spec 原句：220行，“主要验收入口只有一条：现有创建工作区的‘保存草稿 → 准备资产/素材 → 查看并冻结预览 → 提交 → 查看任务结果’用户流程。”42行，“用户确认具体冻结预览后，沿既有 creation_status=ENABLE 合同直接创建”。JB-10 负责把冻结原生预览接入同一提交入口。
- 反例：投手按 UI 保存合法原生草稿、准备到 READY、生成 FROZEN 预览。`current.native_request` 为真，父组件立即返回 `NativeFrozenPreview`，不再渲染下方已有确认提交区。子组件没有 submit callback、提交按钮、受理结果恢复入口或任务详情链接，尾部仍显示“原生广告执行尚未开放”。所有原生预览都会命中该分支；后端提交 API 已实现不能补上用户入口。
- 可观察影响：指定的完整用户闭环停在预览，用户不能创建任何原生广告；API 层的正常创建演示没有证明真实 UI 完成此 seam。
- 基线：fixed point 没有该 native early-return，预览沿统一提交组件；属于本次新增阻断。
- 同类检查：核对了新建/编辑草稿、准备页、原生预览和结果页接线。取消按钮已接结果 API；缺口集中在原生预览的提交/受理处理被整体旁路。

## P1-2：原生自选封面与重复位置在旧发送围栏必然被拒

- 新接线位置：`backend/app/modules/builds/native_execution.py:197`～206；`native_preview.py:172`～177；`native_materials.py:93`～101。
- 实际失败点：`backend/app/modules/builds/execution_state.py:173`～177，以及 `execution.py:1088`～1094 无条件进入旧 `cover_execution.validate_ad_assets`；该函数 `cover_execution.py:212`～236 只按视频的 AccountMaterial 行数及其 image_id 校验。
- Spec 原句：41行要求“视频、逐视频封面”；389行要求“返回数量、顺序和 material ID 必须与选中集一致”；426行“显式复制的创意单元不按内容摘要误合并”。JB-8 明确支持可选独立 `cover_material_id`、重复位置和同N同顺序，JB-10要求精确执行冻结视频/封面。
- 反例 A：选择视频 V 和独立封面图片 C，二者在目标账户均已核实；V 映射默认封面为 C0，独立 C 的 image_id 为 C1。JB-8 正确读取 C1，JB-9 冻结 C1，原生编译也发送 C1；最终旧围栏却比较 `V.AccountMaterial.image_id == C1`，即 C0==C1，恒失败。即使父级 Campaign/Adgroup 已成功，也无法发送 Ad。执行 MATERIAL 分支 `execution.py:940`～952 还会额外准备视频默认封面，不能替代用户冻结的独立 C。
- 反例 B：同一视频显式选择两个位置，SHARED 到同一个 API Ad。新预览为两位置生成不同 unit_id 并冻结两条 creative；旧围栏 SQL 的 material_id IN 只返回一份账户映射，`len(rows) != len(material_ids)`，仍拒绝。两个输入都可沿新准备/冻结路径走通，无平台未知条件参与。
- 只读探针：执行真实 `validate_ad_assets`，仅用内存 session 行替代 DB、固定已核实映射（没有网络/DB调用）。上述 A/B 分别输出 `INDEPENDENT COVER FINAL FENCE DomainError material_refresh_required`、`EXPLICIT REPEATED VIDEO FINAL FENCE DomainError material_refresh_required`。这证明最终围栏的确定性失败，不冒称已跑完整 DB 时间线。
- 基线：旧围栏在 fixed point 已存在，但旧输入没有本次独立封面/显式重复位置合同；本 diff 新增入口与冻结内容让这些新支持输入到达它，按新增暴露定 P1。
- 同类检查：首次 arm、每次物理发送前、已冻结请求重试都保留旧围栏；MATERIAL 依赖和 retry 路径也未消费原生封面选择。两例作为同一接线根因报告，不能只修首次编译。

## P1-3：实际嵌套分销链接被回读投影丢弃，错误目的地仍可 MATCH

- 位置：`backend/app/modules/builds/readback_compare.py:294`～309；完成消费点 `backend/app/modules/builds/native_completion.py:37`～47。
- Spec 原句：600行，“Adgroup：Series ID、Series 身份、地区、版位、排期、VALUE / VO_MIN_ROAS / ZERO_DAY / NO_BID，以及直接模式无分销链接。”43行要求全部回读一致，否则不是完成。R20（191行）要求核对全部冻结事实。
- 反例：对合法直接原生 Adgroup 的完整实际读回，保留所有冻结字段，再加入 `drama_selling_setting={"type":"DISTRIBUTION","distribution_link":"https://example.test/distribution"}`。官方字段快照 `create-an-upgraded-smart-ad-group.md:445`～446 明确该字段为嵌套结构；实现却只保留不存在于该结构的顶层 `distribution_link`，`_project` 丢弃真实嵌套字段。
- 只读复现：加载独立 `observed_group()` oracle，加入非空 `drama_selling_setting.distribution_link`，执行生产 `parse_page → compare_record`，输出 `NATIVE ADGROUP drama_selling_setting MATCH`；相同内容放顶层则为 INCOMPLETE。这不是平台语义猜测，而是已观察到禁止字段时比较器丢失证据。三级其他字段全 MATCH 时聚合只看此 MATCH，不会再检查被丢弃事实，能够错误标为 completed；UNKNOWN 唯一认领也共用该比较器。
- 基线：fixed point 无 NativeAdGroupCreate / 原生观察合同；旧通用投影虽已有，本 diff 新增原生回读入口与完成保证，属于新增暴露。
- 同类检查：核对原生组预算、地区/父级/排期、Mini/直链字段及 Ad 创意。组预算等已显式保留；同类 Ad 的 `call_to_action_list` / `interactive_add_on_list` 也会被投影丢弃，探针均得到 MATCH。主 finding 以 Spec 明确禁止的嵌套分销目的地为准，修复须覆盖真实 nested 路径并复用到精确回读与 UNKNOWN 恢复。

## P2-1：仅证据过期也无法按原提交刷新恢复

`native_execution.py:66`～68 把过期检查点统一抛成非 retryable 的 native_preview_not_ready；`execution.py:1252` 只对 scene_refresh_required 安排刷新，且旧刷新函数没有 native_selection。排队超过 TTL 后，未变的冻结计划永久失败；重新 prepare 会升 draft revision，又使原预览失效。Spec R10（181行）要求可恢复失败有界重试，JB-10 允许刷新过期证据；Mini 原路径有对应刷新合同。本次新增原生分支引入。应刷新同 scope 证据，只有冻结意图变化才要求新预览。

## 验证与 Spec 缺口

未跑完整真实 UI＋隔离 DB 提交时间线；本轮只读探针不替代该验收。复用票据中的已通过证据与已复现基线失败，没有将旧夹具失败判为新增 P1。首次探针因落地链接负例触发 ValidationError 而提前退出，另一次直接导入触发模块循环；最终按 app.models 初始化后，封面两例均获得上列结果。无源码变更。

未发现需要以双重措辞解释而代替 finding 的新增 Spec 缺口。真实平台接受、active 完整映射、跨账户/BC占用及不同身份组合支持仍为原 Spec Unknown，不作为本轮 finding；未运行形式验证。

**最终 Spec verdict：BLOCK；P0=0 / P1=3 / P2=1 / P3=0。**
