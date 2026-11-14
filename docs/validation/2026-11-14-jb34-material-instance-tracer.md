# JB34 素材投放实例首切（本地候选）

基准 `1c987867971e8340ff793597b3c42c6acbb6fecc`，指定共享树 `feat/jb33-native-material-m4b`。只实施 JB34；M4b 五票尚未整体完成，JB35 须 root 阶段验收后继续。未改 Linear、父 Spec、main，未推送、部署、迁移生产或调用真实平台。

## 实现合同与消费入口

既有素材 SQL 继续负责成员、筛选、排序和 summary。构建查询的同一 RR 内按100个已选素材行读取 creative 投影及其原始事实观测坐标，一次事实读取供原素材行和实例载荷共用；原查询行 JSON 保存展示结果，不保存长期原始响应。分页改读 frozen_result。无新表、同步 owner、生命周期或业务开关。

素材合同3、`material-instance-result-v1` 与非素材 `selected-report-result-v1` 分开。实例使用完整 tenant/BC/connection/account/父Ad/素材/type/MID 身份；直接 fact keys 为原 tenant/account/creative/localday 主键的JSON编码，不增加钱的主键。原 fetched_at/sync_job_id 是实际事实坐标；source_info 描述派生覆盖，不能冒领为该事实 publisher 的完整字段证据。

首切只展开可精确归属且单 creative 的分组。原复杂行/汇总保留，未承诺多creative、冲突、轮播或五态完整支持。实例金额只取其直接投影，父对照复用原 Ad 直接查询、独立 `FULL_PARENT_AD` 范围，不计入素材合计，不计算差额。实例字段证据历史未知；收益支持、D0窗口、自然与付费组成和total成熟截止明确未确认。

现有素材详情 Sheet 的素材页直接展开当前行冻结的 material_instances / parent_controls，来源信息可展开；不再触发实时目录详情以解释该实例。CSV `INSTANCE` 和 `PARENT_CONTROL` 各保留 identity、creative_ids、fact_keys、MetricVector、coverage、observations、evidence_digest、result_digest。原导出 outbox、Worker、原子文件及当前授权检查继续负责生成下载。已完成旧文件无需重解释，待生成旧合同明确拒绝。

## 前置能力证据

|字段/能力|本地观测|官方资料或历史出处|可承诺|
|---|---|---|---|
|creative身份与spend|真实PG发布/投影；SDK transport夹具creative40、parent100|本地缓存官方 creative overview/breakdown 与 BASIC creative维度；见仓外 m4b-spec-source-notes.md 4.2|精确可映射样本的直接事实，不下摊父值|
|Native Growth creative数值|本地SDK替身返回，另有仓库11月10/11日历史验证；本轮未连平台|官方公开creative指标表未列native_growth字段；不等于平台必不支持|只展示实际观测；普遍支持 Unknown|
|D0点击窗|本地值不能证明窗口|父级BASIC点击24h定义不能外推creative|UNCONFIRMED，不能承诺creative24h|
|自然与付费|本地值不能证明流量组成|官方TTAM SOP展示语义不能外推API|UNCONFIRMED|
|total成熟截止|没有一手creative证据|未知|Unknown|
|历史字段状态|旧creative事实没有逐字段publisher evidence|现有事实/投影结构|LEGACY_UNKNOWN；JB35保存新publisher五态，不回填旧证据|

官方资料来自用户指定的既有本地缓存及源码证据（`/tmp/tkada-native-series-20261112/`），本轮没有重新访问官方或生产接口；官方支持矩阵保持 Unknown，不用本地SDK替身冒充真实平台。

## AC 对照与实际层级

|AC|本轮证据/边界|
|---|---|
|平台前置探测|上述矩阵；本地观测与官方未确认严格分开|
|代表性payload|单素材、单实例、单creative、单币种/时区；SDK→Worker→PG→browser→原Celery导出。一次payload 66,437 bytes，CSV 69,949 bytes，Worker+HTTP约95.84ms；单素材直接PG查询128.52ms。只是本地合成窄样本，代表性不足，容量/生产性能Unknown|
|40/100/40|PG断言实例40、父100、素材40；真实浏览器筛选vid1展开同值；CSV按行类型为40/100|
|同快照|UI展开消费所选行；浏览器点击原“导出”创建任务，原Celery Worker生成，HTTP下载逐行核对digest、身份和数值|
|源变化/旧合同|PG中creative40→90、目录删除后旧page载荷不变，新query新id/digest；旧合同page409，旧成品字节保持，待生成旧合同拒绝。无实时回退|
|隔离/授权|PG另租户相同remote IDs且creative700不能进入原租户40；冻结身份断言BC/connection；跨BC下载拒绝；真实HTTP撤权后query/export/download拒绝。独立BC/connection同remote ID碰撞矩阵尚未完整增加，不能声称无界隔离证明|
|原子导出|复用原生成/原子对象owner；真实Celery队列完成后下载完整CSV；未新增失败点矩阵|
|形式验证与真实链路|新增有界素材模型；真实源码破坏要求业务AssertionError并finally恢复/cmp，见下。真实浏览器/HTTP/PG/Redis、原同步Worker与真实Celery导出；仅SDKtransport替身，登录依赖仅固定本地测试操作者|

## RED、修复和验证

最初新PG检查在素材 result_contract={} 失败（明确行为RED）。修复素材冻结并消除分页/导出实时读取后GREEN。首条浏览器UI金额通过后，捕获HTML路由而非API响应造成工装JSON失败；限定真实API响应修复。随后CSV因未筛选包含原有差额行失败；按真实用户流程筛选vid1后核对实例/父对照两行，不删除产品原差额行。以上工装失败不算业务拒错证据。

所有pytest通过专用M4b guard：`python3 /tmp/tkada-native-series-20261112/m4b-implementation/run-test.py python -m pytest ...`。不使用M4a数据库，无FLUSHDB/重装依赖，工装finally退出本轮Uvicorn/Vite/Celery服务。

已运行：

- `backend/tests/modules/reporting/test_material_instance_tracer.py`：2 passed，包括真实SDK→同步Worker→浏览器点击导出→Celery→CSV（约10–13秒）。
- 原 `test_material_query.py` + `test_native_exports.py` 连同首切定向检查：12 passed（当时源码版本；后续仅本票观测与证据扩展，最终定向结果补录）。
- 原 `test_exports.py` 连同首切定向检查：10 passed。
- 前端 `bun run build`：tsc与两份Vite构建通过（实例首版；最终生成类型后重跑结果补录）。
- Ruff和diff最终结果、源码探针及最终命令见末尾补录。

旧M4a验收只作既有owner背景，没有当作素材冻结或当前完整验收。没有重跑全仓mypy历史410诊断基线；本票新增诊断须单独处理。

## JB35 精确消费与剩余边界

沿用本票JSON字段、素材合同3和原快照行/导出owner。后票扩展同一冻结载荷的多creative与特殊行、逐事实五态以及完整父级差额，不建立平行实例HTTP或冻结owner。当前实例 metric_buckets grain=creative、合同creative-observed-v1，Native Growth语义未确认；父控制自己带父MetricVector的实际provenance。不得把LEGACY_UNKNOWN升级为已验证publisher证据。

独立BC/connection完整碰撞矩阵、分页实例超过10条的厚化、复杂特殊行与五态、完整收益口径/差额属于待补证或后票范围。root需独立评估JB34隔离AC的剩余矩阵和完整MMF边界；本记录不是Done、生产验收或整个M4b已完成。

## 最终补录

- 最终相关四文件回归：**22 passed / 13.94s**（含真实素材浏览器点击导出及原Celery下载核对）。其后将父控制筛选改为全新原ReportingFilter，避免继承任何素材子集约束；对应PG首切再次 **1 passed / 0.64s**。原 Campaign SDK/Worker/HTTP/浏览器工装回归 **1 passed / 7.58s**，未改非素材行为。
- 最终生成客户端类型后 `bun run build` **exit0**（tsc + 生产/演示双Vite）；构建日志仓外 `/tmp/jb34-final-build.log`。Ruff全部修改Python产品/测试文件 **All checks passed**，`git diff --check`通过。Python导入、真实PG执行同时覆盖语法；没有重跑全仓mypy或宣称其全绿。
- `material.py` **32 cases passed**；`--broken-parent` / `--broken-scope` / `--broken-freeze` 分别预期AssertionError RED。
- `material_probes.py` 四个真实源码探针分别得到业务断言RED：父值借入、BC身份改写、移除creative SQL tenant筛选、分页从冻结读改为实时重算。每个探针仓外备份、finally恢复、执行cmp并再次PG GREEN。跨租户探针由相同remote IDs的另一租户700触发观测数量断言，不是SQL/import错误。pytest重写assert会显示 `E assert` 而非字面AssertionError；工装已按实际业务断言形式识别，未减弱业务期望。
- 隔离AC的最小“两个租户同remote IDs”路径已实测满足；额外BC/connection组合是更广覆盖的限制说明，不冒认为全组合证明。当前没有已知未闭合的首条单实例业务断言；官方语义、生产容量仍Unknown，root独立验收未执行。
- 本轮没有新增迁移；原query15min/export24h owner未变。测试服务均由原harness finally关闭，没有清理他人进程、M4a库或共享依赖。原未跟踪`.venv`、`node_modules`软链保留。
