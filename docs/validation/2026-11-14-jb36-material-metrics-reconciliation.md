# JB36 / M4B-03 本地实现候选

起点 `8a743863b990d7c31f4f98b5ca7ee31b9b302b94`，分支 `feat/jb33-native-material-m4b`。仅指定共享产品树，一名写者、没有子代理。保留 `.venv` / `node_modules` 原软链。没有平台调用、远端写入、Linear 写入、推送、PR、main 合并或部署；没有新增迁移、开关、同步/映射/资格/导出 owner。JB35 的身份与 publisher 五态证据直接消费，不回填历史。

## 前置口径矩阵

本轮读取已保存的 source notes、JB34/JB35 验证记录、官方 followup 汇总及历史 11月10日 creative 验证。notes 中三个简写 creative 缓存路径已不存在；实际一手缓存索引位于 `/tmp/tkada-native-series-20261112/m4a-official-report-contract-followup.md` 的 S8–S12（完整标题文件名），不是新的平台请求。

|项|一手资料及观测边界|产品状态|
|---|---|---|
|creative Native Growth 支持|公开 creative 指标表没有 native_growth；历史选定 BASIC creative 样本曾有数值，不代表所有对象支持|`creative-observed-v1` 仅为本地直接观测合同，支持范围 Unknown|
|D0 点击24小时|父级 BASIC 有定义，creative 没有同等定义证据|UNCONFIRMED，click_hours=null|
|自然/付费组成|TTAM SOP 的展示语义不能外推 creative API|UNCONFIRMED|
|total 成熟截止|没有可冻结的 creative 一手合同|Unknown|
|历史缺失/删除|JB35 已保留 legacy、缺失、冲突；本票不制造 publisher 或映射|保持五态和身份原因|

MetricCatalog 在原 `semantics_for` 中接受显式 creative/material_instance grain 与 canonical observed revision；未知合同仍拒绝解释。快照合同冻结逐指标语义。Native Growth 收益和事件不参与数值差额；基础 spend/impressions/clicks 仅在其直接字段、日期、币种、完整范围和发布覆盖可证明一致时对比。不把未确认当成不支持。

## 实现与下游合同

- 现有 material_query 的行和实例使用 Decimal、严格完整分母和 ratio-of-sums。已知部分收益仍除以全部已知完整消耗；任何分母字段缺项禁止比率，未知不补零。
- 原 frozen_result 保存完整 ReportRow（实例、逐事实 values/states/publication/identity、父控制、reconciliation_rows 和 digest）。summary 从 `INSTANCE` 汇总，标 `SELECTED_MATERIALS`；特殊行、父控制和差额不进入普通实例汇总。
- 父控制通过新的干净父 Ad/date/account filter 和显式成员独立读取，仍是 `FULL_PARENT_AD`。完整 creative 范围和原选中范围另从 canonical read_facts 取得，不受当前页限制。CSV 按父身份去重，按 bucket 展开币种/时区。
- reconciliation_rows 保留父身份、fact_keys、双方 observations、父 digest、coverage、逐指标原因与自身 evidence_digest。完整覆盖需原 parent direct provenance、原 creative ACCOUNT_DAY publisher、同账户本地日期、同币种/时区、已知合同；时间兼容沿现有投影的30分钟窗口。不满足即 NOT_COMPARABLE 且没有指标桶。可比基础指标按完整父值减完整 creative 求差，Native Growth 留空并解释；非可加比率永不由差额重算。
- 原广告组 residual 保留 `ADGROUP_RESIDUAL` 和原投影向量作为带来源的 observations，标 NOT_COMPARABLE，不冒充父级差额或实例金额。
- 原 export Worker 只读冻结结果，CSV 最终五类 INSTANCE/PARENT_CONTROL/UNMAPPED_CREATIVE/COMPOSITE/RECONCILIATION_DIFFERENCE，不再输出临时 SUMMARY。identity、observations、五态、coverage/比较状态、指标向量、evidence/result digest 随文件保存；未知数值空白，状态/原因不丢失。
- 原素材 Sheet 展示原始收益、ROAS 及 availability、完整父范围、比较状态与具体原因；大量证据折叠。客户端由真实 OpenAPI 再生，沿原脚本规则去除生成器空白行空格。

JB37 消费素材 contract3 / `material-instance-result-v1`，包含新增 metrics 合同映射和 ReportRow.reconciliation_rows；必须把全部 frozen row、summary、publication_map、parent 和差额视为一个 RR 查询结果，继续负责原子失败、重查、旧合同和15分钟生命周期。不得用实时源补齐冻结缺项。

JB38 消费同一 export.frozen_rows/result_contract/publication_map。五行类型均已在原 `_export_rows` / `_csv_rows` 内完成；parent 和 reconciliation 去重只影响文件呈现，不重读源。继续负责权限阶段、幂等/故障恢复、部分文件不可见和原24小时到期；旧成品和旧待生成合同边界由该票验收。

## AC 与实际证据

|JB36 AC|本轮证据|
|---|---|
|02 接口/五态/身份|原 tracer 的多creative、五态、特殊桶、BC连接冲突、源变更冻结检查重新运行；没有改写身份 owner|
|口径探测|上述矩阵，实际缓存与历史验证；无本轮平台支持证明|
|金额/分母|真实PG `test_material_metrics.py`：2/8与1/8 => .9；第二收益缺失 => .1 PARTIAL；全缺失、缺分母、零分母 => MISSING|
|币种/时区|真实PG两账户、USD/EUR、Hong_Kong/New_York 分桶；同币种跨时区用原聚合 owner 得到MIXED，实例保留本地时区；没有新增禁查规则|
|父未就绪/子集|PG FAILED父覆盖和vid1子集均 NOT_COMPARABLE、空指标桶；子集父控制仍为完整10|
|真正完整对账|真实publisher分别发布完整parent10和creative2+8，基础消耗差额0；收益合同未知仍无数字；summary10，不加父10|
|五类CSV|真实浏览器打开vid1、展开实例/父控制、显示9/40=.225、未确认与不可比原因、点击原导出；浏览器清空筛选、取得全行快照并点击原导出，原Celery Worker生成CSV；真实HTTP仅补充全行接口断言。文件五类逐行身份/证据/digest核对，父控制去重一次；不可比差额无金额|
|冻结稳定|原tracer查询后creative更新/目录删除、旧分页和新旧digest核对在本轮重新通过；完整差额同时包含在原始冻结对象比较中|
|容量|见下，合成样本不足，生产性能/容量Unknown|
|形式化/源码绑定|32有界组合，4种破坏模型均RED；4个真实源码探针均业务断言RED、finally恢复/cmp、PG GREEN|

最高纵切使用原 live_campaign_workspace + Playwright + native_export_harness，只有官方SDK urllib3 transport替身；测试操作者认证沿既有本地fixture。HTTP补充未替代浏览器入口。币种/父完整/缺分母矩阵在真实PG query seam定位，浏览器保留代表路径，没有声称每个组合均跑过浏览器。

## 验证命令、失败与修复

所有执行经 `/tmp/tkada-native-series-20261112/m4b-implementation/run-test.py`，专用M4b loopback PG/Redis、已有Python3.14环境；没有FLUSHDB或重装依赖。

- 初次5项RED：缺分母输出4.5、素材行grain=None；严格分母、明确creative grain后GREEN。
- 普通实例summary接线首次写成list而既有API要求dict，导致验证失败；改为保留原summary元数据并替换buckets，未改API形状。一次定向启动误与前一浏览器suite尾段重叠；不使用该失败轮作为有效数据库证据，之后所有fixture检查和探针均串行重跑。
- 浏览器第一次要求 SELECTED_MATERIAL_SUBSET，但该样本中的特殊映射缺口先导致 CREATIVE_COVERAGE_INCOMPLETE；校准到真实场景原因，独立PG子集用例仍严格要求SUBSET并保持空差额。
- 首次父范围源码探针只改父filter.ids未触发失败：显式member是实际父读取选择者。改绑定实际parent_payload金额来源，保持业务预期；最终4个源码探针均有效RED。不是把模型通过外推为产品正确。
- 最终 metrics + tracer：13 passed（包含真实浏览器/原同步和导出链）；最终residual来源元数据补齐后再验通过；最后完整五类行从浏览器点击导出到原Worker/CSV：1 passed / 15.34s。
- 既有 metric_catalog、material_query、exports、native_exports：25 passed；先前五文件轮32 passed/1 browser assertion failed已由上述最终结果取代。
- Ruff修改产品/测试文件全部通过；`git diff --check`通过。前端 `bun run build`（tsc +生产/演示Vite）通过。没有重跑无关巨型suite或历史410诊断mypy基线。额外Biome格式化被已有root/frontend双root配置拒绝（显式config路径也相同），未修改其配置，TSX按现有风格手工排版；不宣称Biome通过。
- material_metrics模型32组合GREEN，broken scope/cross-scope/nullzero/parent-total各预期AssertionError RED；真实源码探针逐个PG GREEN，日志 `/tmp/jb36-probes-final.log`。假设是已提交publisher事实、原身份、原RR原子捕获和冻结，不是无界证明。

## 规模与限制

最后完整合成场景5 creative、1普通实例、轮播/冲突/未映射及父控制/对账，PG-backed冻结payload约437,107 bytes，筛选结果约421,336 bytes（同父控制证据在多个素材展开中重复保存）。一条窄PG查询约81.72ms；早期筛选CSV277,322 bytes；最终浏览器全行CSV357,311 bytes，Worker+HTTP约123ms。单机低负载合成数据只证明该规模能处理；不是客户分布、并发/大分页、生产P95或容量上限。生产容量预算Unknown，不能承诺无限实例或复用这组数字估算租户规模。

本票本地关键断言已闭合；官方creative口径、真实平台适用范围、生产容量保持Unknown。JB37/JB38生命周期、整个MMF的独立Standards/Spec审查与本地集成尚未执行，不把票标Done。

最终入口补证：第一次试图在同一浏览器先排队筛选导出再排队全行导出，原UI正确显示“导出中…”而工装未启动消费Worker，第二按钮等待超时。调整为先查看筛选实例和轮播，再清空筛选、只从浏览器导出完整五类集合；不mock响应、不绕过UI锁。最终15.34s通过，取代此前HTTP创建全行导出的证据。
