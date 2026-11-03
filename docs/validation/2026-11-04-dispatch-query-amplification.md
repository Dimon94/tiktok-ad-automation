# 调度候选与成本类别查询合并

状态：隔离工作树实现与针对性验证完成，尚未发布；不改变正在进行的 a29 八槽容量窗口。

## 生产证据

2026-11-03 23:05:30 UTC 对 a29 正常调度入口做三轮 cProfile/SQL 采样，沿正常领取路径执行，无自动重放，不是只读采样。三轮真实提交 0/0/12 条，调度累计4.275秒，报表家族3.949秒；SQLAlchemy execute1064次，SQL事件捕获1063条，驱动wait约2.85秒。候选四类探测407条SQL（累计约0.912秒），cost_classes调用80次产生160条SQL（累计约0.257秒）。有80次候选任务查询和14次成本预留，部分准备未最终提交；不能用成功12条来估算所有工作量。

这些证据证明存在查询往返放大，不证明它是唯一根因。EndpointBudgets.acquire已有事务内缓存，本轮不再增加缓存。平台额度、任务页量、候选重试次数、家族预算均保持。

## 改造边界

- 报表候选：每个工作类仍按原tenant/endpoint、due、有效租约、跳过scope及槽位条件，原NULL排序各做LIMIT 1；四个有界子查询UNION ALL，一次往返后按完整(scope_id,work_class)主键回读，恢复原工作类平局顺序。没有改成全量DISTINCT/窗口排序，没有发现阶段加锁。
- 成本类别：active/inflight两组带来源标记UNION ALL；未来到期active类仍保留债务，仅inflight类仍参加记账。每次重试重新查，本事务未flush的新ledger/收据变化仍可见，不做跨轮缓存。
- 精确任务锁、整组head短锁/刷新、owner代数、共享池准入、平台占位、成本向量、outbox和最终CAS均保持。

只修改dispatcher.py、costs.py及两份回归，无DDL、业务开关、请求额度或数据清理。对应SQLAlchemy 2.0接口见[官方select/union文档](https://docs.sqlalchemy.org/en/20/core/selectable.html#sqlalchemy.sql.expression.union_all)。

## 验证

真实PG/Redis：十账户全部真实领取且token唯一，旧实现候选查询44次违反≤16预算（RED），合并后通过。成本类别旧实现2SQL违反1次预算（RED），合并后保留未来active、仅inflight、closed/expired排除、未flush收据关闭/新ledger及接口隔离。补混合实时/历史、多账户不同服务时间的回归，领取各类最早账户，冻结ledger权重不变。

控制层355项通过、4项显式/可选跳过（66.87秒）；最终针对性6项通过（5.05秒）。Ruff检查通过。初次strict检查新join表达式有两处SQLModel列类型错误，改col标记后，两源文件follow-imports=silent严格mypy通过；相同基线也通过。未把首次扩散到无关模块的574项历史strict报错称为成功。

独立审查无Critical/Important阻断。额外在生产只读REPEATABLE READ快照比较3个物理接口×3种槽余额共9组，候选ID全部一致。新SQL每组一次，计划为各子查询Limit/索引或bitmap索引访问、外层主键Index Scan，无Seq Scan；仍有与原查询相同的索引结果局部排序，不能称全程无排序。实际Execution Time 0.512–10.171ms，Shared Hit Blocks 582–8641；单样本新墙钟3.527–11.790ms，旧4.893–15.295ms，有一组新查询略慢，不能外推全局倍数或仅凭查询数量宣称完成清欠。

## 上线与验证计划

等待a29八槽30分钟窗口完整结束并按结果完成保留或恢复，不混合容量。仅推公司精确候选，保留本地main未发布provider0100和无关前端产物；同schema0099正常更新11角色、Beat最后，不执行DDL/备份/全量任务删除。上线后重新采样调度SQL/已提交grants、SDK成功页、到达/完成/净积压及资源；若吞吐仍不达标，继续按实测阻塞链处理，不称整体解决。
