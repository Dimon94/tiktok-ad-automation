# JB-14 原生计划集与独立准备

起点 `4e89c407257b68f44923eb95176fbb71f40df6ae`，main 基线 `b185736753df3a0f6992d9494867ae877eb1b089`。仅指定 `feat/jb13-native-series-m2` 产品树；无 push、PR、部署、Linear 修改或真实平台调用。SDK pin、依赖及部署配置未改变。

## 实现与接口

- `scene_selection` 新写入唯一形状为 `NativeSeriesPlanSet`；`plans` 内 UUID 键唯一且非空。旧平铺输入/持久化 JSON 在读取边界成为固定键的单项，不回填数据库，不改历史冻结 payload。
- 工作区新增、复制、删除、选中计划批量修改地区；每项明确账户、资产、双身份、有序视频和可选封面。复制生成新键；显示批量编辑影响范围，未选项保持原值。新计划 HTTP 输入必须包含精确选择。
- 20 计划为当前任务扇出资源预算，每项沿用最多 1000 来源、每轮 20 素材的 owner；不是运营默认或平台商业上限。
- Workspace/Readiness 使用计划集，素材回执保存在既有 `DraftScenePreparation.native_materials.plans[plan_key]`；逐项复用 SceneJob、distribution、covers，未建立新的上传或资格 owner。
- `GET native-checkpoint?view=plans` 返回 `NativeSeriesBatchCheckpoint`；READY 项携 checkpoint，其他项携 selection 与 reason_codes。原 `native-checkpoint` 增加可选 `plan_key`，缺省仅允许单项。
- 同修订重试复用 preparation，递增 dispatch generation，保留有效证据/游标；输入修改仍走原 revision/obsolete 机制。先完成账户/场景检查，再全批按素材 ID 取锁。
- Preview/Execution 暂通过 `single_native_selection` 消费单项；多项返回 `native_batch_preview_pending`，页面说明批量冻结尚未开放。JB-15/16 迁移消费者，JB-18 移除此适配。没有 `is_batch`、`batch_ready` 或第二可变选择。
- 无 DDL 迁移：使用已有 JSONB 列做 expand 形状，保留旧材料回执读取。没有 downgrade 或历史冻结重写。

## 验证入口与证据

所有 Python 命令从本树 backend 经 `/tmp/tkada-native-series-20261112/m2-implementation/run-test.py`，使用私有 M2 环境、锁定 Python 3.14 依赖和隔离 PG/Redis。未打印私有环境。前端使用既有 Bun。

- 红：新增 `test_plan_set_preserves_explicit_mapping_and_normalizes_legacy` 在旧实现失败 `KeyError: plans`；随后同测试绿。早期一次错误 cwd 导致找不到测试，不计红。
- 初始回归：该测试 + `test_prepared_workspace_freezes_native_plan`：2 passed / 6.78s。
- 混合独立准备、同修订证据保留、旧 revision 与原 M1 浏览器提交：3 passed / 47 deselected / 55.99s。浏览器后继续真实 SDK transport 创建/回读 MATCH；不替换业务 owner。
- 真实计划编辑发现 PATCH 错发 `media_kind` 导致 422，已修复；测试仍通过真实 HTTP 保存。浏览器 BC fixture 改用 M1 的数字 BC；在途 route.fetch 清理属于测试收尾。
- 实际前端正式与 demo 构建曾通过；最终源码检查结果追加下方。
- 错用宿主 Python 3.11 的 compileall 因项目 Python 3.14 语法失败；改用固定 runner 的 compileall 通过。未改项目语法迎合宿主。

## 所有权与接续

本票实现 T1/T2/T5/T6、R1/R2，以及 R4 资格与 R12 准备范围/旧修订部分；T3/T4 的准备结果可观察，但多计划冻结准入由 JB-15 接续。占用冲突、完整冻结、执行、恢复、取消和形式模型分别归后续票。不得把本票当作完整 M2 MMF。

JB-15 应从真实保存的 canonical plans 与按键 readiness 开始；不得直接写 READY，不能静默取首项，必须继续显示不可用项。单项 M1 冻结历史与执行 payload 保持原格式。

## 最终针对性结果

- `pytest tests/modules/builds/test_native_plan_set.py <ready-case> <multiconnection-cover-case> -q --tb=short`：6 passed / 42.44s。包含真实页面三份计划、显式复制与选中两份批量编辑；两账户、两资产映射精确落库；未选第三项地区保持 CA。页面实际 PATCH 保存和 POST prepare，随后真实 continuation 与读取；第一项 READY、越权账户项 BLOCKED、第三项未就绪保持可见。
- 读取接口收敛到既有 `native-checkpoint?view=plans` 后，仅重跑受影响浏览器：1 passed / 3 deselected / 60.43s。
- 并发准备重试：两个独立 PG session 同时请求，复用同 preparation，revision 不变、dispatch generation 只推进一次、旧素材回执保持。旧平铺选择及旧素材 JSON 的只读回读返回单项且原 JSON/冻结 payload 不变。
- 真跑近似错误探针：关闭重复键拒绝 → schema 检查失败；逐项回执只返回第一项 → 混合准备检查失败。均 exit 1，逐文件从外部备份还原并逐字节核对；恢复后两项检查 exit 0。日志位于本机 `m2-implementation/jb14-probe-*.log`。
- 最终 `bun run build`：TypeScript、正式 Vite、demo Vite 全通过。`ruff check` 与固定 Python 3.14 `compileall` 通过。
- TDD 边界：先行红覆盖旧输入缺 canonical plans；PG/浏览器用例按纵切补齐，部分检查在接线后加入，不宣称所有用例均先红后写。上述探针证明关键检查拒绝近似错误。

独立验收仍应关注：两项均有不同账户正资格的组合、有效项与兄弟项仅 TTL 过期的完整异步刷新交错、跨来源/tenant/route 的完整失败矩阵及不同草稿素材锁竞争。本轮真实浏览器第二账户刻意无授权，用于证明 BLOCKED 不吞 READY；不把它当成双账户均可执行的证明。没有真实平台或生产验收，没有形式证明。

最终命令（backend 工作目录，均经固定 runner；前端命令从 frontend）：

```sh
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest tests/modules/builds/test_native_plan_set.py 'tests/modules/builds/scene_jobs/test_native_series.py::test_native_job_exact_asset_complete_streams_and_unverified_combination[ready]' tests/modules/builds/scene_jobs/test_native_series.py::test_native_multiconnection_cover_ambiguity_cannot_reuse_ready_mapping -q --tb=short
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest tests/modules/builds/test_native_plan_set.py -k browser -q --tb=short
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python /tmp/tkada-native-series-20261112/m2-implementation/jb14-probe.py
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest 'tests/modules/builds/scene_jobs/test_native_series.py::test_workspace_prepare_drives_native_job_without_provider[False]' -q --tb=short
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest tests/modules/builds/scene_jobs/test_native_series.py -k 'video_verification_resumes or selected_image_cover' -q --tb=short
bun run build
```

末次素材改动：任务键加 `plan_key`，避免同 preparation/source 的不同计划使用相同调用键；已有 distribution/covers 仍负责真正去重和上传。上述素材专项 2 passed / 26 deselected / 107.09s；空视频旧输入保持明确 BLOCKED，针对性 1 passed / 52.09s。时间为本机真实运行时间，不作为性能基准。

原生调用方回归实际命令：

```sh
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest tests/modules/builds/scene_jobs/test_native_series.py tests/modules/builds/test_native_series_selection.py tests/modules/builds/test_native_preview.py tests/modules/builds/test_native_execution.py -q --tb=short
```

该轮初始源码结果 **92 passed、2 failed / 1338.87s**，不是全绿：①旧断言把过期能力期待为 `account_build_unverified`，与已存在的 M1 TTL 分类 `account_build_evidence_expired` 不符，更新断言保持既有业务语义；②本次准备聚合遗漏 `material_issues`，导致封面多义时父回执 error_code 为空，已补回具体素材原因。两项均包含在上述 **6 passed** 的修复后针对性运行中，没有重新跑整套。其余 92 项包含单项直接 ENABLE、实际回读、UNKNOWN、取消、TTL 与最终发送围栏；配合末次素材任务键专项复验，不声称整仓或所有跨计划交错均已通过。
