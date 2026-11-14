# M3 Spec 轴 R2（共 3 轮）

**Verdict: PASS。未解决 P0=0、P1=0。R1 唯一 P1 已关闭；修复影响范围内没有发现新增 P0/P1。**

## 范围与版本

- 只读任务树：`/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3`。
- 固定 base：`d6e209c73f6af27637e7733185a5b92fb10bf1f4`；head：`2937e6dc285b4f0be3ccc1e7c79ef8dc1f13d9c4`。亲自核验两 ref、HEAD、merge-base（等于 base）、status、三点 diff 和 commit list。
- 正式命令：`git diff d6e209c73f6af27637e7733185a5b92fb10bf1f4...2937e6dc285b4f0be3ccc1e7c79ef8dc1f13d9c4`。仅一个提交 `2937e6dc fix(ads): preserve details for unknown creative fields`，4 文件，72 additions / 3 deletions。产品源码仅 `backend/app/modules/reporting/detail.py` 9 行差异（+7/-2）；其余为回归测试和文档。
- 已读取 code-review skill、任务树 AGENTS.md、原 Spec、R1 Spec finding、原 review-evidence-r1 bundle 以及修复验证文档。本轮仅复审 R1 P1 与修复影响行为，不重审完整 M3、不重新猎取 P2；自己执行，未派下级代理。运行模型为用户指定的 Codex gpt-6.1-sol/high。
- Standards R1 PASS 沿用，不冒称本轮重新审查 Standards。公开 DTO、依赖、目录同步/发布、持久模型及前端没有变化。

## R1 P1 关闭依据

原触发：可正常发布的 `creative_list=null` 或独立子创意 `creative_info=null`，分别在遍历清单、身份成员检查处抛 TypeError，导致整个公开广告详情失败。

Spec 第 161–162 行要求保留已观察创意、区分未知与确定空集合，并记录覆盖原因；第 191–194 行要求保留原配置、如实展示缺失、保持报表来源详情入口。

1. **null 清单**：`detail.py:175–189` 先读取现有子对象配置，`creative_list` 为 None 时添加 `creative_list:not_provided`，仅跳过未知的内嵌清单遍历，仍以已有独立创意配置与素材事实的 ID 并集生成投影。没有清空 configs/facts，没有回写原 configuration。正式回归通过真实 SDK transport→stage/publish→PostgreSQL→HTTP，响应 200，cr1/cr2、Series/组身份均保留，原 `configuration.creative_list` 仍为 null，`creative_coverage:unknown` 与具体原因均存在。
2. **null 创意身份配置**：`detail.py:96–103` 对 None 返回 None；原 `detail.py:195–200` 继续记录 `creative_identity:not_provided`。正式回归同样经发布与 HTTP 返回 200，cr1 原配置中的 `creative_info` 仍为 null，创意 identity 为 null，同时组身份仍为 group-1。没有借用组身份或其他创意身份。
3. **受影响同类调用与两来源**：已检查 `_identity` 的两个调用点（creative、group）；非 null 配置仍走原逻辑。原配置、父级、观察版本、来源、素材关系及按相同 creative ID 合并的逻辑未改变。目录/报表来源仍共同调用 `reporting/api.py:428–445` 的 `get_ad_detail` owner；snapshot 仅先验证上下文，随后走同一投影。既有 partial-observation 测试验证带/不带 snapshot 的 native 投影一致。本轮 null 参数测试直接覆盖目录请求，未另加 null+snapshot 参数动态用例；共用修复路径已静态核对。

亲自用 `git show base:backend/app/modules/reporting/detail.py` 核对 R1 的两个错误假设在本轮 base 仍存在，head 的两处处理正好关闭它们。本轮没有在源码上回退、替换或执行变异。未找到可达的新 P0/P1 反例；没有以缺少生产证据生成 finding。

## 证据：本轮亲跑

从任务树 backend 执行既有隔离 runner，测试使用本机专用 `tkada_native_m3_20261114_test` PostgreSQL，事务 fixture 回滚；仅 SDK urllib3 transport 为合成响应，不访问真实 TikTok、生产或共享业务数据库。禁用 Python 字节码与 pytest cache 写入。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 /tmp/tkada-native-series-20261112/m3-implementation/run-test.py python -m pytest tests/modules/ads/test_native_directory.py tests/modules/reporting/test_detail.py -k 'not test_native_workspace_live_http' -p no:cacheprovider -q --tb=short
```

**8 passed, 1 deselected in 3.28s**，包括正式自包含回归 `test_published_unknown_creative_fields_remain_readable` 的两个参数场景、已有原生 HTTP/部分观察/详情合同；不依赖仓外旧 baseline 模块。日志：[review-r2-spec-tests.log](/tmp/tkada-native-series-20261112/m3-implementation/review-r2-spec-tests.log)。`git diff --check base...head` 亲跑通过。

## 继承证据与 Unknown

- 已读 root 原日志：`r1-null-red.log` 两项 TypeError 真实 RED；`r1-null-green.log` 两项 GREEN；两份 `r1-null-probe-*.log` 分别因丢弃观察创意、借用组身份而失败。它们证明检查能拒绝具体错误，均为继承证据，本轮未执行变异；字节恢复结论来自 root 修复证据，不冒称本轮亲历恢复。
- `r1-null-regression.log` 与 `docs/validation/2026-11-14-native-m3-r1-null-fix.md` 的 9 passed（含浏览器主链）、Ruff 通过为继承证据。浏览器用例本轮明确 deselect；没有把 root 的 9 项写成本轮亲跑。原完整 scope 的其他结论、构建记录与 Standards PASS 沿用 R1。
- 真实平台字段、Series 名称、完整目录覆盖、生产验收及 Celery/Beat 宿主仍 Unknown；不自动阻断这次本地只读投影修复。Nowledge 已做一次定向历史检索，无匹配本轮的相关结果，未作为审查依据。
- 本报告仅给出 Spec R2 范围结论；不代替 root 的 M3 统一接受、集成或发布决定。按 skill，本 scope 无未解决 P0/P1，Spec 轴修复复审终结。

## 只读边界回读

审查前后 HEAD 均为 `2937e6dc285b4f0be3ccc1e7c79ef8dc1f13d9c4`；tracked/staged diff 均为空，status 仅原有 untracked `.venv`、`node_modules`。两链接目标前后分别仍为主仓库 `.venv` 与 `node_modules`。只新增仓外本报告及本轮日志，未创建 checkout，未修改源码/模型/测试，未提交、合并、push、deploy 或操作 Linear。
