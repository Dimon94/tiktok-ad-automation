# 补链重叠需求导致历史报表回滚

状态：本地修复与针对性验证完成，准备精确同schema快修。

## 根因与修复

生产e211844c的5个历史material_day已收到成功HTTP响应，发布事实后创建material_links时触发uq_sync_execution_report_job_id，整步回滚；采样可见同一任务约4分钟重复。只读核验冲突父为QUEUED/gen0、无token/claimed/observed、两个PENDING日期桶。

existing只保留最新父；最新父已启动且冻结revision落后时，新需求进missing。missing不意味着所有父都无该子桶，较老严格未启动父可能被相邻合并选中，原逻辑无条件INSERT撞唯一键。

现在只对已短锁且严格未启动的合并父批量读取全部引用，检查租户、scope、owner、PENDING、generation0、无观察/发布版本证据；不符合即保留原父另建执行，判断在扩日期/重置progress之前。合法已有子桶只更新冻结revision/requested_after并保留ID，缺失日期才INSERT。已启动新父及其引用不变，无全局ON CONFLICT吞错、需求虚假满足或事实删除。

## 验证与发布范围

真实双PG连接通过锁住旧父、规范enqueue创建第二父，释放后令第二父已启动，再请求重叠+新增日期，先红复现UniqueViolation。修复后44项相关回归通过（8.28秒），包括七种PENDING/终态/代数/观察/发布版本/owner边界、childID保留、只新增缺失桶、已启动父不变、原需求及授权围栏。核心严格mypy及Ruff通过；独立审查无Critical/Important。

以实际e211844c/schema0101为基线隔离精确候选，仅本修复及前一轮4行测试时钟更正；不夹带其他会话业务改动。无迁移/配置/平台额度/资源/四开关变化，后台实际8与typed8/prefetch12保持。按已授权同schema快修，持独占锁暂停本项目CI、保留正常退出/原策略/租约保护、后台正常排空零Pod切换、Beat最后；验收11角色/9Celery/唯一Beat/HTTPS及实际配置。无新备份/恢复演练。上线后核验原失败任务真实提交、唯一键冲突及接口利用率，未验收前不宣称完成。
