# 后台目录完成调度实施计划

> **For agentic workers:** 使用superpowers:executing-plans，由当前执行者逐任务实施并独立复核；用户已授权直接实施，不增加确认或新分支。

**Goal:** 使最老后台目录真实完成，同时保证新需求继续服务，各物理接口可以独立推进。

**Architecture:** 维护当前有效需求年龄，以每租户/接口独立policy分配后台完成及普通机会。现有canonical新代、额度、成本和原子发布保持。

**Tech Stack:** Python、SQLModel/PostgreSQL18、Redis/Celery、官方SDK；不增加依赖。

**Spec:** [设计](../specs/2026-11-04-directory-completion-design.md)。设计独立审查通过后执行，约束及接口以最终审查版为准。

## Global Constraints

- 生产b32c5397/schema0099；后台4/typed4/prefetch12，不改额度、资源、四开关或任务正文。
- 无DDL、事实删除、真实广告写入；旧页/新代/权限及成本围栏必须保持。
- 固定普通份额非零；2:1是后台读取机会，不是HTTP成本份额。
- 独立工作树继续使用`.worktrees/sync-query-redesign`，只推公司候选，保留main的provider0100。

## Review Focus

- 重新唤醒的scope不能继承已完成历史年龄；Task1回归重开/覆盖/旧owner。
- 单接口锁不能遮住其他接口；Task3双连接锁竞争和真实Redis准入回归。
- 不可执行前缀超过四页仍须跨调用前进；Task3验证发现与SQL回滚。
- 实时/发布不能消耗后台普通机会；Task2/3单独断言policy推进。
- 多账户和持续新到任务不能被大目录全部占用；Task4验证实际发布及普通份额。

### Task1：当前目录年龄与存量纠正

**Files:** 修改`backend/app/modules/sync_control/directory_demand.py`；新增目录年龄维护/短批修正模块及`backend/tests/modules/sync_control/test_directory_pending_age.py`。

**Interfaces:** `refresh_directory_pending_age(session, heads)`维护固定head集合；`repair_directory_pending_ages(session, after, limit=64)`只返回/修正metadata短批，不改due/lease/owner。

- [x] 旧实现下写出结束后重开、部分覆盖、owner换代、下一旧run前移的失败测试。
- [x] 实现同事务维护，测试短锁跳过、keyset、幂等；登记前后事实字段保持。
- [x] 只读生产执行计划及小批耗时验证；不先执行数据修正。

### Task2：接口policy的独立命名空间

**Files:** 修改`backend/app/modules/sync_control/dispatch_budget.py`；新增`backend/app/modules/sync_control/directory_policy.py`及对应PG/Redis测试。

**Interfaces:** `EndpointBudgets.peek_directory_policy(tenant_id, endpoint)`只读本批shadow或库；`directory_policy(tenant_id, endpoint)`在取得原接口锁后返回可变policy。`persist()`同时CAS成本和policy根键。

- [x] 回归接口间合并、同接口冲突、回滚、policy-only扫描进度提交，成本四键算法不变。
- [x] 实现固定字段状态、后台机会推进、单调接口机会时间、账户交替和扫描重置；实时/发布不推进turn。

### Task3：接口间发现与后台完成机会

**Files:** 修改`backend/app/modules/sync_control/directory_dispatch.py`；提取候选发现到`directory_candidates.py`，新增调度行为测试，更新受影响的原有私有接口测试。

**Interfaces:** 候选发现输入显式tenant/endpoint/class/模式与policy快照，返回有界候选及扫描后位置；dispatcher仍负责短锁复验、真实准入及canonical领取。

- [x] 先写首接口预算被另一连接持有、第二接口可领取的RED测试。
- [x] 实现有界接口机会轮转和忙接口fallback，普通候选保留原服务年龄排序及安全游标协议。
- [x] 写超过256不可执行头、退避/租约恢复、排除账户LIMIT前过滤、普通游标不被优先成功推进的RED测试并修复。
- [x] 保持5页/45秒、无HTTP跨事务、旧grant/gen/head/owner拒绝，运行现有针对性组。

### Task4：规模回归、审查与发布

**Files:** 新增真实PG/Redis/SDK运输边界规模场景；更新验证、生产手册、实施进度与HTML。

- [x] 千级候选头、至少三账户、多量子老快照、持续新任务；断言真实发布与普通机会，不用人工样本优先。
- [x] 相关全组、Ruff/格式、差异及类型检查与基线对比；独立复核设计及最终代码/发布方案。
- [ ] 聚焦提交、本地合并保留provider0100、公司候选推送；按精确镜像同schema方案上线。
- [ ] 全新角色验收后短批纠正active目录年龄，回绕检查，记录无事实/租约变化；恢复CI与释放锁。
- [ ] 固定版本至少30分钟，原四/32样本、到达/完成/普通服务/净欠账及资源共同验收；无证据不报全部解决。

补充裁定：有效执行排除已覆盖frozen版本后，必须同时有界收尾这些旧目录，否则形成永久PENDING。复用Control清理入口增加covered_directories类别，按PENDING索引keyset扫描；仅真实水位完整覆盖、全部当前owner且无worker/head有效租约的目录取消并沿原outcome收尾。锁跳过/不确定保留，不删除事实、不推进水位、不声称COMPLETE。\n