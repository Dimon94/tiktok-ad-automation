# 经营看板实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现可查询与导出的多租户T+1经营看板，分别预计三种新剧补贴。

**Architecture:** 经营模块复用系列日报，在后台按BC、季度分块建立不可变计算版本，原子切换查询指针。配置与归属版本独立于报表原值；读接口只做日投影聚合，导出冻结计算版本。

**Tech Stack:** FastAPI / SQLModel / PostgreSQL / Celery / Redis；React / TanStack Query与Router / shadcn/ui。

**Spec:** `docs/superpowers/specs/2026-11-04-business-dashboard-design.md`

## Global Constraints

- 保留租户、BC、账户授权边界；当前账户撤权使含其数据的旧快照不可读。
- 不查询素材指标、不显示当天；补贴仅预估，Q3/Q4三情景分开、不叠加。
- 版权方连接抽成和BC其他返点默认0%，历史按有效业务日期匹配；允许追溯重算。
- 复用现有依赖和视觉规范；本轮不推送/部署、不调用真实广告写入。
- 遵守仓库禁止新建分支要求，原地在main开发，仅暂存本任务明确文件；保留四个已有前端生成文件。原有独立工作树属于其它任务，不使用它。

## Review Focus

1. 历史回填仅覆盖查询月而未覆盖季度前缀：基础经营可用，补贴不可伪填0。
2. 局部系列/账户筛选改变剧补贴门槛：先全政策范围算，再筛选分摊份额。
3. 计算过程中费率/来源/映射变化：输入版本冻结，新旧结果不能混合发布。
4. 非USD、混合时区与账户撤权：未知保持空，权限检查不得漏过聚合/下载。
5. 同剧不同名和系列改名：稳定业务ID归属，名称只展示与搜索。

### Task 1: 纯核算合同

**Files:** Create `backend/app/modules/business/calculation.py`, `backend/app/modules/business/__init__.py`; Test `backend/tests/modules/business/test_calculation.py`。

**Interfaces:** `tier_amounts(spend: Decimal, day: date, currency: str) -> tuple[Decimal|None,...]`；`business_amounts(spend,revenue,share,rebate) -> dict[str,Decimal|None]`；`compensation_amount(spend,revenue_d0,bid,events,eligible) -> Decimal|None`；`allocate_amount(amount,weights) -> dict[str,Decimal]`。

- [ ] 先写边界测试：2000→800/500/400，10000→4000/2500/2000，2027和非USD→NULL；123.45收益×92%加100×6%=119.574；赔付严格1.2门槛、未知资格NULL；分摊和守恒、负增量。
- [ ] 执行 `uv run pytest tests/modules/business/test_calculation.py -q`，确认缺实现失败。
- [ ] 实现Decimal纯计算及中文业务注释，运行同一命令通过。
- [ ] 聚焦提交核算与测试。

### Task 2: 持久合同与配置/归属API

**Files:** Create `business/models.py`, `schemas.py`, `configuration.py`, `api.py`; Modify `app/api/main.py`, `app/models.py`; Create `app/alembic/versions/0101_business_dashboard.py`; Test `tests/modules/business/test_configuration.py`。

**Interfaces:** `save_rate(session,context,scope_id,kind,effective_date,rate)`；`save_mapping(session,context,bc_id,advertiser_id,campaign_id,drama_id)`；`request_rebuild(session,context,bc_id,start_date,end_date)`，均不私自commit。

- [ ] 写真实PG测试：默认0、日期命中、同日修订审计、费率0..1、越租户连接/剧/账户拒绝、mapping稳定ID与同名不合并。
- [ ] 执行配置测试确认功能缺失失败，再实现版本模型与Alembic迁移（0100之后），API复用require_tenant与authorized_grants。
- [ ] 表包含配置版本、映射、成员、运行/范围、版本指针、系列日投影、冻结导出；所有跨租户关系用复合外键。
- [ ] 执行测试与迁移升级/降级/升级在专用测试库验证；聚焦提交。

### Task 3: T+1后台生成与恢复

**Files:** Create `business/projection.py`, `tasks.py`, `source.py`; Modify `app/jobs/celery_app.py`, `app/jobs/tasks.py`; Test `tests/modules/business/test_projection.py`。

**Interfaces:** `advance_run(session,run_id,now) -> bool`（一批一个月/季度范围，可重试）；`schedule_due(session,now,limit) -> int`；发布版本指针为系列/剧/版权方查询统一输入。

- [ ] 写真实PG用例：初始近30日优先、季度前缀、每日完整日、三档位变化额与当日累计权重、跨季度、删除/修正来源、费率追溯、幂等重复任务、取消或失效代数不可发布。
- [ ] 来源查询只读CampaignReportDay/完整覆盖状态与业务归属；存在构建精确ID证据自动补映射，歧义留空；不得使用剧名财务认领。
- [ ] 首次已存历史分批处理；缺历史只登记对应ad_day补采，不触发material_day。定时复用ads-reporting队列，在既有Beat加入每日到期检查/恢复，短批让出worker。
- [ ] 赔付缺历史输入可条件预估但不伪造资格，保存计算缺口；完整输入才按公式计算。
- [ ] 并发锁和发布用真实PG/Redis验证，测试通过后提交。

### Task 4: 查询、明细与冻结导出

**Files:** Create `business/queries.py`, `exports.py`; Extend `business/api.py`, `schemas.py`, `tasks.py`; Test `tests/modules/business/test_queries.py`, `test_exports.py`。

**Interfaces:** `query_dashboard(session,context,filters) -> DashboardPublic`；`create_export(session,context,filters,version_id) -> ExportPublic`；详情复用同版查询按日分组。

- [ ] 测试系列/剧/版权方汇总、全筛选卡片与分页、情景分开、局部账户不改变门槛、NULL传播、币种分组与跨时区截止。
- [ ] 实现参数化SQL聚合、排序白名单、分页及所需索引，不在请求内读广告/素材事实或计算补贴。
- [ ] 导出固定版本和筛选，后台按批生成CSV，金额及三情景与列表一致，公式注入防护；创建/执行/下载重新授权，撤权不可读。
- [ ] 测试导出过程新版本发布仍导出原值；读者不可改配置；通过后提交。

### Task 5: 页面、连接费率与设置

**Files:** Create `frontend/src/features/business/{BusinessDashboard,RateSettings,MappingEditor,queries}.tsx`（查询文件用.ts）；Create `routes/_layout/tenants.$tenantId.business.tsx`; Modify `components/Sidebar/navigation.ts`, `features/providers/ConnectionPanel.tsx`; generated route/client files按项目生成；Test `frontend/tests/business-dashboard.spec.ts`。

**Interfaces:** 前端请求沿现有认证client，query key包含租户/BC/用户/筛选/版本；scope变更丢弃旧结果。后端business服务合同由Task4提供。

- [ ] 用Playwright路由边界模拟后端，先写五卡/三维/三情景/日期筛选/导出、版权方0%和管理员设置权限测试并观察失败。
- [ ] 复用shadcn卡片/表格/字段/选择/管理Sheet和全局BC切换。全部预估列保持可见，横向滚动；缺口提示独立于补贴状态。
- [ ] 版权方连接增加独立抽成设置入口，不让改费率触发版权方凭据验证；看板设置其他返点与历史重算/映射。
- [ ] 运行定向浏览器测试和项目build，检查桌面/窄屏；提交。

### Task 6: 整体验证与交付

**Files:** Update `docs/implementation-progress.md`; Create `docs/validation/2026-11-04-business-dashboard.md`。

- [ ] 全部business及直接影响模块回归、Ruff/mypy定向、前端build与浏览器测试。真实PG/Redis用于事务并发，不发真实平台写入。
- [ ] 独立整体代码审阅，重要问题以复现测试修复，再运行影响测试。
- [ ] 记录实际测试结果、迁移head、回填覆盖/缺口和未部署事实。检查暂存差异后提交；不推送。

## 执行记录

2026-11-04：用户已批准设计并要求进入开发。原地执行优先遵循仓库不创建新分支及保留其它工作树的要求，不重复索取流程性许可。设计与计划的公开默认保持一致；每项测试/提交结果追加于此及实施进度。
