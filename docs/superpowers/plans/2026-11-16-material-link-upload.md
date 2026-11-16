# 素材链接上传 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement task-by-task in this session. User confirmed implementation; no additional execution-method approval needed.

**Goal:** 在素材库完成多账户 VID 链接上传、可恢复批次和实际账户素材预览。
**Architecture:** 独立持久链接批次与逐账户任务，复用事务 outbox、冻结路由、SDK 素材读写和现有预览。核实后的素材登记 MaterialFile/AccountMaterial，输入来源保留在链接任务。
**Tech Stack:** FastAPI/SQLModel/PostgreSQL/Celery/Redis；React/TanStack/shadcn/ui。
**Spec:** `docs/superpowers/specs/2026-11-16-material-link-upload-design.md`（用户已确认）。

## Global Constraints

- 当前租户与当前 BC 隔离；提交固定连接与授权代数；每次物理请求重新授权。
- 不创建新分支、不推送、不部署、不执行真实平台写入；本轮完成后聚焦本地提交。
- 默认上海日期 + 源 VID + 原输入行号；未知请求只回查，明确失败才允许重试。
- MCP 通道已退役，产品新增 VID 写入走现行官方 SDK；MCP 明确拒绝该能力，不复活退役通道。
- 每批最多 500 个非空链接、20 个目标账户、2000 条账户任务；分页读取，后台一步一次有界请求，沿用现有导入开关。

## Review Focus

- 同一 MID 的多个输入必须保留来源映射，不能重传或不断改名。
- 上传后 VID 变化按 MID 查回；空搜索不能据此重传。
- 已发送任务崩溃、重复队列消息与撤权不应产生第二次写入。
- BC 切换与预览请求迟到不能串用旧账户。
- 连续提交使用相同默认名称时先查回已存在素材；同请求 ID 内容变化必须拒绝。

### Task 1: 持久批次与授权 API

Files: create `backend/app/modules/materials/link_models.py`, `link_schemas.py`, `link_service.py`, `link_api.py`, `backend/app/alembic/versions/0132_material_link_upload.py`; modify `app/models.py`, `app/api/main.py`.
Interfaces: `parse_links(text) -> list[ParsedLink]`，`create_batch(session, context, body)`，分页批次/任务读取与单项重试。链接任务保存冻结路由、发送阶段、下次核实时间与领取租约。
- [x] 添加解析、边界、顺序、账户权限、幂等及跨租户任务隔离测试。
- [x] 实现模型、迁移与 API；并发请求以请求唯一键复用，所有目标账户先验证后建批。
- [x] 在隔离测试 PostgreSQL 上执行新行为测试与迁移检查。

### Task 2: 官方上传与恢复

Files: modify TikTok material contracts/SDK/gateway/platform quota registry; create `link_worker.py`, `link_tasks.py`; register in `celery_app.py` and recovery schedule.
Interfaces: `upload_video_id(VIDVideoUpload, budget) -> VideoReceipt`；`run_link_item(database_engine, redis_client, context, item_id)`；`repair_link_items(session, limit)`。
- [x] 添加传输边界测试及真实数据库领取/重复消息测试。
- [x] 使用既有上传物理端点额度，原子领取与发送标记；先核查稳定命名，回执后按 MID/实际 VID 核实，同内容登记实际映射，未知结果不重传。
- [x] 测试平台合并、VID 变化、撤权、进程中断、明确失败重试及延迟分页恢复。

### Task 3: 素材库界面

Files: create `LinkUploads.tsx`, `LinkUploadSheet.tsx`, shared link upload presentation; modify `MaterialsPage.tsx`, routes search schema, `AccountPicker.tsx`; regenerate OpenAPI client.
Interfaces: 链接批次与任务分页 API；现有 `readAccountVideoPreview`，账户选择增加 upload eligibility（保留其他调用方默认 build）。
- [x] 添加 Playwright 行为测试：多行校验、多账户、提交幂等、列表刷新恢复、详情、预览及 BC 切换。
- [x] 使用现有 shadcn Field/Textarea/ManagementSheet/ServerTable，复用预览组件与授权取消。
- [x] 运行定向 Playwright、Biome、TypeScript/Vite 构建。

### Task 4: 验证与交付

- [x] 审阅租户/账户/路由/未知结果/迁移边界，修复后重跑受影响用例。
- [x] 更新实施进度、迁移发布注意事项与本地验证结果。
- [x] 检查暂存范围并提交，仅包含本轮改动；报告未部署及真实平台联调未执行。

验收证据：`docs/validation/2026-11-16-material-link-upload.md`。部署及真实平台联调未执行。
