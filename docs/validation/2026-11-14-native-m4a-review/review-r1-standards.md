# M4a whole-MMF Standards 独立审查 R1/3

Verdict: **PASS**（无未解决 P0/P1；不代表自行放行 M4a）。

| P0 | P1 | P2 | P3 |
| --- | --- | --- | --- |
| 0 | 0 | 1 | 0 |

## 固定范围与证据核对

- 审查者：Codex gpt-6.1-sol/high；本轮自行审查，未派下级代理，仅 Standards 轴。
- 树：`/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a`。
- Base：`b0f3b1443e7fedc19f297d078a706d5f8ef50040`。
- Head：`573ebc8fc2f50d212856c9d8ec23a29645ae3b59`。
- 亲查 `rev-parse`：树 HEAD 与指定 head 一致；`merge-base(base, head)` 恰为指定 base。
- 完整比较命令：`git diff b0f3b1443e7fedc19f297d078a706d5f8ef50040...573ebc8fc2f50d212856c9d8ec23a29645ae3b59`。核对 bundle 的 base/head/tree/diff_command；实际 `--name-only` 的 80 路径及 `log --oneline base..head` 的 16 提交，分别与 `review-r1-paths.txt`、`review-r1-commits.txt` 逐字一致。未换基线。
- 起止工作树状态相同：分支 `feat/jb24-native-growth-m4a`，仅 `.venv`、`node_modules` 未跟踪项；未操作这些项。无源码编辑、提交、切分支、新 checkout、Linear、平台、生产、PR 或 push 操作。唯一写入为本报告。

## 规范来源与审查方式

已读 `/Users/dimon/.codex/RTK.md`、`/Users/dimon/.agents/skills/code-review/SKILL.md` 的严重度、基线和轮次规则，以及目标树 `AGENTS.md`、`CONTRIBUTING.md`、`development.md`、其引用的 `docs/runbooks/bootstrap-deployment.md`、`docs/engineering-baseline.md`、当前视觉计划 `docs/superpowers/plans/2026-09-10-workspace-visual-rollout.md`。以父工作区 `GLOSSARY.md` 和 ADR 0002 确认业务语言及 BC 隔离边界；目标子仓库没有自己的 GLOSSARY/ADR 目录。

重点规则：AGENTS 的 “Preserve tenant and BC scope in every data path”、官方 SDK 边界、复用现有 owner、评估调用方/数据/接口/后台任务影响、相关行为验证与真实 PG/Redis 并发证据、历史数据和凭据保护；视觉计划第20行要求保留角色权限和外部接口契约。工程基线要求使用既有锁文件和独立测试服务。按用户授权接受本分支共享集成树串行工作流及 Sol/high，不追溯判定已授权流程为违规。

独立追踪了 API → 原需求/调度/SDK Reader → 分页 checkpoint → 原执行围栏/发布事务 → direct/derived 选择 → SQL 成员、阈值、排序、summary → RR 冻结结果 → 分页/详情/CSV 的主路径。实现者验证记录只作为有来源的继承证据，不作为本审查 verdict。

## Findings

### P2 — creative 详情携带快照时触发未绑定局部变量

- 精确位置：`backend/app/modules/reporting/detail.py:442`；绑定仅发生于同文件 `:328-330`。
- 违反规范：`docs/superpowers/plans/2026-09-10-workspace-visual-rollout.md:20`：“保留……角色权限和外部接口契约”；AGENTS 要求修改前评估接口和调用方影响。
- 触发/反例：同一当前用户、租户、BC，已有可读 creative C 和有效快照 S；请求既有 `GET /ads/creative/C?advertiser_id=A&bc_id=B&snapshot_id=S`。`api.py:448-456` 允许 creative 和 snapshot_id。快照与目录检查通过后，creative 跳过 `:328` 分支；返回时 snapshot 为真，却求值尚未绑定的 `_snapshot_public`，抛 `UnboundLocalError`，HTTP 500。
- 影响：该合法参数组合无法读取目录详情；不带快照的 creative 入口不触发。故按特定条件问题定 P2。
- 基线/同类：精确 base 中只校验快照后返回详情，没有该局部函数调用。跨 diff 查 `_snapshot_public`/`report_snapshot=`，另一调用在 queries 模块顶层定义后执行，未见同类未绑定路径。以上为静态可走通反例，未冒充真实 HTTP 亲跑。

## 主路径核对结果

- 权限与边界：新父级需求在写 scope/outbox 前复用 verify_route；任务保留 actor/frozen route，执行继续使用原 TransactionBoundaryReader/fence。查询、快照读取、详情目录定位与导出生成/下载仍走当前租户、BC、操作者和账户权限检查；未发现新增无授权直达入口。
- 持久状态：新 parent 事实与 Ad 派生 writer 物理隔离；selected views 用成功发布状态压制旧 derived，包含 direct 缺项/空发布。新迁移为 additive，未改旧金额或给历史行补造当前证据；涉及金额的 downgrade 明确拒绝未经审定的回退。
- 发布：先完整读页及身份校验，再在既有账户/kind 锁内比较观察水位；同观察重放 no-op、冲突拒绝；事实、证据、状态与 requirement 满足仍位于原事务。失败状态保留旧成功金额/观察。检查了 Campaign、Adgroup、Ad 同类路径。
- 查询/金额：MetricCatalog/LEVELS 继续作为合同与层级注册 owner。SQL 同一选择源参与成员、阈值、排序、summary 和冻结投影；按 level/source/revision/currency 分桶，金额使用 Numeric/Decimal，平台比率作为单观察，不把其平均值充当本地 ROAS。贡献子集有独立显式输入和维度限制。
- 冻结/导出：普通日报把最终展示向量及 provenance 固定到 snapshot row；分页与详情读取该结果，旧合同拒绝解释。导出复制结果、过滤范围、合同与发布图到现有 ReportExport，生成不依赖原快照继续存活；保留当前授权重查、TTL 和旧已完成 CSV，过期清理清除复制载荷。素材继续使用原 owner 路径。
- 前端：新增展示沿用现有组件与 SDK 客户端；日层发布提示用户重新查询，不自动改写已打开快照。未将命名、排版偏好或工具可强制的风格列为 finding。

## 动态验证与 Unknown

本轮亲跑（标准库、无 PG/Redis、无源码写入）：

| 命令 | 结果 | 证明范围 |
| --- | --- | --- |
| `python3 .agents/skills/formal-verify-reporting-data/scripts/check.py` | PASS，48 states | R4/R10 有限选择模型 |
| `python3 .agents/skills/formal-verify-reporting-data/scripts/publication.py` | PASS，57 component states | R7 两 worker/两观察/三层分量模型 |
| `python3 .agents/skills/formal-verify-reporting-data/scripts/snapshot.py` | PASS，128 observations | R9 有限冻结/导出观察模型 |

这些模型没有执行产品 SQL，不证明任意并发或整个系统正确。本轮未运行 source_probes、源码变异、共享 PG fixture、浏览器、升级或真实外部调用。

内存 compile 静态检查未完成：预期 `.venv/bin/python` 不存在；系统 Python 为 3.11.2，不支持项目已有的 Python 3.14 泛型语法，在 diagnostics.py 的既有 `summarize_job[**P, R]` 语法处停止。此为检查环境限制，不是新回归；未安装依赖或改动软链，不宣称语法检查通过。

继承而非本轮亲跑：`docs/validation/2026-11-14-jb25-native-growth-campaign.md` 至 JB30 同日期记录及 formal skill 的 SDK transport、真实 PG/Redis、HTTP/browser、升级、数值与 source-probe 证据。用户提供 root 最新 native_exports+exports 12、snapshot+reuse 19、scope+dimension 79 及修后 scope 63 通过，按调用方证据记载，不计入本轮亲跑。亲查 `2a42c983e6dd364032f6ea7875b29d2be1080a90..head -- backend/app frontend/src` 为空，JB30 产品候选之后没有产品源码变化。更早票证据按其实际覆盖与后续变化理解，不称所有旧测试都在最终 head 重新执行。

严格 mypy、全仓历史失败未在本轮重跑，未据旧410条或旧 fixture/PG版本失败判新回归。真实平台指标含义确认、生产部署/迁移/回滚和生产容量仍 Unknown；缺少这些验收不新增审查 blocker。P2 的真实 HTTP 动态回放留给 root 在原独立环境串行验证，不需要修改源码即可构造上述参数组合。

本报告只结束本轮 Standards 轴审查；M4a 的其它审查轴、整体验收和本地 main 集成由 root 决定与执行。
