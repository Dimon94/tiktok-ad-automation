# Standards 审查 R1 / 3

**Verdict: BLOCK。P0=0，P1=2，P2=1，P3=0。** 仅评价 Standards 轴。

范围：`da190b59d66bc08b1e67890e31a250af50a5a7d2...2dce3bc7d19f132209a6d3c6a8ba1bbd87619af2`。已实际核验两 ref、HEAD、merge-base（等于 fixed point）、9 个提交及 bundle 的 80 路径完全一致。工作树仅原有 untracked `node_modules`。已读 AGENTS.md、CONTRIBUTING.md、development.md，及 pyproject/biome 的工具覆盖；未报告工具负责的格式问题。按既有集成树授权审查，不把模型/逐票工作树豁免列为违规。

## P1：所有原生冻结预览提前返回，网页没有提交入口

- **证据**：`frontend/src/features/builds/PreviewWorkspace.tsx:173-176` 对 `current.native_request` 直接返回 `NativeFrozenPreview`，跳过下方共用提交、未知提交恢复及已提交结果导航。`frontend/src/features/builds/NativeFrozenPreview.tsx:34-42` 的组件参数只有 preview/back/tenantId，没有提交回调；完整 JSX 只有返回草稿、目录跳转，`:149-150` 仍显示“原生广告执行尚未开放”。
- **触发与反例**：operator 正常保存原生草稿、准备到 READY、生成 FROZEN，进入现有 `/build-previews/{id}`。无论权限和素材如何齐全，唯一分支都渲染无提交动作的页面，不能调用已实现的 submission API。API 的执行验收通过不能补足这一网页主路径；通过 API 已提交后再打开同一预览也不会出现共用任务返回/恢复区域。
- **影响**：原生网页业务链在预览永久终止，用户不能从产品入口提交创建。
- **违反规范**：`AGENTS.md:18` 具体预览提交后创建 ENABLE 的产品合同；`:59-60` 复用现有功能、清理失效分支并评估调用方/接口影响。
- **基线**：fixed point 无 native 提前返回/独立组件，本次新增，不降级。已核对同类早返回，问题覆盖全部原生预览。

## P1：原生自选封面与重复视频未迁移最终素材校验，父级创建后广告必失败

- **证据**：`backend/app/modules/builds/native_materials.py:93-102` 为 `cover_material_id` 准备独立 IMAGE 映射；`native_preview.py:172-177` 明确保留重复视频的位置身份；`native_execution.py:197-206` 把这些位置及各自封面编译为 creative_list。实际发送仍经过 `execution.py:1092-1098` 的旧 `cover_execution.validate_ad_assets`。该函数 `cover_execution.py:212-215` 要求按 material_id IN 查询得到的唯一视频行数等于创意位置数，`:221-230` 又要求请求封面等于视频映射自己的默认 image_id。
- **触发与反例**：视频 V 已核实默认封面 D，用户给 V 选已核实图片 C（C≠D）；准备与冻结接受，Campaign/Adgroup 创建后，Ad 最终围栏把 C 当作变更拒绝。另一反例：同一个 V 在同一 Ad 明确选两次，冻结保留两个位置，而 IN 查询只得一行，数量条件必失败。都不能通过原样重试恢复。
- **影响**：合法原生计划在已创建 ENABLE 父对象后以 `material_refresh_required` 失败，无法完成广告创建；不是平台拒绝或素材实际失效。
- **违反规范**：`AGENTS.md:59-60` 复用机制须同步迁移调用方和数据合同、清理不适用旧分支；`:18` 具体预览的 ENABLE 创建合同。
- **基线**：旧校验函数在 fixed point 已存在，只支持默认封面；本 diff 新增原生调用及上述合法输入，属于新 caller 暴露，按 P1。
- **动态局部探针**：在当前锁定 venv 中执行真实 `validate_ad_assets`，仅替换只读查询输入和 PreviewContract；默认单位置输出 `ACCEPTED`，自选封面输出 `REJECTED material_refresh_required`，重复视频输出同错。未连接 PG/Redis、未执行业务请求；该探针只确认 guard 的反例，不冒称完整数据库/SDK 时间线。建议将已有 JB8 自选封面/重复来源 fixture 接到现有完整执行检查。

## P2：证据 TTL 到期后，原生冻结任务没有同 revision 的刷新接线

`native_execution.py:65-68` 将过期检查点统一变成不可重试的 `native_preview_not_ready`；`scene_jobs.py:862-864` 证实其来源。反例：已创建 Campaign 后队列等待超过 Scene TTL，下一步失败；重试不刷新。唯一网页重新准备经 `drafts.py:416-417` 增 revision，随后 `native_execution.py:29-33` 永久拒绝旧冻结计划。既有 `execution.py:1252-1253` 只接续 `scene_refresh_required`，没有原生刷新参数。影响为特定等待条件下恢复失败。违反 `AGENTS.md:59-60` 共用恢复机制/调用方迁移要求。基线无原生路径。

## 验证边界

- 已静态追踪目的地保存、租户/BC/route 与绑定代数、身份迁移、素材证据消费、冻结摘要、每次发送准入、回读唯一匹配、UNKNOWN 和取消/迟到回执；未发现其他有足够证据的 P0/P1。
- 复用六票 validation 文件里的动态证据及其明确基线失败/旧夹具边界，没有把测试通过当作无 bug 证明。未运行共享 PG/Redis、真实平台业务、迁移或浏览器；TTL 恢复为可达静态反例，尚未跑完整隔离库时间线。
- 无产品/测试/配置修改、无 tracker/平台写操作；唯一写入为本报告。目标 HEAD 复核仍为固定 tip。
