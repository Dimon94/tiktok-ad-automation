# 后台同步容量与进程内存联合验证

## 已知事实与范围

a29fef9c 已解决原四条长期分页目录不能完成的问题，四条均有真实发布版本。但固定 30 分钟目录积压增加 991，报表增加 50，整体清欠尚未达标。随后相同 2CPU/3Gi 配额的六槽试验在第 24 分钟后 RSS 达到 2778.87MiB，触发 2.7Gi 保护线；无 OOM/重启，已正常排空恢复实际 4、typed 4、prefetch 12，并完成全角色验收、释放锁、恢复 Jenkins。该试验不是成功的 30 分钟验证。

六槽相近调度相位前 12 分钟 SDK 成功读取吞吐只改善约 9.5%，不同任务混合不可归因成纯并发收益。六槽并非持续满载，不能宣称并发是唯一根因。全局队列尚需验证调度补位与任务成本。

## 本轮假设与配置

验证单后台消费者的实际容量受到执行槽与进程内存预算共同限制。只调整 worker-ads-backfill 的资源/进程配置，使用同一 a29fef9c 镜像：实际与 typed 同步 4→8，prefetch 12 保持；request 250m/1536Mi→1CPU/3Gi，limit 2CPU/3Gi→3CPU/6Gi；增加 prefork 参数 `--max-memory-per-child 512000`。不改其他角色、平台额度、业务开关、数据库结构、事实或合法任务。不新增集群节点/云服务，不购买机器。试验只使用预检中已核验可调度且实测余量最大的既有节点，临时增加该节点 hostname selector；恢复时连同原调度模板完整恢复。正式保留配置时需要重新审查可调度容量，不能把试验定位当作永久节点依赖。

这是联合资源配置验证，不能作为相同资源并发翻倍的性能结论。已有六槽 CPU 峰值约 1.52 核、RSS 2.714Gi；线性估算八槽约 2.03 核和 3.62Gi，仅用于配置预估，不能代替实测。每进程数据库池 2+1，八槽最多 24 连接，比四槽增加 12；变更前核对实时连接和预留。

Celery 官方说明 Python 子进程内存高水位可能保留至进程退出，`worker_max_memory_per_child` 单位为 1024 字节；超过限额时完成当前任务后更换子进程。该设置不硬性限制单次任务内存，也不能把子进程正常替换报告为容器崩溃或任务中止。参考：[内存说明](https://docs.celeryq.dev/en/stable/userguide/optimizing.html#memory-usage)、[配置定义](https://docs.celeryq.dev/en/stable/userguide/configuration.html#worker-max-memory-per-child)。

## 执行与验收

私有脚本 capacity45/change.py、restore-partial.py 和 watch-resources.py。先只读检查 11 角色同版本、9 Celery 节点、现有 4/4/12、节点可调度请求及实测余量、PG 连接和服务端 dry-run。暂停本项目 Jenkins，持有发布锁，只 cancel 目标两队列；active/reserved/scheduled/active_queues 连续两次为空，正常缩容至目标 Pod 为零后，才使用既有离线 configure_background_pool 更新 typed 容量并保留 fairness。用 resourceVersion 和完整模板校验仅修改目标命令/资源，正常启动后实查八进程与 typed 8/12。

实际八槽稳定后新建完整 30 分钟窗口，每三分钟记录目录/报表积压、需求未覆盖数、固定旧任务、真实创建和完成量。每 15 秒记录 Pod 内存/CPU/重启及当前宿主机内存；RSS>5.4Gi、OOM、重启、MemoryPressure 或当前节点实测剩余<1Gi 时立即结束试验并正常恢复原四槽及原资源/命令。监控缺失不能当作零：连续三次采样失败须中止并恢复，Pod/节点 metrics 超过 90 秒视为失败。子进程正常回收瞬间允许实际进程数为零，配置并发仍须为 8；有界等待补齐后才启动窗口，恢复排空不要求八槽先健康。对照真实 SDK 成功页数、平台限流、本地拒绝和补位利用率，不能只报进程数。

失败或收益不足：正常排空、零 Pod 后 typed 恢复 4/12，完整恢复原 2CPU/3Gi 模板；全 11 角色验收后释放锁、恢复 Jenkins。任一 apply 中间态可走独立 restore-partial，不能盲目重放 apply。试验成功也不能只保留线上漂移，须把明确配置和离线 typed 配置要求写回清单/手册并完成精确版本发布验收。

## 状态

a29八槽完整30分钟验证已完成，有净消化和旧样本完成；八槽配置已随cd09faed正式上线并全域验收。新版本独立效果窗口进行中，尚未声明整体清欠完成。


## 实际窗口与保留决定

2026-11-03 23:05:56.401400→23:36:18.024410 UTC，期间同a29应用、实际8/typed8/12、request1CPU/3Gi、limit3CPU/6Gi、子进程任务后回收及临时既有节点selector保持。目录27953→27185（-768）、报表16958→16785（-173）、未满足桶134932→131736（-3196）。原四长任务均已发布；固定32从15 COMPLETE/17 RUNNING变为24 COMPLETE/8 RUNNING，11条页/代数/状态有变化。不是清空全部积压。

新增目录2780，真实COMPLETE3516，其他STALE/CANCELLED等32（2780-3516-32=-768）；报表真实COMPLETE5064、其他终态55。74个有效资源样本、缺失0，RSS峰2526.45MiB，无OOM/容器重启。不同窗口到达量/任务混合变化，不能把净下降全部归因于容量或承诺吞吐翻倍。

保留实际及typed8/12，配额不变；正式清单将request提高到1CPU/6Gi、limit仍3CPU/6Gi，给整个Pod上限预留可调度内存，移除试验单节点selector，BG改Recreate防止新旧八槽叠加。该request变化不增加进程/HTTP额度或Pod内存上限。变更前无pin预检：现有8节点，所有可能满足6Gi请求的落点（含移除旧BG后重新可调度节点）实测余量≥6Gi；PG117/600、active12、lock0，指标时效和serverdryrun通过，无新增节点/云资源。发布仍正常取消消费/连续两次空/零BG Pod，typed保持8/12，精确镜像和全部模板/策略全域验收。

代码候选的查询合并另见[记录](2026-11-04-dispatch-query-amplification.md)。容量45完整验收后将CI暂停的原始enabled状态交接到下一次发布，期间不重新启用CI；下一版本11角色全部验收、解锁后恢复，不能覆盖为disabled并遗留。


正式配置已随cd09faed全角色验收：实际8/typed8/12、request1CPU/6Gi、limit3CPU/6Gi、Recreate/任务后子进程回收/无node pin。正常BG排空、零Pod、精确模板/策略/版本与配置验收均完成，锁释放，CI恢复enabled无active/queued。新版本固定窗口23:52:21UTC起，不能把a29的先前净下降当成新代码的对照结果。
