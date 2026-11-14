# JB38 / M4B-05 导出生命周期候选

起点 `6bcf1addccb659ee98414e9331d76c36d2d2883c`，唯一可写产品树为 `projects/tiktok-ad-automation.wt/feat-jb33-native-material-m4b`，分支 `feat/jb33-native-material-m4b`。用户指定 Sol/high、一个写者，无子代理。原 `.venv` / `node_modules` 链接保留。未创建分支/checkout，未写 main、外层仓库、Linear、平台、推送、PR、部署或补写源事实。

## 前置与实现

亲跑 JB36 最终 metrics + tracer，13 passed /15.58s（`/tmp/jb38-preflight.log`）：真实浏览器→HTTP→PG/Redis→原同步/导出 Worker，完整 contract3 / `material-instance-result-v1`、五类 CSV。依赖阶段接受沿用 root 的 JB36/JB37 记录，tracker 仍 In Progress，不变更图。

保持原 ReportingExport、状态机、outbox、Celery、对象存储及24小时TTL，无迁移、新 exporter、重试/恢复/TTL 系统。生成仅消费任务自有 frozen_rows/result_contract/publication_map/coverage；不需要源 QuerySnapshot 存活。

- 原 Worker 在领取及发布事务使用原任务 PG 行锁；重复消息等待后重读 COMPLETE，不重复写对象。失败处理也锁同一行，不覆盖已经成功的并发结果。
- 原 generate_export 写前和写后重建当前授权；写后重新读取授权行和到期时间，再开放 object_key/COMPLETE。对象写成功后抛错也删除确定性任务键；失败事务不能发布部分文件。跨写入到期保持 EXPIRED。
- 原创建入口对幂等候选加行锁。FAILED 重试复用原 PendingDispatch 身份，清 published_at、更新 available_at 并转 QUEUED；重复请求复用同一行。保持原 snapshot、冻结结果、合同、消息正文和 expires_at。
- 已完成旧合同文件走下载原字节，Worker COMPLETE 不重新解释；不兼容旧待生成合同沿原 owner 拒绝。未修改 CSV 算术、父级归属、查询合同、UI 或平台字段语义。

## RED、工装修正与真实边界

`/tmp/jb38-red.log` 首轮复现写后遗留文件、跨写入到期/撤权仍 COMPLETE。随后给夹具加“必须有实例且消耗40”的断言，发现独立查询 Worker 看不到尚未提交的投影，前轮生成的是空表头。没有把该轮计为有效 payload 验收。修正为先完成原 projector 并提交，再经原查询构建；后续70,510字节真实实例及最终全行 payload 结果替代首轮。

`/tmp/jb38-retry-red.log` 真实 HTTP 重试原 FAILED 导出仍返回 FAILED，原已投递消息未重新排队。补原 outbox 重用后，同一 HTTP 请求两次均 QUEUED，只有一个任务和一个原消息，冻结三元组未改变。

存储故障注入在实际 `os.replace` 执行前/执行后；真实本地对象目录、PG和原 Worker，不替换业务 service/repository/browser 响应。时间跨越通过时间边界替身，三种既有状态到期用合法 PG 时间坐标；未等待24小时。SDK 是平台唯一 transport 替身。本轮对象存储是原磁盘 fallback，未调用远端 S3；远端 S3 故障/生产容量不在证明内。

## 本地验收与证据映射

|验收|本轮证据与范围|
|---|---|
|最终接口/容量前置|上述13项真实完整浏览器链；最终追加同一全行 payload 的写前/写后故障和原Redis/Celery重试|
|失败/重试|test_material_export_lifecycle 的实际存储故障，FAILED/无键/磁盘无文件/下载拒绝；同任务重试逐字节等于原冻结意图，记录任务/文件键/sha256|
|重复消息/请求|三次原 run_export_task 消息、三次幂等创建与下载；两连接并发原 Worker 只写一次；HTTP两次FAILED重试复用一个原outbox消息|
|冻结结果|源40→90、删除原snapshot后生成仍40；生成后目录改名、删除creative事实/creative/目录引用/广告对象，重复下载仍原字节。每次核对 frozen_rows/result_contract/publication_map|
|阶段权限|创建、生成、重试、下载分别独立撤账户授权；另在实际写入期间撤权，不能 COMPLETE。生成阶段重建 User/Tenant/Membership 与 BC/账户授权|
|范围/键|另一真实tenant同BC/账户/素材/creative远端ID不能get/download原文件；相同tenant已授权另一operator、另一BC也不能读取。相同幂等字符串在另一tenant新建不同任务。有效原storage key不能绕过scope检查|
|到期|QUEUED/RUNNING/COMPLETE过期均EXPIRED、无键/文件、下载409；写入跨到期同样不发布；原15分钟snapshot删除不影响有效导出|
|旧合同|继承JB37旧material合同与成品保护；本轮重跑test_native_exports旧完成/待生成原Worker合同回归，不把旧pending重解释|
|形式化|64有界组合，live/partial/scope/expiry破坏模型均AssertionError；实际源码拒错及外部恢复/cmp，见下|

## 整个 M4b T1–T12 / R1–R10 支持图

“继承”只指源绑定未变且先前root接受的证据；不声称本轮重新执行全部。独立fresh Standards/Spec与整个MMF接受由root负责。

|项|owner|证据来源|限制|
|---|---|---|---|
|T1 查询完成|ReportingData|继承JB34/36冻结；本轮完整tracer复跑|合成本地观测|
|T2 查询失败|ReportingData/page_cache|继承JB37三类PG执行错误/无半批|本轮未再注入PG故障|
|T3 失败重查|ReportingData/AdsWorkspace|继承JB37浏览器错误/重查|无生产运行证明|
|T4 查询到期|ReportingData|继承JB37 page/detail/export409|保存时间坐标，非实等15分钟|
|T5 请求导出|ReportingExport|本轮浏览器/HTTP创建及独立撤权|本地认证fixture|
|T6 文件完成|ReportingExport/object store|本轮五类CSV、实际写入、PG COMPLETE/HTTP字节|本地磁盘fallback|
|T7 文件失败|ReportingExport|本轮写前/写后故障，无键/文件，HTTP409|未注入S3服务|
|T8 原导出重试|ReportingExport/outbox|本轮真实HTTP原消息rearm、原Worker及Redis/Celery重试|非全系统恢复证明|
|T9 重复下载|ReportingExport/grants|本轮原字节、源更新/删除不变、撤权拒绝|当前授权在读取前检查|
|T10 成品到期|ReportingExport/cleanup|本轮EXPIRED拒绝/删除、既有cleanup回归|保存时钟坐标|
|T11 未完成到期|ReportingExport|本轮QUEUED/RUNNING及跨写入到期|有限时序|
|T12 到期重查|ReportingData|继承JB37新查询新ID/原值不覆盖|不等于平台重新取数|
|R1 完整身份范围|MaterialPerformance/Directory/Data|继承JB34/35身份模型/源探针；本轮同remoteID导出隔离|有限tenant/BC样本|
|R2 直接creative金额|MaterialPerformance/Sync|继承JB36独立父100/子40；本轮CSV复跑|平台creative支持未确认|
|R3 去重/特殊桶|MaterialPerformance|继承JB35/36；本轮完整五类CSV/父控制去重|非任意图证明|
|R4 五态/证据|ReportingSync/MaterialPerformance|继承JB35五态；本轮tracer/CSV原证据|历史缺证仍LEGACY_UNKNOWN|
|R5 同冻结观测|ReportingData/Export|本轮snapshot删除、源更新/删除、原字节与源码live探针|假设已提交捕获正确，捕获原子性继承JB37|
|R6 Decimal/桶/比率|MetricCatalog/MaterialPerformance|继承JB36完整分母/币种/时区；本轮metrics前置与CSV回归|D0/traffic/maturity未确认|
|R7 父控制独立|MaterialPerformance/Data|继承JB36完整父/子集拒算；本轮五类CSV|非平台差额保证|
|R8 目录显示/身份|MaterialPerformance/Directory|继承JB35删除/冲突；本轮成品源删除不变|不自动补链|
|R9 当前权限/幂等|ReportingExport/grants/outbox|本轮各阶段撤权/并发/重复/跨域/原消息rearm|不证明任意并发和外部服务|
|R10 CSV行粒度/完整|ReportingExport|本轮全行浏览器→原Worker→CSV、写前/后失败、字节/digest|合成单机样本|

## Unknown 与 root 审阅包

creativeNativeGrowth 官方支持、D0、traffic、total maturity 继续 Unknown/UNCONFIRMED。父值不分摊，未做源数据回填。低负载合成样本只能说明该 payload 可处理；无租户分布、生产并发/P95/容量上限承诺。

审阅重点：原 owner 的发布锁、对象副作用与PG事务边界、写后当前授权/到期重读、FAILED原消息重排、完成旧合同短路；结合本文件矩阵与JB36/JB37文档做整个MMF两轴审查。实现候选不等于root最终接受，不关闭票、不集成main。

## 最终命令、实物与结果

以下pytest/Ruff命令均加前缀 `python3 /tmp/tkada-native-series-20261112/m4b-implementation/run-test.py`，从指定产品root串行执行，专用loopbackPG/Redis及既有Python3.14。未FLUSHDB、并发fixture suite、重装依赖或终止其他进程。

- `python -m pytest backend/tests/modules/reporting/test_material_metrics.py backend/tests/modules/reporting/test_material_instance_tracer.py -q -s --tb=short`：前置13 passed /15.58s。
- `python -m pytest backend/tests/modules/reporting/test_material_export_lifecycle.py backend/tests/modules/reporting/test_native_exports.py backend/tests/modules/reporting/test_exports.py backend/tests/modules/reporting/test_material_instance_tracer.py -q -s --tb=short`：32 passed，唯一浏览器工装FAILED轮询失败（`/tmp/jb38-final.log`）。原Worker异步消息尚未消费时，工装立即把此前故障留下的FAILED认作本次终态。改为先经真实HTTP重试转QUEUED，再消费原消息；未改业务成功预期。
- `python -m pytest backend/tests/modules/reporting/test_material_instance_tracer.py -k workspace_sdk_worker_csv -q -s --tb=short`：最终1 passed /14.93s，13条既有弃用警告，取代上述工装失败。未修改UI，沿用JB37前端构建证据，不重跑无关构建或旧mypy基线。
- 最终完整快照 `729d977e-2e21-46f5-8548-40b2dba46b37`：5行、4实例、3份展开父控制（CSV去重）、4份对账、publication_map 5坐标。digest详见 `/tmp/jb38-browser-final.log`。
- 导出任务 `7c34951d-7556-4d2a-a341-2ff98ebe24a2`，确定性对象键遵循 `report-exports/tenants/<synthetic-tenant>/<BC>/<task>.csv`。CSV **357311 bytes**，SHA256 **78df6695657f2886e01386b8b095c9e50cdc49d8db9fa99290ec14d1f0bc238b**；冻结payload **437107 bytes**，本轮最后一次Worker+HTTP **135.67ms**（不含此前故障轮总耗时）。五类逐行身份、字段状态、observations、evidence_digest、result_digest、父100/子40均经原harness断言；两次真实存储故障后HTTP不可读，HTTP重试→Redis→原Celery生成精确意图字节，重复下载相同。
- 窄实例样本70510 bytes；任务 `3716f493-3465-489d-a6e6-772f3c0eab9f`，digest `c9136b84ba06c1bd9d6469e70ce5c7183cbab8803e03a57ad30d92a8081bc3a1`（同文件重试比对，不跨不同随机tenant比较digest）。
- `python -m ruff check --fix` 及 `python -m ruff format` 仅针对本轮四个产品/测试Python文件，修正工装imports与格式；最终检查结果补在下段。无环境文件和凭证入库。
- `python .agents/skills/formal-verify-reporting-data/scripts/material_export.py`：64组合GREEN；分别追加 `--broken-live`、`--broken-partial`、`--broken-scope`、`--broken-expiry` 均exit1/AssertionError。上述模型命令由guarded runner的Python子进程串行调用。
- `python3 .agents/skills/formal-verify-reporting-data/scripts/material_export_probes.py`：实际测试子进程都经guarded runner。原四探针全部有效RED→外部备份finally恢复/cmp→14项GREEN。首次scope拒错是正确的HTTP“应拒绝却未拒绝”，probe工装原只认AssertionError而漏认pytest DID NOT RAISE；扩大为同样明确的业务拒绝失败判据，没有改变产品或成功预期。补原消息rearm源码探针后重新执行最终五探针，结果见下段。

模型假设：两个冻结/实时金额token、有限授权/时钟/写前写后故障组合、对象put原子、已提交且正确捕获的payload。源绑定为原 `_export_rows`、`generate_export`、`_scope_check`、原Worker锁/失败落账和原HTTP/outbox。模型不证明PG/Redis/S3实现、无界并发、故障恢复活性或生产容量；实际PG、Redis、HTTP、磁盘和Worker证据独立列出。

最终补证：`/tmp/jb38-probes-final2.log` 五个实际源码近似错误（retry/partial/scope/expiry/live）全部业务RED，每次finally恢复/cmp后15项GREEN（4.27/3.32/3.35/3.67/3.76s）。恢复后的四个产品/测试文件Ruff、六个Python文件py_compile和diff check通过。

真实Redis重复消息补证在原harness使用同一个原PendingDispatch task_id发送三次，不新建业务任务。`/tmp/jb38-queue-final.log` 新快照 `1ee20f05-3154-44f4-a696-44487d377622`，任务 `73146308-9dea-4beb-8a60-778943f614c8`；357311 bytes，sha256 `85bfaf3955d862a95cc39f8cdfd404798bf64fd9d74de9e3dc5ecc594a171d09`，437107 bytes payload，最后一次Worker+HTTP142.05ms。不同随机tenant/观测自然产生不同文件digest；每个任务均只与自己原冻结字节比较。并发单次物理写断言在独立两连接测试，不能把该HTTP耗时外推成生产吞吐。原消息重排由HTTP+PG验证，harness沿原约定从PendingDispatch读取消息发给真实broker，未运行全局flush_dispatch公平扫描或声明验证所有outbox调度策略。

最终重复队列纵切：1 passed /13.11s、13条既有弃用警告；完整五类结果、故障恢复和字节校验通过。除原声明的外部平台/S3/生产容量与root整MMF审查边界外，本票本地AC无未闭合项。
