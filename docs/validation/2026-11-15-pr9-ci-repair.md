# PR #9 CI 根因修复（2026-11-15）

## 输入与范围

原 PR 头 `79ed56279182a9bbf95aff55e02ae21f6a43ec7c`，GitHub run `43756074001`。本轮只处理该次 CI 暴露的缺陷和旧合同断言，继续保留 M1–M5c、PR7/8，不纳入 M6/M7 或 JB-66。

远端初始结果：4 项成功；frontend 12 个浏览器断言失败；backend acceptance 2 failed、54 passed、1 skipped、4 deselected；backend modules 的 Ruff/ty 成功，但 runner 在约 40 分钟收到 shutdown signal，以 143 退出，没有模块测试结果或可下载模块 artifact。该项不能解释为测试通过，也没有证据将其解释为某个源码断言失败；不调整超时/容量门槛或删用例，修复后重新运行原检查。

## 根因与修复

- **真实 N+1：** `materialize_rows` 已批量读取事实及同步状态，却仅在显式 `selected_facts` 路径传来源观察，整页及素材父级路径重新逐行读取。复用原 `facts[(advertiser,id)]` 索引，账户、剧及贡献范围沿同一身份集合展平，空证据也显式传递。唯一 `report_provenance` 调用点改成必填证据/状态，删除两段数据库回退及不再使用的 session/tenant 参数；不新增平行 owner。
- **路由清单：** 将 M3 已批准的只读 `GET /ad-directory` 加入精确允许列表，保留完整集合等式；不修改公开接口或增加平台写。
- **当前目录与冻结日报：** 本人管理操作回读后仅失效当前 tenant/BC 的目录详情缓存；详情页头读实际 `statuses`。非素材的原表格、金额、分页、选择及关联报表保持冻结，用户明确加载才换查询。素材维度保留原自动刷新。
- **旧测试合同：** 日报改为明确加载断言，并将 60 秒/同版本/操作中暂停等自动刷新覆盖保留在素材维度；轮播补时区且保留图片顺序和不分摊；部分合计使用实际单元格/标记；Native 测试使用 M2 完整 canonical plans，保留双角色及精确 Series 保存回显。新增当前详情状态后两个旧定位器产生歧义，只收窄定位器，未删除断言。

## 实测与拒错

全部在独立本地 PG17/Redis 或实际 Playwright 浏览器中完成，外部服务仍为 transport/HTTP fixtures；不是生产或真实 TikTok 验收。

| 检查 | 原始失败 | 修复后 |
| --- | --- | --- |
| 原两个 backend acceptance 用例 | 2 failed / 31.19s；2017 SQL | 2 passed / 26.68s |
| 完整工作台验收文件 | 原远端两例失败 | 4 passed / 34.43s；同一 1000 系列/10000 广告样本 11 SQL，查询 468.82ms，仍要求 SQL≤30 且 <30s |
| 原始前端代表场景 | 3 failed，与 CI 一致 | 原 12 项及相邻分页共 13 passed / 28.0s |
| 相邻前端 99 项 | 执行中遇到两个旧定位器歧义 | 88 passed、9 专用 live 环境用例 skipped、2 failed；修正后只重跑对应两项，2 passed / 4.9s |
| 既有来源、快照、详情交界 | 复用匹配来源 | 27 passed / 4.12s |
| 强化真实合同断言 | 覆盖两条读取入口及当前目录 | 9 passed / 2.37s |

最后一组检查整页 `build_rows` 与冻结读取对账户/剧/父级/贡献范围保留精确 observation；真实数据库中同一 snapshot 的金额/原状态不变，而目录名称和启停状态更新可见。前端 fixture 不把原快照伪造成实时数据。

上述集合重叠，不相加为独立测试数量。正式/demo production build、Ruff、ty 与差异检查通过。Standards 与 Spec 两轴 fresh R1 均 PASS，无新增未解决 P0/P1；审查者只读检查，没有冒充亲跑动态测试。

源码拒错：临时换回两个前端产品文件的原 `79ed5627` 内容，新目录/冻结报表断言实际 2 failed；finally 逐字节恢复后相同场景 2 passed / 8.2s。两个文件均完整恢复后才提交。最后远端读回结果另见本轮提交和 PR 检查。私有环境、完整日志及 runner 位于 `/tmp/tkada-pr9-ci-20261115/` 与本轮原测试服务目录，不入 Git。新远端 CI 是最终 PR 门禁，不能由这些本地通过结果代替。

## 交付边界

只向现有 fork 的同一源分支推送并更新原 PR #9；不强推，不自动合并、关闭 issue、部署或执行平台写。主工作树、在途 M7 及原业务数据保持。JB-66 的窄上游 PR 仍依赖维护者合并前置基线。


## 重复 runner 终止后的完整分区

修复头 `9265cb2e` 的 run `43787725472`：前端 17m56s、后端验收15m49s及另四项已全部成功；模块仍以143结束（36m12s），后续 `always()` artifact、post action、服务清理也跳过。再次没有测试结论。没有仓库 timeout/取消规则，也没找到测试杀父 runner 的路径；宿主物理原因仍 Unknown，不能称 OOM 或40分钟上限。

本轮修复已证实的观测盲区，并缓解单机工作量：

- canonical `partition()` 增加完整文件模式，外层两台 runner 先同参全量收集，再按文件归属；随后保留原 markers、内部4组、文件间隔离、容量串行、失败/缺失/进程错误及重跑退出规则。
- 对当前全量实际收集证明：**8465 = 4233 + 4232**；文件 **384 + 383**，文件交集为空、用例并集完整。无新增排除或门槛放宽。
- 原必需检查名 `backend (modules)` 保留，独立汇总两个模块分区，仅 `success` 可通过；失败、取消、跳过均拒绝。两份 artifact 使用不同名称。原服务、job env、steps 通过 YAML anchors 共用同一配置；[GitHub 官方支持依据](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations#yaml-anchors-and-aliases)。审查R1捕获 job env 锚点错层，补环境同一性断言准确RED后修正，R2两轴均PASS，P0/P1=0。
- 利用既有 JSONL 每30秒输出各组完成/失败数、最近用例、进程状态，并采样可用内存/磁盘；完整读取允许尚未写完的末行，但损坏的完整行仍失败。正常轮询1秒，不给短测试强加30秒等待。不宣称进度输出能阻止宿主关闭。
- 模块包含实际浏览器子测试；原 job 只给独立 browser suite 安装 Bun/前端/Chromium，未给这些内嵌测试准备依赖。本轮把共同工具链移到模块开始前，将15份实际消费浏览器 helper 的文件放入既有串行阶段，避免共享固定端口（5197及默认5174）竞争；原用例保持。
- 小型实际执行暴露 M3 旧断言：目录列表本身仍不创建报表，后续详情按现行合同可有独立本地快照；creative 报表页明确“该对象没有日报指标”。测试仍保留范围/分页/不触发同步及撤权断言。撤权前等待实际父详情200响应完成，消除授权请求尚在飞行时撤权造成的定位器竞态，不增加超时或固定 sleep。

验证：23项分区/进度/配置门禁合同通过；实际 shell gate 对 failure/cancelled/skipped/空值全部拒绝；刻意令分区重叠或 gate 恒成功的源码探针均实际失败，finally逐字节恢复后23项通过。独立外层入口样本第一份14项通过；第二份沿最终 CLI 完整通过11项（含先普通组后串行的真实目录浏览器），无并行不稳定，两个分区合计覆盖原25项小样本。真实 PG/HTTP/浏览器的目录撤权链1 passed/15.98s，Native启停、ROAS与搭建三个内嵌浏览器代表场景3 passed/54.45s。语法、差异及 scoped Ruff 通过；两个CI脚本原有EXE001/RUF100告警按基线规则忽略，不修改文件执行权限或冒充全仓lint。

这些检查证明分配和门禁合同，以及已修正场景，不证明整套模块已通过。最终仍须取得两台runner的完整结果与汇总检查成功；新工作流会有9项检查，而原 `backend (modules)` 名称不变。


## 合并后续 upstream AI v2（待最终远端门禁）

上游由 `456a2c86` 前进至 `63d0257b`。本轮在原分支以普通 merge 保留双方历史，未改主工作树、在途 M7 或 JB-66；没有撤回上游异步准备、持久 outbox、独立 ReviewSource/ReviewReceipt/ReviewArtifact、按需分页、逐日分析与操作日志独立规划。原始冻结来源及分析产物保留完整 provenance 和 digest，交付页复用既有 Native 投影去掉深层来源展开，继续受 64KB 限制，仅实际交付登记 receipt。

新直接冻结入口使用报表层级选择与实际事实发布坐标，分析分组增加 level/source_kind/contract_revision；素材路径复用完整 INSTANCE tracer 与对账合同，汇总只计普通实例。新增 `0132_native_ai_archive_merge` 只汇合 `0131_native_ai_merge` 与 `0131_ai_review_archive`，无DDL，不改两侧历史迁移。上游逐日回测当前仍沿既有 Ad-derived 日事实口径，不把它写成已完成 Native direct 层级回测迁移。

针对性证据：直接冻结及素材完整人口 parity 2项、AI归档/读取/准备/规模相邻40项、Native混合来源与晚发布截止1项通过；交界回归114项通过，另2项失败分别为不可变观测夹具重复时点和本地浏览器依赖缺失，修正后对应测试均通过（后者真实浏览器1项/13.40s）。两旧head升级及既有迁移6项通过、图/no-op断言2项通过；这些集合重叠，不相加。正式/demo构建与Ruff/ty通过。独立审查与最终远端结果另追加。

以实际工作流相同 `--ignore=tests/acceptance`、`not mcp` 收集，合并后共8541项=4271+4270，两组文件387+387且互斥、并集完整。旧头b629的fork运行只作分区诊断，不能代替合并最新上游后的PR检查；未将运行器关闭的物理原因解释为已确认OOM或时限。


本次新合并范围独立审查：Standards R1 PASS（P0/P1=0，P2=1），Spec R1 PASS（P0–P3=0）；未重开已通过的 CI runner R2 或 M1–M5c 原范围。Standards指出显式DateTime Column会丢失原SQLAlchemy Python默认值，已为Receipt/Artifact同时保留Column default和模型factory；元数据探针确认时区/非空/default不变，相关真实PG读取/产品14项通过，修正后Native原件/交付专项1项通过，Ruff/ty通过。两审查者的纯内存探针另验证raw/digest、分组、缩页游标及超限先拒绝再交付，但不把替身验证算成PG/HTTP验收。旧AI目标日志的Native夹具失败由后续1项绿色接续，不覆盖原失败记录。
