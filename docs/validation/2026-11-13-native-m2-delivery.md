# M2 本地交付验收

范围：JB-13 及实施票 JB-14～JB-18；总 Epic 为 JB-19。批准的一批共享策略和投放连接，逐计划明确账户、Series、双身份、地区与素材，沿现有创建工作区完成准备、准确冻结、提交、逐计划结果、UNKNOWN 恢复和本地取消。

## 固定版本与验收结论

- 集成基点：`b185736753df3a0f6992d9494867ae877eb1b089`。
- 完整独立审查 head：`4743ccbfe2f7db9c2960f339610f440b52a2b3ec`。
- 唯一审查修复产品提交：`0d9c8846fe1d2ba7318d28c159e891a275018c62`。
- 独立模型同步及最终审查 head：`47d74d4cc476e9836ef1ac2fc37ddf6efce615c1`。
- Standards R1 PASS，无 P0/P1；Spec R1 的一个 live-sibling P1 经真实红绿修复，Spec R2 PASS、无新增 P0/P1。R2 只复查修复及影响，其余范围沿用 R1。
- root 核对完整交付记录、模型同步、审查报告及实际源码 diff，接受本地开发验收。`git rebase main` 无变更；集成前 root 在同一源码运行 `test_native_batch_execution.py -k native-ad-PENDING`：**1 passed，47 deselected，22.58s**。

## 已验证的用户流程

- JB-14：显式计划集、复制/选中编辑、双账户独立准备、单项 TTL 刷新与兄弟隔离。
- JB-15：逐计划 INCLUDED/EXCLUDED、精确冲突范围、既有策略分配、Decimal 预算及原子冻结。
- JB-16：同预览唯一 Submission、逐 Unit ENABLE、队列和物理发送围栏、不可变操作者批准代数、实际 MATCH 聚合。
- JB-17：唯一完整原范围 MATCH 才认领 ID；零/多/缺字段保持 UNKNOWN，不盲重发、不重建成功兄弟。历史拒绝条件路径仅消费原明确无效果拒绝证据。
- JB-18：未发送本地取消、在途及 UNKNOWN/迟到事实保留、历史 flat 冻结请求读取、统一调用方。
- 最终同后端真实浏览器准备/冻结/提交/结果/恢复/取消 **3 passed**；真实 PG/Redis，只在 SDK transport 使用业务替身。T1～T20、R1～R13 的 owner、直接实测与继承证据逐项见最终验证矩阵。
- 修复后可执行兄弟的四状态 **4 passed**，blocked 后代对照 **4 passed**；独立同步时受影响结果、取消、恢复 **16 passed**。仍可执行的兄弟保持 RUNNING；失败父级后代不导致无限等待；最终无完整 MATCH 的多计划批次保持 FAILED。
- 原提交、恢复、历史拒绝、取消五模型入口及错误变体已运行；新增 focused Completion 模型与源码错误探针验证聚合修复。模型均限于记录中的有界假设，不是全系统 refinement、liveness 或任意规模证明。

## 证据与维护入口

- [JB-14](2026-11-12-jb14-native-plan-set-readiness.md)、[JB-15](2026-11-12-jb15-native-batch-preview.md)、[JB-16](2026-11-12-jb16-native-batch-create.md)、[JB-17](2026-11-12-jb17-native-batch-recovery.md)、[JB-18](2026-11-12-jb18-native-batch-cancel.md)。
- [最终 T/R 矩阵及浏览器验证](2026-11-13-native-m2-final-verification.md)、[R1 修复](2026-11-13-native-m2-r1-fix.md)、[独立形式同步](2026-11-13-native-m2-r1-formal-sync.md)。
- 独立审查原报告位于 `/tmp/tkada-native-series-20261112/m2-implementation/review-r1-standards.md`、`review-r1-spec.md`、`review-r2-spec.md`。
- 项目验证入口：`.agents/skills/formal-verify-native-series-batch-submission/SKILL.md`，产品 sync 为 `0d9c8846`。

## 运行与未交付边界

新增迁移 `native_batch_preview`、`actor_authorization_stamp` 仅在隔离本地数据库验证；历史无批准 stamp 的提交保持可读，但不允许新写。部署前需按项目迁移与运行手册安排，不代表线上已应用。

真实 TikTok 接受、审核、消耗及收益未验收；需针对具体冻结预览另行授权真实广告写入。未推送、建 PR 或部署。远端只读检查遇到 GitHub SSH `Permission denied (publickey)`；本轮不变更任何远端分支。

基线目录 Campaign/Adgroup 覆盖表达问题已独立登记待分诊，不在本轮修复中扩大范围。JB-19 的 M3 及后续 MMF 仍为候选，整个 Epic 不关闭。

本记录固定本地验收证据；合入后的 main SHA、票据完成状态与工作树回收以协调者和 Linear 实际回读为准。
