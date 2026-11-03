# 明略版权方接入实施计划

> 按用户指令在当前会话直接实施；不启动额外审批或自动发布。

**Goal:** 明略复用现有版权方连接、应用选择、搜剧、持久取链、预览和搭建流程。
**Architecture:** 按本地明略 CLI 协议新增 Python 适配器；凭据按租户加密，应用保存 partner/app 上下文；建链进入现有 effect 与 scope 恢复机制。
**Tech Stack:** Python/httpx/SQLModel/Alembic，React/shadcn，离线验证码识别。
**Spec:** 当前会话已确认的接入方案及 `../minglue-drama-link-tool/README.md`。

## 约束与验收重点
- 不调用真实版权方写接口，不发布，不推送；保留现有未跟踪构建文件。
- 所有业务请求绑定已发现的 partnerId/appId，只开放 TT Minis 应用。
- 起播集默认 1，按剧集真实编号转换 sectionId，不改付费设置。
- 分页未完整、可推广性未核实、创建结果未知时禁止盲目创建。
- 验证码失败有界重试；连接失效沿用共享恢复，账号错误不能无限重试。

## Task 1：协议与持久流程
- [x] 增加明略协议和连接/取链的针对性回归用例。
- [x] 新增 adapters/minglue.py 和 minglue_steps.py，接入 registry、connections、session_refresh、capabilities、link_steps。
- [x] 验证分页、应用隔离、起播集、链接复用、超时恢复、严格回读和租户权限。

## Task 2：模型、界面与运行依赖
- [x] 更新 ProviderKind、模型约束并新增可安全降级的 Alembic 迁移。
- [x] 接入验证码识别依赖，检查 Python 3.14 安装及离线运行，锁定 Linux headless 依赖并记录容器实际验收待办。
- [x] 更新连接界面、名称、演示能力和 OpenAPI 客户端。
- [x] 运行迁移、版权方回归、静态检查及前端构建。

## Task 3：交付
- [x] 检查差异并记录实施进度、验证结果和真实联调待验项。
- [x] 按子仓库要求聚焦提交，只包含本轮文件；不推送。
