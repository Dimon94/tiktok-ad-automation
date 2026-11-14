# 复盘报告、规则确认与剧单排期交互修复

## 用户要求与根因

复盘应提供可读报告和可审阅执行的规则候选；历史不能平铺，剧单排期须使用与广告搭建补充信息一致的工作表交互。当前 summary 被单个 p 展示、recommendations 未展示、历史固定50条且按钮平铺；冻结 optimizer 的废弃 authorization_limits 与实际发布合同冲突，模型误判不可提出规则变更。既有 accept 接口实际能够校验版本并发布规则，不能重复实现另一套写规则通道。

## Global Constraints

- 在现有 main 工作，不创建/切换分支；按文件职责共享工作区，不覆盖其他修改。用户已授权改造及生产发布。
- 保持租户/BC/账户/版本/fence、人工确认及自动模式边界；不为了造待确认事项而编造证据、启用规则或修改生产投手模式。
- 保留历史原结果；新复盘结构化输出，旧结果按原文分段展示并展示已有 recommendations。
- 使用现有 shadcn/ui；排期工作表与 ManualLinksDialog 的连续单元格、表格边框、粘贴和键盘行为一致，提取真正共用组件。
- 不新建备份/恢复演练，不更改运行配置/资源/并发/开关。规则发布仍由现有领域 accept 服务完成。

## Task 1: 前端报告与工作表（独立实现）

文件职责：frontend/src/features/ai-workbench/*、必要共用表格组件、frontend/src/features/builds/ManualLinksDialog.tsx、frontend/tests/ai-workbench*.spec.ts、必要独立表格测试。不要改后端/插件/发布脚本。

- 复盘页改为分页历史列表（时间、中文状态、短结论），点击单轮详情，100轮不平铺。报告、普通建议、可执行变更明确区分。新报告结构如下：result.report={headline:string,verdict:string,sections:[{title:string,body:string,evidence_ids:string[],table?:{columns:string[],rows:string[][]}}],rule_decision:{status:'PROPOSED'|'NO_CHANGE'|'INSUFFICIENT_EVIDENCE',reason:string}}。summary 为简短概述。旧任务无 report 时分段呈现 summary，不能整个 p 吞换行。不引入不安全 HTML。
- 展示 recommendations 的问题/建议/验证条件与证据；无 proposals 时解释本轮未调整或缺证据，不将普通建议冒充待确认规则。待确认变更可跨轮分页查看，按 review_task_id 追溯所属轮次。变更展示 rationale/problem/expected_effect/validation、字段级调整前后、范围与规则版本、发布状态；原始 JSON 仅折叠详情。确认后调用原 accept API 并展示 published_version_id；拒绝可填原因。不要自动调用任何确认接口。
- 后端 API 合同：GET .../reviews?offset=0&limit=10&status=COMPLETE 可筛选，返回 {items:[{id,status,created_at,finished_at,optimizer_version,preparation,headline,summary,proposal_count,recommendation_count}],total,offset,limit,has_more,runtime_ready,active_task:{id,status,preparation}|null}。列表无 result；选中后 GET .../reviews/{id} 返回 result 及 created_at/finished_at/optimizer_version，另 review_context={optimization_mode,proposal_submission_allowed:true,publication:'HUMAN_CONFIRMATION'|'AUTHORIZED_AUTO'}。轮询活动任务不因查看历史停止；存在活动任务时禁止重复发起。
- GET .../proposals 返回数组，支持 status=PENDING、review_task_id、offset、limit（可取11条显示10条判断下一页）；不传 status 为全部。Proposal 完整字段看现有 _public，包括 review_task_id、rationale、problem、expected_effect、validation、before、after、authorization、published_version_id。继续使用现有确认/拒绝请求及稳定 request_id。
- 修正全部“剧单排”为“剧单排期”。复用/抽取 ManualLinksDialog 工作表组件，让排期连续无圆角单元格、固定表头、横向滚动、合理列宽、添加行、批量粘贴、撤销和键盘移动；保留只读已执行行、文本ID前导零、日期/投手切换隔离。不要复制第二套核心粘贴解析器。
- 增加真实浏览器回归：分页100轮/详情/结构化建议、可执行规则确认发布回读与拒绝、无提案解释、活动轮询/跨投手隔离；排期与搭建共同表格行为、只读/粘贴/前导零。跑相关 Playwright 和 build，报告命令和结果。不提交，根代理集成后聚焦提交。

## Task 2: 后端与 Agent 合同（根代理）

- 调查本机 Skill 和本地工作台规则提案/报告格式，明确生产复盘无提案原因。
- 移除冻结输入中的废弃细粒度授权，唯一能力目录明确提出候选与发布授权两回事，MANUAL 可提交并等待人确认。保留既有账户/版本/共享规则校验。
- 增加上述结构化 report 合同和领域验证/插件工具 schema/Skill 指导。新成功复盘必须给出清楚规则处置：有提案、无须变更或证据不足及原因，不能用一段自然语言代替可执行配置。证据不足允许不提案，不伪造规则修改。
- 实现分页轻量历史列表、活动任务信息、按轮筛选及分页提案、详细上下文，校验与测试真实PG隔离/旧历史/确认发布实际规则及拒绝不发布。

## Task 3: 集成、审查与发布

- 独立审查改动、修复重要发现，运行适用后端/插件/浏览器测试和构建，不重复未受影响大套件。
- 记录测试/交互截图与限制，明确无证据支撑时不应制造规则候选；生产已有结果保留不改写。
- 提交、推送公司main，精确应用/DSH配套发布，保持配置与 schema；真实只读复盘核对结构化报告/规则决定与页面。未经用户逐项确认不采纳生产规则。
