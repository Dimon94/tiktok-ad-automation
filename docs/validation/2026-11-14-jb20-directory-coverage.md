# JB-20：Campaign / Adgroup 独立目录覆盖

- 基准：`9bbe663f137c9c85d54ef65b8c0327d92c8d1a29`。
- 分支：`fix/jb20-directory-coverage`；实现阶段用户授权创建独立任务树，不合并、不推送、不部署；后续显式调用 `git-rebase-main` 授权本地集成与直接 Issue 收尾，仍不推送目标分支、不部署。
- 问题：`observe_occupancy` 将两类目录一起排序取一条，较新类型掩盖另一类旧/缺失事实。
- 修复：按 kind 各取最新已发布 COMPLETE Run，保留原 tenant/BC/account/actor/frozen route 限制。`directories.campaign/adgroup` 各自携带 `coverage`、`directory_coverage`、`directory_query`、`observed_at`、`published_version`。移除新预览中含混的顶层共同元数据。
- 聚合：任一缺失为 UNKNOWN；否则任一过期为 STALE；否则 PARTIAL。目录同步 COMPLETE/COMPLETE_MATERIALS 不等于完整平台 ACTIVE 语义。各类真实同步覆盖及查询范围单独展示。无新增全局阻断、同步 owner、迁移或平台操作。
- 冻结：沿用现有 JSON 持久化及摘要 owner，不重写历史；历史预览仅把已记录的证据展示在其注明的目录类型下，另一类仍 UNKNOWN。

## 验证

使用既有 Python 3.14 环境、独立本地 `tkada_jb20_test` PostgreSQL 数据库，Redis 使用既有专用测试库且仅操作测试前缀。未输出配置凭据。SDK 外部传输为替身，预览准备、HTTP、数据库持久化及读回均为真实实现。

先运行新增六场景（新 Campaign/旧 Adgroup、反向顺序，各含 STALE/PARTIAL/缺失）：

```text
AssertionError: assert 'PARTIAL' == 'STALE'
KeyError: 'directories'
AssertionError: assert 'PARTIAL' == 'UNKNOWN'
6 failed, 13 deselected in 45.53s
```

修复后定向运行：

```text
pytest tests/modules/builds/test_native_batch_preview.py tests/modules/builds/test_native_preview.py \
  -k 'directory_kinds or partial_coverage or visible_occupancy or directory_coverage' \
  -q --tb=short --show-capture=no
11 passed; 2 failed（旧单计划测试仍从批次顶层读取 occupancy）
```

旧测试改为从 `native_request.plans[0].occupancy` 读取，再定向运行：

```text
pytest tests/modules/builds/test_native_preview.py -k directory_coverage -q --tb=short --show-capture=no
2 passed, 14 deselected in 12.56s
```

以上合计 13 个相关场景通过，含现有 ACTIVE 只排除受影响计划、PARTIAL/STALE/UNKNOWN 不禁投、精确预算及六个独立持久化输入场景；未重跑无关全量测试。

前端 `bun run build` 的正式及 demo 构建通过。最终 `bunx tsc -p tsconfig.build.json` 通过。通过 React `renderToStaticMarkup(NativeBatchApproval)` 真正渲染新双目录与历史单目录输入，断言两类名称、独立时间/范围和缺失 UNKNOWN：

```text
PASS: current and historical frozen evidence render under their recorded directory kind
```

Ruff format/check 及 `git diff --check` 通过。独立 Standards R1、Spec R1 均 PASS（无 P0/P1）；两轴报告同一历史冻结展示 P2，已按已记录目录类型保留展示并完成上述组件验证。

近似错误探针：临时将类型筛选改为恒取 Campaign，运行 `[PARTIAL-adgroup]` 场景；测试因真实目录覆盖由 COMPLETE_MATERIALS 被冒充为 PARTIAL 而失败。`finally` 恢复源码并逐字节核对后，同一场景通过：

```text
AssertionError: assert 'PARTIAL' == 'COMPLETE_MATERIALS'
1 failed in 6.89s
Restored source byte-for-byte
1 passed in 11.24s
```

## 边界

浏览器视觉与生产验收 Unknown，无真实广告操作。实现阶段未提交、合并、推送、部署或写 Linear。后续本地集成及 JB-20 关闭的精确结果以 Git 历史和票据落地评论为准；本地验收不代表线上已发布。
