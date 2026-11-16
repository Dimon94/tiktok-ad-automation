# 13:00 两轮复盘模型中断调查

## 结论与边界

两轮均已完成数据准备，失败边界位于公司模型网关 `com / claude-opus-5-5` 的后续推理请求。原生会话记录的终止消息相同：

> The selected model cannot continue this conversation. Please select a different model, or start a new conversation, or rewind the current conversation to an earlier point and try a different approach.

DSH 将其分类为 `PI_AI_ERROR`。这是上游拒绝继续当前会话的提示，不是 TK-ADA 生成的业务判断。当前证据不能进一步断定是上游策略拒绝、路由/会话状态问题，还是网关隐藏了其他错误；不能据此认定内容违规、账户过多或上下文超限。应用持久层没有原始 `stop_reason`、HTTP 状态、上游请求编号，需用下列精确时间到模型网关追查。

实际连接为公司网关 `https://ai-claude-code-hub.jetmobo.com`，协议为 `anthropic-messages`，不是直接连接模型原厂。网关中的模型名称及用量不能证明真实底层路由或容量。

## 生产只读核验

时间均为北京时间 2026-11-16，来自生产 PostgreSQL 与 DSH 持久化会话交叉核验。两轮绑定的 **复盘方法** 都是 1.8.4；1.9.0 也是方法/插件版本，不能等同应用 Git SHA 或 DSH 宿主版本。

| 项目 | 星屿 | 另一轮 |
| --- | --- | --- |
| 任务 | `cda85ddb-3abf-43a3-99e2-85f666bf58ab` | `25f1416d-d017-45d0-89d5-c60645f3e96d` |
| 创建 | 13:00:14.970 | 13:00:14.862 |
| 分析开始 | 13:17:47.189 | 13:00:53.131 |
| 失败 | 13:23:30.044 | 13:07:51.830 |
| 准备成功/失败 | 1344 / 0 | 50 / 0 |
| 工具调用/回执 | 28 / 28 | 38 / 38 |
| 失败推理步骤 | 第 8 步 | 第 11 步 |
| 失败请求上报总 token | 105,172 | 172,618 |
| 失败请求输出 token | 0 | 0 |
| 冻结来源 / 已交付凭证 | 24,203 / 15 | 911 / 21 |
| 已保存分析 / 时序 / 日回放 / 笔记 | 4 / 2 / 6 / 1 | 5 / 2 / 6 / 1 |

星屿最后成功的一组动作包含知识搜索、经营分析和放量方法读取，随后模型请求失败，未调用完成工具。另一轮在提交草稿后收到 `work_required`（约 56 KB 的工具事件，含诊断提醒），下一次模型请求失败；这是应用要求补齐工作，不是工具异常，也没有写入最终报告。两轮均无工具 `isError` 回执，不是相同业务工具直接报错。

原生会话登记上下文容量均为 1,000,000；两次失败用量低于登记值。但登记值未证明网关真实容量，用量也只代表网关上报，不能用它证明或完全排除隐藏的容量限制。50 账户同样失败，不能将问题归因于星屿 1344 账户。

对照任务 `fc4c3628-0582-49e1-be24-9658f90c6e7f`：同为星屿 1344 账户、方法 1.8.4，10:59:22 已提交最终报告并处于 COMPLETE；原生 Agent 之后还遇到两次传输失败及同文模型拒绝（197,533 token）。应用已有 `completed` 保护，没有把已完成结果覆盖成失败。因此“原生 turn/end 是 error”与“业务报告未完成”必须分开判断。更早任务 `405aecff-4455-4a46-b9d7-bb6a8839052b` 也完成了 1344 账户复盘。

## 错误在哪里丢失

核对生产安装的 `@earendil-works/pi-ai`：Anthropic 适配器支持将 `stop_reason=refusal` 的 `stop_details.explanation` 转为错误消息；SDK 异常也可进入同一出口。因此本次现象符合上游会话拒绝，但仅凭最终文本不能还原原始 stop reason。

DSH `dsh-llm-pi-ai` 的 `mapStopReason` 保留文本，`classifyPiAiError` 未匹配明确状态码、网络、超时等分类时统一返回 `PI_AI_ERROR`；其原始 stop reason 没有进入当前会话的 finish failure。

应用 `integrations/dsh-ai-workbench/runtime.mjs:168` 只检测错误 kind，在 169 行抛出固定 `Native Agent model turn failed`；179 行向领域完成接口提交统一失败摘要，丢失模型错误类型及文本。196 行 pump 再丢弃异常，只输出固定“durable task retained for fenced recovery”日志。界面只是显示领域结果的 summary，不是前端自行遮掉了具体原因。

## 现有接续能力与实际缺口

- 自动领取仅选 PENDING 或授权过期的 RUNNING（`backend/app/modules/ai_workbench/runtime.py`）；本次异常被立即落为 FAILED，因此不会自动续跑。固定日志所谓“retained for fenced recovery”不意味着 FAILED 已自动接续。
- **已有手动重启能力**：`ai_tasks.restart_task` 对 FAILED 生成新 fence、清空失败结果并置 PENDING；数据准备完成时不重拉账户资料。来源、产物、凭证、笔记都保留。不能笼统声称“没有任何恢复机制”。本次调查未执行重启或新建任务。
- 新 fence 会新建原生会话；提示要求读旧笔记。但 `verificationRuntime` 的完成工作账本和诊断记录是内存 Map/Set，新会话没有从持久化凭证重建；`usedMethods` 和草稿自检阶段也重新开始。因此“保留证据/一篇笔记”不等于完整分析进度无损接续，容易重复工具工作和再次增长上下文。
- 1.9.0 当前代码仍保留上述异常处理，更新报告方法不会自动解决本次问题。

## 后续修复边界

1. 在原生适配层保留原始 stop reason、经清理的错误类型、请求编号及失败步骤；应用持久化可公开的分类与可操作提示。明确区分会话拒绝、上下文超限、限流/配额、认证、传输、超时、工具异常和取消，不能把此文本强制归为上下文超限。
2. 为可恢复中断设置有限次数的新会话接续，复用同任务冻结来源、方法、证据凭证和笔记；重建已完成工作账本，补剩余缺口。继续进行租户/账户/版本/授权检查及 fence 校验，不重放领域提交，不影响已 COMPLETE 任务。
3. 对话中重复返回的诊断应按本轮缺口精简。56 KB 提醒是可优化因素，尚无证据证明它触发本次拒绝；星屿尚未到该提醒阶段便失败，不能将它当作共同根因。
4. 网关侧用 13:07:51.790 与 13:23:29.988 的模型终止时间及模型名匹配请求，核对原始响应 stop_reason、stop_details、请求 ID、实际后端路由和真实容量。现有应用记录无法独立完成这一层归因。

## 本轮操作与验证

仅执行生产只读 SQL、持久化会话解压摘要和已安装适配器源码核对，没有模型重放、任务重启、广告/规则操作、配置修改或发布。私有摘要和调查脚本位于 Git 忽略的 `.runtime/review-failure-investigation-20261116/`，原有摘要位于 `.runtime/unified-release-20261116/failed-session-audit.json`。未运行应用测试：本轮只有调查文档，未修改运行代码；不宣称故障已修复。
