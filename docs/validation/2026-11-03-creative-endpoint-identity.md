# 2026-11-03 创意目录调度端点与真实SDK对齐

## 确认的问题与证据边界

creative 的普通查询在 compile_query 中明确令 smart=False；素材子页 _supplement_page 也明确使用 ADS_OPERATIONS['ad',False]。两条已支持路径均调用 ads.get_ads，即 GET /open_api/v1.3/ad/get/。目录调度却映射 build.get_ads，即 GET /open_api/v1.3/smart_plus/ad/get/。

生产只读采样发现2230个active和44227个inactive创意头均记录在错误的SmartPlus端点。该次SmartPlus/regular一分钟实际调用分别48/26，策略均10QPS、600QPM、6在途；单点采样并未打满平台额度，不能说已经触及平台限流或认定它是唯一瓶颈。错误会使就绪检查、queued占位及公平预留参与错误端点的竞争。真实HTTP仍受regular端点准入，真实成本仍进入辅助端点收据，不是漏计真实调用或绕过平台限流。

前版c615d2f3五页量子已上线并完成11角色验收；固定16:40:27→17:10:35 UTC目录32987→34452（+1465）、报表15833→15568（-265）、未满足桶192764→185870（-6894），原四长任务仍6/1/68/4。业务统计稍后截止17:10:54，目录COMPLETE2753/STALE11，报表COMPLETE5325/CANCELLED45/FAILED1。任务减少和未满足桶下降不能替代目录完成时延验收，整体验收仍未通过。

## 修改

1. creative 主调度端点改为 ads.get_ads。所有领取在取得接口预算前核对head端点；短锁执行后再次核对，拒绝把A预算的状态写入B端点成本。
2. 消费者从本代规范head捕获原physical_endpoint作为queued scope。旧SmartPlus grant沿原身份交给实际regular发送时，既有reserve_for_send释放原queued并逐次准入regular；成本登记失败的清理也读取本代原head端点。历史ledger和冻结权重不迁移、不合并、不重算。
3. 有界数据校正器仅修复directory:creative:SMART_PLUS且仍为旧端点的head。每批最多64（函数上限256），按scope/work_class keyset，FOR UPDATE SKIP LOCKED；同时覆盖inactive及两个工作类。有效head grant或有效worker claim跳过，回绕后复查。只更新physical_endpoint，保留owner、generation、due、服务年龄、执行关系和事实。apply必须显式声明旧派发进程已隔离。

## 直接切换及故障恢复计划

普通混合滚动不安全：旧dispatcher可能读取新regular头，随后又用旧helper覆盖成SmartPlus端点写成本；新代码也不能在旧头上直接跨接口记账。按用户要求不新增兼容旧映射逻辑，采用短暂同步处理隔离：

- 保持精确c615版本完成固定观察后，再暂停本项目Jenkins并锁定发布；核对实际active_queues，五组之外不得有额外旧派发消费者。
- 先停Beat，只取消并正常排空control、ads-reporting、ads-backfill（含ads-directory）、ads-publish、ads-live-directory五组的active/reserved/scheduled。队列与outbox无需清空，不清任务；API仍按旧版接收需求。部署角色分别为worker-control、worker-ads-reporting、worker-ads-backfill、worker-ads-directory、worker-ads-live-directory。
- 五组scale0并确认零Pod后，在本地私有操作状态中持久化mapping_barrier_crossed，再更新任何新API或producer。保留原120秒退出宽限、全部配置/资源/并发及typed4/12。其他素材、搭建、管理、结果服务保持运行；调用图核验它们不直接执行目录allocator。
- 只替换11角色精确镜像，五组与Beat继续为0。用新API执行64行短事务校正；旧有效grant跳过。恢复五组新消费者，Beat最后启动，核对所有11个Pod均为新镜像、9节点/唯一Beat/schema0099/实际参数/HTTPS。
- 全角色验收后无条件再次校正并查剩余总量。分别记录head_grant、worker_claim、eligible_or_locked；空eligible批次不等于零残留，回绕重查。只有全新进程之后的新快照剩余总量为0才结束该校正并最终验收、恢复Jenkins。

屏障前失败可恢复原订阅/副本并核验旧版。屏障后新producer可能已写regular头，禁止通用旧镜像rollback；失败保持五组/Beat停止、保留锁和状态，只向前恢复同一候选。停止失败必须如实报“尚未确认全部停止”，不能冒称已隔离。回退需另行审核所有端点数据与进程的逆向切换。本次无DDL、队列清理、事实迁移、真实广告写入、新备份或恢复演练。

## 验证

错误映射与旧头跨预算领取均有回归。最初构造DirectoryQuery用了list，适配合同要求tuple；修正夹具后旧映射在真正的端点相等断言失败，修复后通过，不把夹具异常作为故障复现证据。

真实PG/Redis：旧头在预算前拒绝、发现后端点变化短锁复验、两类及inactive头校正、有效grant/worker跳过、字段不变、重复执行、独立连接行锁跳过、keyset续扫与回绕。真实gateway/官方SDK链仅替换HTTP运输，模拟旧版本签发creative SmartPlus grant，新消费者在regular端点逐页准入，旧queued清空，原主成本0/regular真实成本2，完整收据保持。

相关全组447通过、2可选压测跳过，新增realtime头夹具缺少next_eligible_at导致1失败；补齐夹具后4项通过。增加领取短锁后的端点复验后最终15项针对性回归通过。四个修改typed源模块定向mypy、所有修改源/测试Ruff与差异检查通过。代码与切换方案独立只读复核；私有切换driver已修正全角色验收后必须取得新零残留快照、停止失败输出和锁所有权保护。

## 生产结果

候选尚未发布，校正尚未执行。上线后记录实际版本、校正批次/剩余量、旧queued交接、接口调用/占位、固定旧样本与至少30分钟真实净消化；不得仅凭端点字段已正确宣布整体积压已解决。
