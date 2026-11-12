# JB-8 原生直接视频素材就绪

范围：JB-5 的 JB-8；起点 `5a3cb1b6e8ca56e96765ad66c679aad4918a3988`，唯一写树 `projects/tiktok-ad-automation.wt/feat-jb5-native-series-m1`、分支 `feat/jb5-native-series-m1`。本节所在提交为交付 tip。用户授权复用集成树；没有新建/切换 checkout、派下级代理、更新 Linear、推送、PR、部署或调用真实平台业务接口。

## 用户可见行为与 owner

原生工作区沿已有 prepare/continue，在 JB-7 当前目的地/双身份/地区资格 READY 后，逐条准备选择中的视频及可选图片封面。默认封面沿现有 covers owner 的实际上传来源准备/共享；没有实际来源时明确阻断，不把任意目标副本当成上传来源。显式封面沿现有 IMAGE distribution，核实目标账户的图片资源。上传位置不用于关联业务剧目。

素材每轮最多20项，先按素材 ID 锁定当前片，再调用既有 distribution / covers；结果仍按原选择顺序保存，不去重、排序或补入未选素材。同一素材重复用于多个位置复用目标映射及持久任务。结构策略 SHARED / SEQUENTIAL_AVERAGE、基础广告×creative_count 逻辑没有改动。

`DraftScenePreparation.native_materials` 保存当前准备任务的 revision、scope basis、有序来源、VID、封面 image_id、映射 evidence IDs、视频摘要及原任务 IDs。旧任务继续服从草稿→准备锁序和 revision fence；每次只读消费再次核对当前 scope、route、映射归属、视频/封面、摘要及封面任务正证据。任一项失败，`materials=[]`，`materials_ready=false`，具体条目及原因见 `material_issues`。不返回可被误当作完整组的剩余部分。

目标账户既有已核实映射可沿现有素材合同复用；映射上的历史上传连接不是当前计划 route 的替代品。当前消费及任务仍核验冻结连接和授权代数；显式图片封面还核验其完整 frozen route。没有新增资格服务、上传器或封面基础设施。

## JB-9 消费合同

既有 `GET /build-drafts/{draft_id}/native-checkpoint` 增加：

- `materials_ready`：只有目的地 state=READY、非空且同N同序的全部素材证据通过时为 true。
- `materials_state`：READY / PENDING / BLOCKED / UNVERIFIED，与目的地 `state` 分开。`state=READY` 本身仍只证明目的地检查点。
- `materials`：有序 `NativeMaterialMapping`，含 position、material_id、可选 cover_material_id、实际目标 video_id、image_id、video_evidence_id、cover_evidence_id、preparation_id、可选 video_task_id / cover_task_id。复用已存在的资产可没有新任务 ID；目标 AccountMaterial ID 和 verified_at 是既有正证据。
- `material_issues`：定位 position、material_id、reason_code；scope 或数量错误为整组原因。

JB-9 使用真实草稿的 `native_checkpoint(session, context, draft, route)` 或该草稿 HTTP 检查点取得素材证据；仅带原生选择而无 draft.id 的 SceneContext 目录投影仍不包含草稿素材就绪。JB-9 必须同时检查当前 revision、scope、route、目的地 state 与 materials_ready，不以草稿 READY 的历史缓存代替当前检查点。不在本票生成/冻结预览、创建广告、回读广告或实现通用恢复/取消。

## 本地验收方法

全部 Python 命令从本树 backend 运行，前缀：

```sh
python3 /tmp/tkada-native-series-20261112/implementation/run-test.py python -m pytest
```

runner 使用固定隔离 venv、专用真实 PostgreSQL/Redis 和当前源码；私有配置不输出。平台替身在官方 SDK urllib3 传输处。来源/资产初始库存为隔离测试夹具；新封面核实与视频 distribution 续接实际运行业务 owner、DB 状态机、Redis admission 和 SDK adapter。未 mock 整个业务 service。

独立演示命令：

```sh
# 已准备目的地→2条完整目标素材同序就绪；无选择仍阻断
python3 /tmp/tkada-native-series-20261112/implementation/run-test.py python -m pytest tests/modules/builds/scene_jobs/test_native_series.py -k workspace_prepare -q
# 完整准备后缺第二条VID/封面或UNKNOWN，整组无素材输出
python3 /tmp/tkada-native-series-20261112/implementation/run-test.py python -m pytest tests/modules/builds/scene_jobs/test_native_series.py -k 'material_group and (missing_video or missing_cover or unknown)' -q
```

关键行为检查还覆盖：真正错账户映射、回执顺序颠倒、重复条目、条目数量不齐、缺证据、scope改变、视频内容摘要变化、旧revision回执；显式图片封面及同素材重复位置；21条跨有界轮次不截断；并发/重复准备复用封面任务；真实视频分发核实后保留原任务ID；封面任务UNKNOWN使原READY证据不可消费。

### TDD 与近似错误

- 新工作区正向检查对起点实际首红：`1 failed / 1 passed`，旧实现返回 BLOCKED 而预期 READY；`jb8-red.log`。接线后同检查 `2 passed`，`jb8-green.log`。
- 额外损坏/并发/续接检查在首片实现后补充，发现并修正 JSON 嵌套原地修改导致续接结果不落库的问题；最终采用独立副本赋值。没有声称所有分支均逐条实现前首红。
- 真跑近似错误：临时去掉 scope basis 比较，`material_group[scope]` 因把旧scope判为可消费而失败；`jb8-probe.log`。随后恢复原文件并字节比较一致。最终专项重跑使用恢复源码。

所有日志在 `/tmp/tkada-native-series-20261112/implementation/`；无生产凭据。

## Schema 与限制

新增 additive Alembic `native_material_readiness`，父为 `native_series_identity`，只为 DraftScenePreparation 增加 nullable JSONB。隔离库实际 upgrade head；不 downgrade 当前库。存在素材证据时降级明确拒绝丢失事实。生产迁移/发布方案待整个 M1 审查，没有生产迁移验收或恢复演练声明。

没有 UI 交互变化；更新了 OpenAPI 客户端素材合同并执行 TypeScript/Vite 生产及 demo 构建。未运行浏览器自验或 preview 工具。本票复用 JB-7 已通过且未改变的身份/地区资格证据，不重复真实平台读取。

真实平台上传/视频或封面接受性、广告创建、真实身份组合经营资格均 Unknown；没有运行形式验证器。已知旧预览总数上限断言基线失败不在本票范围，未重跑归因或修改组合。独立审查、后续 JB-9～12 及整个 M1 集成验收仍待完成。

## 最终运行结果

| 检查 | 实际结果 |
| --- | --- |
| `tests/modules/builds/scene_jobs/test_native_series.py tests/modules/builds/test_native_series_selection.py -q --tb=short` | **30 passed / 59.70s**，`jb8-final-ambiguity.log`，含探针恢复、21项分页、显式封面、视频/封面SDK续接、并发及多连接封面多义 |
| `tests/modules/materials/test_distribution.py tests/modules/materials/test_distribution_concurrency.py tests/modules/materials/test_covers.py tests/modules/materials/test_cover_fast_path.py tests/modules/materials/test_source_uploads.py tests/modules/materials/test_frozen_material_routes.py tests/modules/builds/scene_jobs/test_draft_bootstrap.py -q --tb=short` | **176 passed / 210.23s**，`jb8-regression.log`，真实PG/Redis，API/MCP传输替身 |
| `bun run generate-client`（frontend） | 通过，`jb8-client.log`；仅生成 NativeCheckpoint 增量及 NativeMaterialMapping 类型 |
| `bun run build`（frontend） | TypeScript、Vite production及demo通过，`jb8-build.log` |
| 改动Python `ruff check` / `ruff format`、`git diff --check` | 通过 |

前置复用：协调者已在起点独立执行 JB-7 `test_workspace_prepare_drives_native_job_without_provider`，1 passed / 3.65s，并读资格源码与SDK证据。本票修改同一个检查加入素材正向路径，最终专项已重新覆盖该入口；没有将早期 blanket-UNVERIFIED 叙述作为当前结论。


收尾补充：同一个目标视频存在两个不同连接的 current 封面任务时，即使图片字段已存在，只读消费与prepare均明确返回 `native_cover_mapping_ambiguous`，整组BLOCKED。此边界使用真实第二授权连接和真实封面任务行验证，补充到最终原生专项；没有制造第二份上传或资格owner。
