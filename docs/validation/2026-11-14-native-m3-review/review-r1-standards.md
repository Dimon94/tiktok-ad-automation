# M3 R1 Standards 独立审查

结论：**PASS**。P0=0，P1=0，P2=0，P3=0。未发现满足具体规则违例、可达触发及可观察影响要求的 finding。

## 固定范围与核验

- 第 1 轮，共 3 轮；仅 Standards 轴，审查者自行完成，未派子代理。
- 唯一任务树：`/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3`。
- base：`55ee37146d7275b8c308161bce1986f9fbbb1670`；实际 HEAD：`d6e209c73f6af27637e7733185a5b92fb10bf1f4`；实际 merge-base 等于 base。
- 核验命令：`git diff 55ee37146d7275b8c308161bce1986f9fbbb1670...d6e209c73f6af27637e7733185a5b92fb10bf1f4`，及对应 `rev-parse`、`merge-base`、`log base..head`。diff 非空，19 文件、2229 additions、27 deletions，与 bundle 一致。
- 三提交依序为：`2701fbfad95c316d8d5a49d8918fafdb2164edf4 feat(ads): navigate directory objects without report facts`；`4ddb3e0031b53f1168b22a7185399285f0daa457 docs(ads): record JB-22 validation and JB-23 contract`；`d6e209c73f6af27637e7733185a5b92fb10bf1f4 feat(ads): expose native Series directory recognition`。
- 审前审后 status 一致，仅原有未跟踪 `.venv`、`node_modules` 链接；未改源码、未执行源码变异探针、未提交/合并/推送/部署/操作 Linear。所有源码命令显式指定任务树 cwd。

## 规范依据与追踪结果

已读 [证据包](/tmp/tkada-native-series-20261112/m3-implementation/review-evidence-r1.md) 和 [code-review skill](/Users/dimon/.agents/skills/code-review/SKILL.md)，按调用方约束只执行 Standards，不启动 skill 的双代理流程。

规范源为任务树 [AGENTS.md](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3/AGENTS.md:13)、[CONTRIBUTING.md](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3/CONTRIBUTING.md)、[development.md](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3/development.md)、[视觉统一计划](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3/docs/superpowers/plans/2026-09-10-workspace-visual-rollout.md:13)及[外层 AGENTS.md](/Users/dimon/02-Work/10-Projects/ads-decision-system/AGENTS.md)。任务树没有 GLOSSARY.md；已核对[外层词表](/Users/dimon/02-Work/10-Projects/ads-decision-system/GLOSSARY.md)的 BC、投放身份、系列和素材投放实例相关词条。未发现嵌套 AGENTS 或 CODING_STANDARDS；词表位置差异本身不列 finding。

逐项追踪：

- **租户与 BC 范围**（AGENTS Execution contract）：新增目录 route 经 `_context`，`read_directory_objects → list_objects → _authorize → require_tenant/usable_grants`；父引用需同租户且处于账户集合，并经 `locate` 校验。keyset 使用完整账户/对象身份，签名绑定 tenant、BC、账户集合、kind、父范围；每页重新授权。创意读取先定位当前授权广告，再按 tenant/account/ad 查询。未发现新增跨范围读取入口。
- **复用既有 owner 与权限合同**（AGENTS 代码与设计要求；视觉计划“保留角色权限和外部接口契约”）：目录入口复用现有账户选择器、目录 SQL、详情 HTTP 和 EntityRef；报表入口保留 snapshot 校验、ReportRow 与管理回调。目录详情分支只读，不产生 ReportRow、冻结选择或管理动作。切换范围仍走原详情关闭流程，查询键包含 tenant/BC/对象或列表范围。
- **持久状态与外部合同**（AGENTS Execution contract）：新增产品代码仅查询现有发布表；无模型、迁移、同步发布器、SDK 业务调用或后台调度变更。native 投影沿真实 parent_ref，配置和素材事实仅按相同 creative ID 连接；素材名称按平台素材 ID、类型、广告内素材 ID 匹配，歧义保留未知。父组、创意配置、素材事实分别保留自己的观察时间和版本；素材事实缺来源时没有借用广告来源。返回 DTO 与生成客户端新增契约相符。
- **展示与维护规则**（AGENTS Tooling/代码与设计要求）：继续使用 shadcn Sheet/Tabs/Button、TanStack Query；两种详情来源共用 DirectoryConfiguration，无平行事实 owner。保留未知字段原配置，空字符串/空数组与未知值区分，明确目录观察不代表实时、完整或整树同版本。未把工具负责的格式问题列入 finding。

关键实现核对位置：[目录 owner](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3/backend/app/modules/ads/directory.py:137)、[目录 HTTP](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3/backend/app/modules/reporting/api.py:380)、[native 投影](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3/backend/app/modules/reporting/detail.py:120)、[详情分支](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3/frontend/src/features/ads/details/AdsDetailSheet.tsx:149)。

## 验证证据与限制

本轮亲跑：refs/merge-base/提交列表/diff/status 只读核验；`git diff --check base...head` 通过。本轮未运行行为测试、构建或浏览器，未运行任何变异探针。

复用而非亲跑：[JB-22 验证记录](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3/docs/validation/2026-11-14-jb22-directory-navigation.md)、[JB-23 验证记录](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3/docs/validation/2026-11-14-jb23-native-directory-recognition.md)和 bundle 所述 root 独立回放。记录包含真实 PG/HTTP/浏览器导航与撤权、官方 SDK transport→暂存/发布→页面、既有报表回归和 build。另实际读取 [jb23-final.log](/tmp/tkada-native-series-20261112/m3-implementation/jb23-final.log) 尾部确认记录为 4 passed/17.48s，[browser-revocation.log](/tmp/tkada-native-series-20261112/m3-implementation/browser-revocation.log) 为 3 passed/16.82s；这些属于既有运行结果。测试源码与受控 transport 的边界已读，未把合成响应解释为真实平台支持。

未验边界：真实 TikTok 字段返回/覆盖完整性/Series 名称支持、生产页面、完整 Celery/Beat 宿主、所有缺失组合的浏览器表现及全量测试均未由本轮动态验证。Unknown 不算 finding，不自动 BLOCK。本结论只覆盖此 HEAD 的 Standards 轴，不替代 Spec verdict 或 M3 整体接受/部署结论；未套用 M2 资金模型或增加生产验收门禁。

**最终 verdict：PASS；数量：P0=0 / P1=0 / P2=0 / P3=0；未验边界：上述平台、生产、调度宿主及本轮未亲跑的动态验证。**
