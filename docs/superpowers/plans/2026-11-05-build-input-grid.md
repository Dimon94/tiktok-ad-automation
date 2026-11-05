# 广告搭建录入改造 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 实现已确认的搭建录入第二版设计和素材标识匹配。
**Architecture:** 复用 DraftInput.manual_link JSON 保存可选补充信息，URL 决定手动/自动取链；素材匹配从当前输入读取标识。前端复用 shadcn Dialog/Input/Table/RadioGroup，纯函数处理表格输入和映射。
**Tech Stack:** FastAPI/SQLModel/PostgreSQL、React/TypeScript/shadcn、Playwright。
**Spec:** docs/superpowers/specs/2026-11-05-build-input-grid-design.md

## Global Constraints
- 保留其他未提交改动，不触及场景并行化文件。
- 不调用真实外部写接口、不部署、不推送。
- 主按钮黑色；入口“填写更多剧目信息”；五列顺序按设计。

## Review Focus
- 标识无 URL 时仍自动取链；空标识按剧名。
- 保存、恢复、重排、取消及单行补链不得丢失标识。
- URL/ID/归因/标识必须对应正确行，不接受越界粘贴及冲突重复。
- 图片候选冻结实际匹配依据，防止候选分页混用。
- 匹配范围仍按租户/BC，字面通配符不得扩大范围。

### Task 1: 补充字段与匹配
**Files:** builds/schemas.py、manual_links.py、drafts.py、catalog.py、image_candidate_service.py；frontend/src/client（生成）。
- [x] 添加并运行失败测试：标识独立保存/恢复、无 URL、网眼条件要求、补链保留标识。
- [x] 扩展 ManualLinkInput；按 URL 判断手动取链；按 first_line 读取当前输入标识；在图片快照冻结标识；提供匹配依据展示。
- [x] 运行后端针对性回归并生成客户端。

### Task 2: 表格弹窗与布局
**Files:** manualLinks.ts、ManualLinksDialog.tsx、BuildInputPage.tsx、api.ts、LinkConfigSummary.tsx、BuildDramaTable.tsx。
- [x] 添加纯函数/页面行为回归，覆盖五列粘贴、取消、恢复、标识独立于链接、ID 前导零及超限。
- [x] 实现纯函数网格处理、详细信息弹窗；紧凑配置、显式素材类型单选、保留剧名及账户粘贴、删除说明。
- [x] 更新 demo 与既有测试中的交互和文案。

### Task 3: 验证与交付
- [x] 运行针对性前后端行为测试、前端生产构建、格式和差异检查。
- [x] 检查真实页面的桌面/窄屏布局，记录本地验证与局限。
- [x] 更新 docs/implementation-progress.md；仅明确暂存本轮文件并聚焦提交。
