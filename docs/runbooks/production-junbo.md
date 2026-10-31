# TK-ADA 公司生产首发与迁移

## 2026-10-31 万条系列暂停恢复与热修复已发布

当前生产`6677a716dee002b5c277f9c3c719701288ca374e`，schema0069。修复真实批量状态回执解析、领取连接池争抢和过期在途租约清理；已删除对象暂停记为无需修改。原10748系列经原SDK路由全部回读：9379已暂停、1369已删除、仍启用0；恢复任务成功。11角色/唯一Beat/镜像/配置/池/HTTPS核验通过，管理队列恢复；Basic配额、50窗口及既有资源保持，无迁移或新备份。详见[故障恢复与发布记录](../validation/2026-10-31-bulk-pause-recovery.md)。

## 2026-10-31 统一配额与后台批量管理已发布

生产已发布`12d24fa38a106aea398965095c4531b05aa10471`，schema`0069_management_batches`，11角色Ready、唯一Beat及HTTPS200核验通过。用户要求不运行测试并直接发布，随后确认默认档位；SDK Basic按物理端点10QPS/600QPM/864000QPD及特殊例外生效，管理请求窗口和端点在途上限50，app/tenant/advertiser资源在途预算50。既有读取覆盖、MCP速率、四开关、Worker进程/资源/队列和数据库池保持；50并发不等于50QPS。正常排空后迁移3.287秒，既有管理记录数量/摘要保持。未做备份或恢复演练，不自动回旧镜像。详见[生产发布记录](../validation/2026-10-31-bulk-production-release.md)。

## 2026-10-31 历史事实容量修正

当前生产为 `0642ad3d7d57115df1a61464b0aec2c1452c9fdb`、schema `0067_merge_directory_turn`，11角色/配置/四开关/数据库池/镜像/HTTPS验收通过，无并发、额度、资源变化。存在到期历史事实时，后台补链与目录各最多1候补，事实借用其余报表窗口；无事实需求恢复原借用。历史目录全量/依赖暂共用FIFO位置，今日依赖仍晋升实时。固定旧任务批次短窗口速度约1.8倍、素材日报约2.7倍；历史积压与平台限流仍在，不能宣称完全解决。详见[实际结果及取舍](../validation/2026-10-30-background-fact-capacity.md)。

## 2026-10-30 整批最新版本上线

生产已更新为`96b297fc653de0864ed6661981f97835ddff9c21`，schema`0067_merge_directory_turn`，11角色Ready及唯一Beat、实际镜像/配置/四开关/池和HTTPS200核验通过。保留既有并发、资源、配额和队列。按授权关闭116个旧步骤，保留已建广告与历史回执；3872条旧视频草稿成员升级，65份旧预览保留V1，历史冻结与成功对象指纹保持。用户明确要求停止剩余全部测试并立即发布，完整回归未完成；未创建生产备份或恢复演练，迁移后禁止直接回退旧程序。详见[整批发布记录](../validation/2026-10-30-combined-production-release.md)。

## 2026-10-29 发布与备份按需分离

普通发布默认不创建备份、不做恢复演练、不核验近期备份；取消Jenkins每日备份定时触发，按需人工选择 `backup-online`、`backup` 或 `FULL_BACKUP=true`。检测到数据/部署合同变化时先停止并要求人工选定方案，不自动开启备份。本条覆盖下文历史规则；当前入口及约束见[发布说明](../../deploy/release/README.md#当前规则普通发布默认不备份)。

## 2026-10-28 统一同步修复运行契约（业务验收继续）

当前生产9576233a，schema0045，11角色/镜像/配置基础验收已通过。线上验收另发现分钟失败阻断coverage调度，以及Spark资产5分钟缓存与历史续步长排队冲突，正在集中补全，未宣称全部解决。本轮双频健康：分钟新鲜度独立更新；目录/素材完整覆盖每3分钟起新轮、120秒完整期限、5分钟有效，页面展示实际覆盖时间及过期/失败。既有health task/payload不变，私有Redis进度升级v2，旧周期安全忽略。背景目录总窗口8中依赖/全量各4保底且互借；无并发、配额、四开关、schema0045或云资源变更。已错误STALE的依赖仅在原任务冻结范围及真实未解析缺口下恢复，正常入口新建请求，不复活旧运行或改成功水位。候选验证及实际发布状态见[本轮验收](../validation/2026-10-28-sync-control-and-dependencies.md)。

## 2026-10-27 22:42 统一同步尾部修复已发布

生产及公司main为`e2b49f5ff0ee5977d575b8df406c983dc0f9b40a`，schema仍`0045_directory_type_index`。三项合并：今日素材快照短退避、来源恢复当前pending物化、健康批次按实际耗时缩放。11角色精确镜像摘要/Ready、单Beat、HTTPS及实际配置核对通过，原四开关、Worker并发/资源、配额和数据库池保持。按用户要求未新建备份/恢复演练或额外全量排空，使用原正常退出宽限；未扩容或新增索引。Jenkins恢复可构建且无排队。真实业务及仍待消化项见[验收续记](../validation/2026-10-27-sync-backlog.md)。


## 2026-10-27 同步积压修复现状

截至21:02当前实际生产为 `408374933a0ca2147a6fac06765afae3450a87e9`、schema `0045_directory_type_index`。十一角色和HTTPS已验收；官方素材拆分报表端点在途12，其10QPS、app/tenant32、账户4保持，配置摘要 `f9ddf2de7dca23a9935184543951aa183891547d3bab47ee81fe7c2bcbe227a0`，四个既有开关保持true。实时目录12槽、内存请求2560Mi/上限3Gi、CPU上限3已生效。后文旧版本/旧配额为历史记录。

0045及无迁移目录/派发/健康/清理修复已经发布。索引约70.3MiB，未扩盘或计算升配；PG主节点1核2GB、存储120GB。原8个发布阻塞进入3COMPLETE/5STALE，另两个大首次导入已提交，其中新版本真实125.764秒发布成功；180秒事务/195/210秒任务和专用210秒租约已生效。素材子页断点、原请求年龄公平续跑、五秒控制信号合并及发布退出宽限240秒已上线；40837493进一步复用实际对象版本消除重复creative回读。三个固定大子运行均完整发布，最后一个在新版本28.699秒成功；21:02控制低优先信号由4717降至42。健康能够完成全周期，仍有瞬时查询失败及少量素材事实延迟，历史素材/目录继续积压。服务Ready不等于问题全部解决，详见[当前诊断与验收](../validation/2026-10-27-sync-backlog.md)。

## 2026-10-21 素材制作跳转反馈修复

当前生产版本 `37115bca3dd2f32adac54048314c73684ceb2953`，北京时间12:12:45发布完成，schema0042、11角色Ready、唯一Beat及全部原运行配置保持。素材制作点击立即显示登录进度，跳转等待持续提示，接口25秒超时可重试；Chrome生产junbo免密登录及返回入口恢复实测通过。按用户快修授权正常排空后仅切镜像，未执行备份或恢复演练。详见[本轮验收](../validation/2026-10-21-material-login-loading.md)。

## 2026-10-21 同步健康修复发布

生产于北京时间11:24:22升级为 `1b100d4ea736d1fe57d6c682fcc89e613a135ff7`，schema保持0042，11角色Ready、唯一Beat、四开关及并发/配额/数据库池不变。目录规划提前量改为热报半周期7.5分钟；健康检查与规则共用当地零点后20分钟新鲜度。按已批准的快修流程正常排空后仅切镜像，未执行备份或恢复演练。两个BC上线后先为健康，随后主BC出现目录缺口并由16→7→0收敛，11:28:22两个BC再次全部健康；目录平台请求异常和第三项未定位超时均未隐瞒。详见[本轮发布验收](../validation/2026-10-21-sync-health-release.md)。

## 2026-10-21 同步精简后续四阶段发布

当前生产版本 `340108c711ec375c5acccea88dc207ff84e4c016`，schema仍为 `0042_drop_ad_material_main`；Jenkins #101于北京时间02:06:20完成，11角色Ready，四开关/并发/配额/数据库池保持。双时钟同相、规则BC前置3分钟、当地昨日收尾及零金额关联/规则保护同步上线。原46个等待关联中44个由正常Worker完成且未新增请求，剩余有金额缺口继续保护；UTC18:15正常规则运行已实际保存忽略计数2/8/2，命中1/5/0；健康告警拆分未实施，剩余目录/身份告警如实保留。完整备份源48197483，170表/6凭据/57个Redis键隔离恢复通过，证据及剩余健康缺口见[发布验收](../validation/2026-10-21-sync-followups-release.md)。

## 2026-10-20 同步请求精简发布

当前生产版本 `481974839bf0f63bda0cbf3da8ffa078863892af`，schema `0042_drop_ad_material_main`；Jenkins #100 于北京时间22:06:41成功，11角色Ready。完整冻结备份已发布到 `/backup/latest/`，升级前版本3b4f8c5e/schema0040；隔离恢复核验170表、6份凭据、56个Redis键通过。四开关、并发、配额保持原值。迁移及上线后核查、尚待观察的目录健康指标详见[同步请求精简验收](../validation/2026-10-20-sync-request-reduction.md)。

## 2026-10-20 素材制作接入发布

生产已升级为 `3b4f8c5ed5e6fe0d761fe5b8510297eed3b5a838`，schema `0040_material_production`，十一角色 Ready。本次新增数据库结构，已执行完整冻结备份、隔离恢复及候选迁移验证，未使用快速修复豁免。最新 `/backup/latest/` 为升级前 bb4a9715 的完整冻结备份，恢复验证 169 表、6 份凭据及 55 个 Redis 键。素材开户配置在十一角色一致，原四开关/并发/配额不变。xingyu 已开户并真实免密进入；junbo 已有用户名冲突待上游处理。详见[本轮生产验收](../validation/2026-10-20-material-production.md)。

## 2026-10-20 快速修复现状

用户明确授权范围明确的快速修复不执行备份或恢复演练，优先于本页旧发布备份要求；保留精准版本、正常排空与真实业务验收。当前迁移head为0042_drop_ad_material_main，精确生产SHA以[同步请求精简验收](../validation/2026-10-20-sync-request-reduction.md)为准，11角色实际政策摘要f1a757b97de22d8eeb1e38edc4dc44a25486b699343909cb1c978aa9cf9a0dec，app/tenant并发32，账户4，已核对的SDK物理读取端点10QPS/6在途，合计日调用预算0；四业务开关true。后续补修及持续验收以[架构记录](../validation/2026-10-19-sync-architecture.md)为准。


> **2026-10-19 发布提速规则已获用户确认**：普通发布与独立在线备份分离，24小时内已恢复验证的数据库备份是普通发布前提；数据库/任务数据合同变化仍完整备份后发布。每日后台备份不停止应用。具体入口、恢复边界及上线实测见[发布说明](../../deploy/release/README.md#2026-10-19-普通发布与数据库备份分离)和[耗时记录](../validation/2026-10-19-release-duration.md)。下文逐次全备的要求属于此前规则；托管自动备份仍不能视为已经核实。

> **当前发布与备份**：公司 Jenkins 自动发布（本机 `deploy/local.py` 为应急入口），见[发布说明](../../deploy/release/README.md)。最新恢复验证通过的项目备份在 ACK `test` 命名空间备份盘 PVC `tiktok-ad-automation-backup` 的 `/backup/latest/`，仅保留最近一份、无异地副本；旧 `latest.tar` 与迁移归档路径仅为历史审计记录，不能作为当前恢复入口。

> **当前部署**：已迁至同一新加坡 ACK 的 `test` 命名空间，入口为 `test/test-https`。本机使用阿里云 ACR 公网地址推送，Pod 通过 VPC 地址拉取。原 `prod` 已停用，旧 Deployment 后续已被外部清理；仍用退役资源清单核对残留资源，禁止重启旧消费者。后文 prod/TCR 记录为迁移历史。验收见[ACR 与命名空间迁移记录](../validation/2026-10-10-acr-test-migration.md)。

> **当前生产入口**：用户指定改为 `https://tkada.shortclipflix.com`；该域名已真实解析到新加坡 ALB，现有 `*.shortclipflix.com` 证书验证通过。本次配置切换及验收见[域名切换记录](../validation/2026-10-10-shortclipflix-domain.md)。下文 galaxyshortdrama 的 DNS 问题为首次发布时的历史情况。

## 2026-10-27 当日素材快照重试实际策略

已随e2b49f5f统一发布：账户当地今日、实际live且priority1/2的素材日报，仅在`report_snapshot_changed`时采用5/10/20/30/60秒退避。五次预算不增加，仍清除私有断点并完整重读、一致后原子发布；第六次失败保留真实失败，下一正常周期可创建新任务。历史、昨日收尾、关联及平台错误沿用旧策略。无新增业务开关、索引、队列或Worker并发，四开关及现有配额保持。来源补派仅优化当前pending消息访问路径。健康检查保持原分钟节奏、完整范围和超时，批次成功耗时超过1.5秒缩小下一批，仅连续两批不超过1秒才放大，避免慢成功后马上扩批。上线证据以[当天验收](../validation/2026-10-27-sync-backlog.md)续记为准。

## 当前实时同步拓扑（2026-10-30）

当前程序发布版本以[同步持续验收](../validation/2026-10-30-sync-end-to-end-recovery.md)为准，迁移head `0064_closeout_job_scope`。API、Beat与9个Worker共11角色；c4418593于10:12完成验收，控制实际并发3、后台实际并发4；收尾索引迁移12.096秒，持续业务结果见[索引验收](../validation/2026-10-30-closeout-history-index.md)。

| 执行职责 | 当前并发与分工 |
| --- | --- |
| 实时报表 | `ads-reporting` prefork12；当前日事实候补窗口24、当前日关联窗口4 |
| 历史事实、关联与全量目录读取 | `ads-backfill,ads-directory` prefork4；历史报表候补窗口6，事实/关联各保底3，空闲份额互借 |
| 实时目录 | `ads-live-directory` prefork12，处理定向目录读取 |
| 目录本地发布 | `ads-publish` prefork1 |
| 控制、广告管理、搭建、资源、结果 | 并发依次3、1、3、4、1；builds实际3以生产容量回执为准，控制3已实际回读，见同步验收 |

候补窗口控制已领取/待交付的工作，不等于额外进程。历史关联按due_at公平轮转，租户间保留持久公平；当前日任务不与历史关联共用执行池。任务代数、冻结授权、账户锁与原outbox继续有效；同账户关联需求合并，真实映射缺口为空时不重复创建任务。没有身份来源的旧帖保留依赖等待、证据变化唤醒及最长6小时复核退避，不删除零消耗事实或伪造VID。

实时广告/素材15分钟规划，规划间隔不代表全账户完成耗时。全量扫描3小时；普通目录广告层22.5分钟、上级两层52.5分钟触发整账户有消耗对象刷新，健康时限分别37.5/67.5分钟；启用规则账户三层7.5分钟触发，健康时限22.5分钟。缺失对象立即补读，已有live在途组让出；事实健康时限20分钟。三条原PAUSE计划已迁为15分钟，恢复规则仍1小时。全部Worker数据库池2+1，API/Beat5+10；新增并发必须重新核算实际连接和资源，不按服务数估算容量。

10月30日本轮明确选择ACK控制并发2→3以减少控制排队，原申请250m/1Gi、上限1CPU/2Gi及池/队列/配额保持；Compose仍2。上线前核实际池、API单进程及40个Worker子进程，项目池上限150，加共享其它数据库实时连接与10运维预留低于普通连接上限才切换；同库其它长期消费者须另行识别。正常grace全部旧Pod退出后原子更新镜像与control命令，验收真实control池为3而非仅清单值。回退同时恢复旧模板control2。业务达标与实际版本以[持续验收](../validation/2026-10-30-sync-end-to-end-recovery.md)为准。

ee7后续后台候选：ACK BG3→4，内存request1Gi→1536Mi、limit2Gi→3Gi，CPU请求250m/上限2及数据库2+1保持，未新增机器或云服务。连接上限目标153，发布前实时核其它数据库/同库未知客户端与10运维预留，必须低于普通连接上限197；同时核节点增加512Mi请求可容。正常退出避免surge，实际模板只修改BG命令/内存与镜像，control保留3；上线验真实BG4及内存配置。失败留锁/状态待明确恢复原整模板（BG3/1Gi/2Gi），不能只换镜像。48123e1c已于09:03按此方案生效：实际BG4及内存配置验收通过；发布前连接预算187<197。完整业务结果见持续验收。

平台配额按应用及SDK物理端点计数，不能通过切换BC或连接叠加。当前app/tenant在途32、账户4；已核对的13个官方读取操作为每物理端点10QPS/6在途，通用及MCP/写入政策保持原值，合计报表日预算0。实际配置摘要与四开关见页首；不能把不同端点的独立额度误作所有读取合计10QPS。未知平台40100按完整查询指纹短退避后有界冷却，明确QPM/QPD及有效Retry-After遵守平台等待。需同时记录本地拒绝与真实平台拒绝，不能将两者混为一谈。

历史#29至#36曾在当时明确授权下跳过过慢PG全备，这些批次不记完整CI成功。此后至#73恢复为每次完整备份和隔离恢复验证（#73为6份凭据、38个Redis键，清单与实际恢复证据见同步清欠记录）。2026-10-19用户批准发布与日常备份分离，按本页顶部新规则执行；`/backup/latest/`以实际已验证清单为准，不能把#28或历史快速快照称为当前完整备份。此前在线备份#85已恢复169表并验证6份凭据（schema为0038），该次在线备份期间十一应用实例未重启；当前完整冻结备份见本页顶部同步请求精简发布记录。普通发布保存的Redis状态与每日在线数据库备份时点不同，不能作为同一时点恢复包。

## 2026-10-16 目录发布容量修复

本轮目录启动并发持久化为 1（此前 `pool_shrink` 仅是运行时措施），部署文件及重建 Pod 均需验收为 1。目录采用分批写入、独立发布续步和总事务预算；目标迁移 head 为 `0036_drop_rule_safety_settings`（含 0034 目录版本序列及 0035 报表公平游标）。四个已有业务开关沿用实际 true，不新增开关。发布结果及备份证据见[收尾记录](../validation/2026-10-16-directory-publish-finalization.md)，发布完成前以记录中的待验状态为准。

## 2026-10-18 目录积压与规则覆盖修复

生产事故发现热报已包含新广告而全量目录仍在等待，素材规则无法展开这些对象。修复后，完整热广告日报会为账户当地今天有消耗、广告或父链缺失/超过一小时的对象生成有界定向回读，每份报表最多 100 条广告，并合并同冻结范围正在执行的 ID。复用原 `ad-management` 槽、官方 SDK、路由检查及 outbox，不新增 Worker、队列、并发或业务开关。目录不再依赖全量历史扫描完成后才发现新投放。

全量目录的提前投递窗口为 4 个运行，实际执行并发仍为 1；有效租约与当前页已有的未丢失投递占窗口。超过六小时的疑似丢失消息仍按原代数恢复，定向回读不受全量窗口阻挡。已有历史消息保留并自然消费，不能据“窗口 4”宣称历史 broker 队列即时降至 4。

素材关联负缓存遇到更晚目录中的同名候选时会重新扫描；没有新候选的历史缺失仍保持缓存，不填零或以广告合计代替素材事实。现有四开关全部保持实际 true。当前应急补数、验证和发布状态见 [修复记录](../validation/2026-10-18-directory-rule-coverage-repair.md)，正式上线前候选规则不能视为已生效。

## 已确认的目标与授权（2026-10-10 更新）

用户已明确“公司集群就是生产环境，现在要部署到生产环境”，并要求“测试服的密钥、队列和授权要直接复制过去”。本次是新加坡测试实例向公司生产集群的迁移，覆盖此前“禁止复制测试密钥/授权”的默认规则；不再要求用户另找一套集群凭据。迁移不授权重复创建广告或绕过已有 UNKNOWN/冻结执行保护。2026-10-10 用户提供 `/Users/yaotingfeng/Downloads/singaporeconfig`，明确改用阿里云新加坡 ACK；原 skill 中的腾讯云 TKE 仅为此前误用的准备目标，后续不再向其部署。

| 项目 | 已核实值 |
| --- | --- |
| 集群 | 阿里云 ACK，新加坡 `ap-southeast-1`；集群 ID `c19dbd82590a34b9f87cebb8c1a858796` |
| context | `kubernetes-admin-c19dbd82590a34b9f87cebb8c1a858796` |
| 凭据来源 | 用户提供的 `/Users/yaotingfeng/Downloads/singaporeconfig`；私有副本 `.runtime/company-production-20261010/ack/kubeconfig`，0600，禁止入 Git |
| namespace | `test`，Active；生产用途，名称不代表改为测试环境 |
| 服务端版本 | `v1.36.2-aliyun.1`；本地使用校验过官方 SHA256 的 `.tools/bin/kubectl-1.36` |
| 应用资源前缀 | `tiktok-ad-automation`；2026-10-10 ACK 查重无同名业务 Deployment/Service |
| 镜像仓库 | 公网 `sg-acr-prod-registry.ap-southeast-1.cr.aliyuncs.com/test/tiktok-ad-automation:<完整 SHA>`；集群将主机替换为 `sg-acr-prod-registry-vpc.ap-southeast-1.cr.aliyuncs.com` |
| 仓库凭据 | 用户提供的新加坡 ACR 账号，私有 `ack/acr-test-review/registry.json`；禁止回显/提交 |
| 对外入口 | 现有 `test/test-https`，IngressClass `alb`；ALB 域名 `alb-ae6m00kf3iaolvbxim.ap-southeast-1.alb.aliyuncsslbintl.com` |
| 证书 | ALB 按域名自动发现现有证书；TLS 条目不设置 secretName，不读取共享证书 Secret |
| 生产域名 | `tkada.shortclipflix.com`；CNAME 已真实指向上述 ACK ALB；原 galaxyshortdrama 域名已不作为生产入口 |
| PostgreSQL 业务库 | `tkada`，2026-10-10 已用所提供账号创建，所有者 `gzjunbo`；已恢复源库 136 张业务表并验证 |
| Redis 逻辑库 | DB 15；用户明确确认使用，已重新检查空库、登记本项目标记并回读验证 |
| 应用端口 | API/容器/Service 均为 80 |
| 拓扑 | 1 API、7 个独立 Worker、1 Beat，使用同一版本及运行 Secret |
| 应用资源 | 合计申请 4.25 核 / 10.5 GiB，上限 10.5 核 / 21 GiB；不含 PostgreSQL、Redis 与发布瞬时资源 |
| 存储 | Beat 申请 20 GiB，使用已存在的 `alicloud-disk-topology-alltype` / `WaitForFirstConsumer`，随首个 Pod 可用区绑定 |
| 镜像拉取 | `test/tiktok-ad-automation-registry` 保存 VPC ACR 主机认证；所有 Pod 显式引用本项目 Secret |
| 配置落点 | `test/tiktok-ad-automation-runtime` 与 `test/tiktok-ad-automation-mcp-registration` Secret；已创建并验证九角色一致 |

ACK 已核实 7 个 Ready 节点，分布在新加坡四个可用区；`test` 未配置 ResourceQuota/LimitRange。节点容量不是可直接独占的剩余资源，部署仍须核对其他工作负载已申请量及实际调度结果。

## 迁移源核验（停写前快照）

- 来源：新加坡 `137.220.150.31` 的现有测试实例，不将源服务器改称生产。
- 实际运行版本：`8b877f4`，9 个应用 systemd 服务均 active。
- 生效配置：`/etc/tt-ada-staging/app.env`；本地旧 `.env.staging` 已落后于实际服务，不作为权威来源。
- 已只读获取实际运行配置及 MCP 注册文件，存放在 Git 忽略的 `.runtime/company-production-20261009/source/`，目录 0700、文件 0600；不在文档记录密钥值。
- 生产配置草稿在同目录 `prepared/runtime.env.incomplete`，已保留来源密钥并转换为 Kubernetes env-file 格式；这是此前目标未定时的历史草稿；当前草稿位置见下文 2026-10-10 记录。源注册 JSON 原样保留，未伪造新域名注册。
- 2026-10-09 队列快照：七条业务队列长度均为 0，Redis unacked 为 0。这只是当时快照，最终切换前仍须停止调度并重新核对。
- 已生效开关为导入 `true`、清理 `true`、周期同步 `false`；迁移方案保留这些现有值，不用模板值静默覆盖。
- PostgreSQL 与 Redis 当前均监听 `127.0.0.1`；直接复制这些 URL 到 Pod 会连接 Pod 自身，不能作为生产目标连接信息。

## 2026-10-10 新目标准备过程（迁移前快照）

用户已提供阿里云新加坡 PostgreSQL / Redis 地址及凭据，覆盖 skill 中公司固定 Redis 的默认值。凭据仅保存于 `.runtime/company-production-20261010/infrastructure.json`（0600），不写入文档、命令行参数或 Git。

| 服务 | 内网主机 | 外网主机 | 端口 |
| --- | --- | --- | --- |
| PostgreSQL | `pc-gs5ka8g997w7sv5ci.rwlb.singapore.rds.aliyuncs.com` | `singapore-pg-test.rwlb.singapore.rds.aliyuncs.com` | 5432 |
| Redis | `r-gs5lmf0pt8tic0r0t7.redis.singapore.rds.aliyuncs.com` | `r-gs5lmf0pt8tic0r0t7pd-test.redis.singapore.rds.aliyuncs.com` | 6379 |

域名已确定为 `tkada.galaxyshortdrama.com`。同日私有运行草稿 `runtime.env.incomplete` 保留原授权加密密钥、R2/API 密钥及三个开关；同步新 origin 与回调，但未伪造远端注册文件。用户随后要求自行创建项目空间：已创建 PostgreSQL `tkada`（所有者 `gzjunbo`），此前自行登记 Redis DB 15 的做法曾撤回；随后用户明确回复“可以，就用15”。据此重新原子检查 DB 15 为空并登记本项目标记，回读匹配、键数为 1。私有草稿已配置 PostgreSQL `tkada` 与 Redis `/15` 的内网 URL；尚未迁入业务数据。

当时待落实项（已完成部分及最新剩余项见顶部状态和正式发布记录）：

1. **PostgreSQL 与 Redis 编号已落实**：使用 `tkada` 项目库与用户明确确认的 Redis DB 15，REDIS_URL 已填写。Redis 的 0–255 编号为实例预设，本项目标记仅用于记录用途，不提供访问隔离或公司全局分配证明；实例 CPU、内存仍共享。不得修改实例库数或清空共享实例。
2. ACK 已用所提供凭据完成内网 PostgreSQL 只读认证（`postgres` 管理库，服务端 18.3）和 Redis AUTH/PING。外网地址连接超时，部署选用内网；已随后建好 PostgreSQL 项目库并按用户确认登记 Redis DB 15；目标恢复仍待执行，没有迁入业务数据。
3. 用户确认运维已完成公司侧域名解析至新加坡集群，不再要求重复配置 DNS；从实际使用网络核验解析与访问。本项目 Service、TLS 声明与路由已创建，控制器已成功同步；应用启动后再验证完整访问。现有通配证书已通过真实验证，无需新申请。不要再使用旧腾讯云入口 `134.175.255.190`。本项目只追加自身路由，不读取其他项目的证书 Secret。
4. 用户已在 API 应用登记生产回调，复测正常展示 TK-ADA 授权同意页。按用户直接处理 MCP 的授权，已通过官方动态注册接口创建生产客户端，HTTP 201 响应确认生产 MCP 回调；私有注册文件为 `ack/mcp-client-production.json`。新客户端 ID 与旧授权绑定不同，现有 3 份 MCP 连接切换后须重新授权；原注册及授权保持不变，新注册尚未应用到生产。R2 CORS 已追加生产 origin，新旧 origin 的真实 OPTIONS 均通过。完整 OAuth 收码/换码仍待应用部署及用户实际授权验证。
5. 最终停写窗口中的一致性备份、目标恢复验证和单侧消费者切换；条件未齐前继续保持测试服运行。

首次迁移的历史资料清单（当前域名和回调见顶部及文末）：

```text
项目：TK-ADA / tiktok-ad-automation
目标：阿里云新加坡 ACK c19dbd82590a34b9f87cebb8c1a858796，namespace prod
DNS：用户确认运维已配置 tkada.galaxyshortdrama.com 指向新加坡集群；目标入口为上述 ACK ALB，实际访问待验
HTTPS：共享入口 prod/prod-https 已追加本项目 TLS host 和 / 路由，原有站点配置保持一致
PostgreSQL：tkada（已创建，所有者 gzjunbo）；使用已验证内网主机
Redis：DB 15（用户已确认使用，已登记 TK-ADA 标记）；使用已验证内网主机，实例资源仍共享
网络：ACK 内网 PostgreSQL、Redis 已认证成功；不再按腾讯云跨云超时处理
TikTok API 回调：https://tkada.galaxyshortdrama.com/api/integrations/tiktok/callback
TikTok MCP 回调：https://tkada.galaxyshortdrama.com/api/integrations/tiktok/mcp/callback
R2 CORS：已添加 https://tkada.galaxyshortdrama.com，保留测试 origin，OPTIONS 验证通过
状态：九个应用服务已就绪；指定 ALB 的 HTTPS/登录验收通过，普通域名解析待运维核对，用户手动重新授权待验
```

2026-10-09 的 13 个部署对象已通过服务端 dry-run，不代表应用、磁盘绑定或网络验收。2026-10-10 此前腾讯云仅创建本项目前缀的临时网络探针，无密钥、无队列消费者，用于验证真实镜像拉取、调度和网络；探针结束后清理。旧目标的结果见当日腾讯云记录；新目标以 [ACK 验证记录](../validation/2026-10-10-ack-deployment-readiness.md) 为准。

## 执行顺序

1. 保留来源私密配置、注册材料和授权加密密钥；整理目标配置，只有网络地址、域名及挂载路径按目标替换。不要生成新的加密密钥导致历史授权不可读。
2. 完成项目相关验证，将已验证 `main` 推送公司 Gitea，构建带完整 SHA/revision 标签的 AMD64 镜像并推新加坡 ACR；保留本机用户未提交的 `AGENTS.md` 改动，使用干净发布副本。
3. 目标运行配置齐全后，离线容器检查真实配置，执行集群只读预检与服务端 dry-run，核对配额、调度和阿里云云盘。
4. 切换窗口停止新入口写入、旧 Beat 和正常排空 Worker，暂停备份 timer 并等待已开始备份结束。备份 PostgreSQL、Redis、独立源码/构建产物、私有配置、注册材料、证书与服务定义；校验和及隔离恢复通过后才进行目标恢复。
5. 迁移原授权记录时同步保留密钥、连接代数、冻结请求、幂等记录和 UNKNOWN 状态；队列与持久业务事实必须来自同一停写窗口。不得用空库加队列、或单独复制 `.env` 代替授权数据迁移。
6. 目标先恢复事实和队列并完成验证，再运行审查后的版本迁移及九角色部署；确认旧消费者已经停止后才让生产消费。若发现仍有未确认的真实广告任务，先列明具体任务状态与原授权再处理，不盲目重发。
7. 本项目 Ingress 规则已提前追加，正式部署时核对现有规则归属，不重复追加。验证证书、DNS、登录、租户、七 Worker、唯一 Beat、版本/配置一致，以及真实存储和 API/MCP 的读取能力。真实广告写入按具体业务授权验收。
8. 完成后恢复应恢复的备份 timer，记录新旧版本、镜像 digest、资源 UID、配置摘要、备份位置和恢复证据。旧消费者保持停止，避免双端消费；旧备份保留至回退窗口结束。

## 回退与当前状态

目标未具备恢复条件或检查失败时，保持来源实例原状。切换后的回退必须先停止生产消费者、核对新旧持久状态兼容性，再按一致性备份恢复；禁止仅 `rollout undo`、清空队列或覆盖共享 Redis 来恢复。

生产已完成数据恢复与九角色部署，旧源服务已停止并取消自启；禁止直接启动旧消费者。回退前先停止生产写入与消费者，再判断是否已有新增业务事实并制定一致恢复方案。当前新域名的真实 DNS、HTTPS 和登录已通过；新域名 API 应用回调登记及用户 OAuth 仍待完成，详见顶部域名切换记录。

## 2026-10-10 磁盘清理与迁移恢复演练

- 源测试服根分区曾为 100%，PostgreSQL 无法创建初始化文件，Redis 最近持久化失败。用户明确授权删除旧数据库/项目备份后，仅清理 `/var/backups/tt-ada-staging/` 中 10 月 5–7 日的 25 个备份目录，释放 3,230,224,384 字节（约 3.0 GiB）；10 月 8 日的 5 个备份目录全部保留。删除前核验保留完整备份 `20261008T021306Z` 的 SHA256SUMS、pg_restore 目录、Redis RDB 与项目/配置 tar。
- 清理后 PostgreSQL 查询恢复、Redis RDB/AOF 状态均为 ok、9 应用服务 active、健康接口 200，备份 timer 保持启用。没有删除运行数据库、Redis 持久文件、当前 release 或素材。
- 已将在线 PostgreSQL 自定义格式备份、Redis RDB、独立程序/前端构建归档、配置/证书归档保存到本地私有 `ack/rehearsal-source/`，并完成隔离恢复。136 张业务表可读取、4 份历史凭据可用原密钥解密；候选镜像 e01da729 的 Alembic upgrade head 通过，head=0019_scene_budget_page，迁移前后表行数及凭据检查结果一致。Redis RDB 校验通过并恢复 7 个键。
- 这是运行中各自采样的恢复演练，不是同一停写窗口的正式切换备份；最终迁移仍须按执行顺序重新冻结、排空并备份。演练使用无外网内部 Docker 网络，没有启动 API/Worker/Beat；演练容器及其匿名卷已按 ID 核对清理，本地归档保留。
- R2 使用既有 `junbo-manju-fb` 桶；控制台核验发现其 Public Development URL 原已开启，本轮未改变该共享桶公开状态，不能把它描述为已验证私有桶。CORS 只增加生产 origin，其他规则保持一致。正式私有存储要求与该现状需另行处理，不能擅自关闭可能仍供其他项目使用的公开入口。

## 2026-10-10 回调检查与待发布镜像

- 从干净发布副本同步公司 main，构建并推送 `783d3b836b61171153cf1863f4ecd7617bc3e7d6` 的 AMD64 镜像，digest=`sha256:3d3a03ec331f7adc2796a30c47dbc0836d64596164cbf6dc40701927aa94b891`。后续文档提交不代表运行代码变化；正式发布须固定所选 SHA 并核实镜像 revision。
- 源库只读汇总仍有 1 个 ACTIVE API 连接与 3 个 ACTIVE MCP 连接。仅为数据库记录状态，未执行令牌刷新或广告调用来判断远端授权有效性。
- API App ID 为 `7690990985458761748`。当前浏览器账号直接访问应用管理详情显示“未找到应用程序”。同一 App ID 使用旧测试回调可显示 TK-ADA 同意页面；换生产回调则显示应用状态错误。这个对照表明不能把错误简单归因为应用整体停用，生产回调需要由该应用管理账号核对并登记。
- 原 MCP client_id 配生产回调也能显示前端同意页面，但未点击 Authorize，不能据此认定远端接受回调。已查官方自定义客户端文档，公开流程只有注册/授权/换码/刷新/撤销，未找到原客户端回调更新接口；完整原注册响应也无管理 URI/令牌。不伪造注册材料，不擅自换客户端破坏原 3 份授权续期。
- 当前真实配置离线预检明确失败于 `MCP registration does not allow the target callback`，未写入集群运行 Secret 或启动应用。已请用户切换到上述 App ID 的管理账号；保留管理详情页供继续操作，临时授权测试页均已关闭，没有提交同意、换码或刷新。

## 2026-10-10：API 回调复测与 MCP 生产客户端注册

- 用户确认已在 API 应用配置生产回调，并明确授权直接处理独立 MCP 客户端。API 使用同一 App ID 和生产回调复测，正常显示 TK-ADA 授权同意页，之前的应用状态错误消失。未点击同意；此项证明授权入口已接受配置，尚未证明生产回调收码与令牌交换。临时检查页已关闭。
- 通过官方 discovery 的 registration_endpoint 执行一次生产客户端动态注册，HTTP 201；平台原始响应明确返回生产 MCP redirect_uri、authorization_code/refresh_token 配置及新的 client_id。完整响应与严格五字段注册文件仅保存在私有 `ack/mcp-registration-production-response.json`、`ack/mcp-client-production.json`，权限 0600。没有伪造原客户端的登记，也没有修改测试服。
- 原注册文件 SHA-256 前后相同。代码刷新流程校验凭据中的 client_id 与当前注册一致，因此新注册不能直接续期旧客户端下的 3 份 MCP 授权；迁移数据可保留，但切换新客户端后必须重新授权。尚未替换运行 Secret、撤销旧授权或启动生产消费者。
- 私有证据 `ack/callback-readiness-retest.json`；API/MCP 完整授权测试及历史 MCP 重新授权仍待完成。
- 使用正式候选镜像 `783d3b836b61171153cf1863f4ecd7617bc3e7d6`、真实运行环境与新注册文件，`DRY_RUN=1` 和 `PREFLIGHT_ONLY=1` 均 exit=0；后者包含镜像 revision 校验、断网容器 Settings/注册配置检查及 ACK 资源归属/入口只读检查。证据 `ack/production-client-config-preflight.json`。未写集群运行配置，未迁移数据库或队列；配置预检通过不代表历史 MCP 授权迁移通过。

## 正式切换记录与恢复入口

- 用户明确“继续完成，重新授权手动测试”，授权生产采用新 MCP 客户端并完成其余迁移。2026-10-10 06:14:06 UTC 源九服务全部优雅停止；七队列和 unacked 均为零，旧 Worker 无 active/reserved/scheduled 任务。
- 完整冻结备份、原配置证书、源独立程序/前端构建、Redis RDB 与逐键逻辑快照位于私有 `.runtime/company-production-20261010/ack/cutover/`。该目录的 `manifest.json`、`backup-verified.json`、`target-restored.json` 分别记录清单、隔离恢复和生产恢复证据。
- 发布 SHA `783d3b836b61171153cf1863f4ecd7617bc3e7d6`，镜像 digest `sha256:3d3a03ec331f7adc2796a30c47dbc0836d64596164cbf6dc40701927aa94b891`，head `0019_scene_budget_page`。实际注入配置备份为 `cutover/runtime.env` / `production-config.tar.gz`，本地 `.env.production` 已同步；新 MCP 注册为 `ack/mcp-client-production.json`。文件均私有且不入 Git。
- 七 Worker 并发各 1、Beat 单副本、20 GiB PVC Bound；开关仍为导入 true、清理 true、周期同步 false。实际九角色配置摘要与注册摘要一致、额度校验通过、Worker ping 7/7、启动日志无 ERROR/Traceback。
- 旧测试服保留数据库、Redis、release 和配置；九应用服务 disabled/inactive，旧 Nginx 返回 503 迁移提示页。源备份 timer 已恢复；它只备份旧源数据，不能当作生产持续备份。生产 RDS/Redis 的托管备份策略尚未从云控制台核验，本次一致性备份及生产配置恢复包已异机保存于当前工作站。
- DNS 管理属于阿里云 ns7/ns8.alidns.com；2026-10-10 集群/Google DNS 实测生产主机名 NXDOMAIN。已请核对 `tkada` CNAME 指向 `alb-ae6m00kf3iaolvbxim.ap-southeast-1.alb.aliyuncsslbintl.com`，本机未持有该 DNS 账号权限；不要通过修改共享 CoreDNS、忽略证书校验或硬编码 ALB IP 冒充修复。

## 当前域名配置与恢复入口（同日后续切换）

- 当前网站：`https://tkada.shortclipflix.com`，真实 DNS / HTTPS / 原账号登录已通过。新域名仅向共享入口追加自身条目。旧 galaxyshortdrama 的 NXDOMAIN 记录为历史观测。
- 当前运行配置与 MCP 注册：私有 `ack/domain-shortclipflix/runtime.env`、`mcp-client.json`；本地 `.env.production` 已与线上核对同步。切换后的恢复包为同目录 `config-after.tar.gz`，校验和及回读证据为 `config-after-verified.json`。上述 `cutover/` 配置及 `ack/mcp-client-production.json` 是旧域名历史备份，不可直接作为当前配置重发。
- 本轮冻结备份与恢复证据位于同目录 `manifest.json` / `backup-verified.json`；未改变运行镜像和数据库 head。九角色 ready，七 Worker ping 通过，唯一 Beat 和 PVC 正常；临时工具 Pod/Secret 已按 UID 清理，旧测试服务继续停止。
- TikTok API 应用须登记 `https://tkada.shortclipflix.com/api/integrations/tiktok/callback`；MCP 新回调已由官方客户端注册并部署。用户手动重新授权及完整业务验收仍待完成。
- 详细证据及运维待验边界见[域名切换记录](../validation/2026-10-10-shortclipflix-domain.md)。

## 2026-10-10：迁往 test 与新加坡 ACR

用户已授权修改并迁移。目标 namespace 为 `test`，共享入口为 `test-https`，与原 `prod-https` 使用同一 ALB，DNS、域名、API/MCP 回调和数据连接不变。公网镜像地址为 `sg-acr-prod-registry.ap-southeast-1.cr.aliyuncs.com/test/tiktok-ad-automation:<SHA>`；Pod 拉取地址为 `sg-acr-prod-registry-vpc.ap-southeast-1.cr.aliyuncs.com/test/tiktok-ad-automation:<SHA>`。用户提供的账号已登录成功；原始凭据仅保存在私有 `ack/acr-test-review/registry.json`。

备份及迁移证据使用私有 `ack/acr-test-migration/`。先发布脚本到公司仓库并构建新镜像，验证 VPC 拉取，再停写/排空并完成 PG/Redis/源码构建/配置/Beat 文件备份及恢复演练。生产 PG/Redis 原地复用，不执行数据覆盖恢复；原 namespace 九服务最终保留零副本。目标 Beat 使用新 20 GiB PVC 恢复原调度文件，目标配置/注册/开关必须与来源逐项一致。共享入口仅迁移本项目 host，保留其他所有 spec 条目。

当前运行 SHA 为 `673eecb9f928261541be57ecc93b3c7f4350682f`；ACR 镜像 index digest 为 `sha256:4923ba800e3c403d40cdbf8131fdadf164b783c25067c554e6dc32e9c439ec48`，AMD64 manifest 为 `sha256:9fa1b3d971991f44a4bf0afeef999c3d50186638dd0bf03002ef19b10637506a`。旧 `prod` 九 Deployment 为零副本，原 Beat PVC 和 Secret 保留作受控恢复依据，禁止直接启动。未来部署必须设置 `RETIRED_SOURCE_INVENTORY` 指向 `ack/acr-test-migration/retired-source.json`，逐项核对旧资源 UID/模板与停止状态；不能绕过查重。

最新恢复配置包为 `ack/acr-test-migration/config-after.tar.gz`，同目录保存冻结备份、独立源码及实际构建、Beat 文件、恢复验证、入口前后快照与部署验收。当前运行配置和注册文件与切换前逐项相同，本地 `.env.production` 匹配。旧 `domain-shortclipflix/` 为上轮配置证据，不能直接重跑其中旧 namespace 部署脚本。

## 2026-10-12：准备阶段读取并发与预算检查

- 已发布配置（`8b22d7ec`）：`worker-resource` 保持一个 Pod，Celery prefork 从 1 槽调整为 4 槽；请求仍受线上共享额度控制（应用/租户并发 4、单接口并发 2、每秒应用 10/接口 3），不放宽 TikTok 配额。
- 发布前基线：资源 Worker 约 355 MiB，原请求 2 GiB/上限 4 GiB；PG 连接约 69/200。新增三槽按默认连接池每槽最多 15 连接估算，上限增量 45；场景调用的短事务使用 NullPool。发布后复核 Pod RSS/重启、连接占用与错误日志；视频处理仍受素材准入约束。
- 每账户场景不再读取历史 Smart+ 广告组来判断预算资格，USD 金额/精度与预算竞价组合本地校验；保留旧预算证据模型供历史记录读取，不再由场景任务调度。
- VBO 最低 ROAS 资格只约束目标 ROAS 策略；最高价值不再被此资格阻断。有效场景仍按租户、BC、账户、Mini、冻结授权及版本隔离，沿用实际 86400 秒缓存；场景合同更新后首次会重新准备。
- 九角色、三个业务开关及应用额度不变。本次不迁移数据库；正式上线结果以对应验收记录为准。

- 本轮发布批次 `20261012T111458-9a0769` 已完成；9/9 Ready、HTTPS 200、实际资源池并发 4，启动阶段约 507 MiB、PG 总连接 63。备份恢复/配置/开关验证通过，详见 [准备提速验收](../validation/2026-10-12-preparation-speed.md)。

## 2026-10-15：目录/报表并发与共享额度上调

- 背景：xingyu 872 个账户接入后目录同步积压。同批发布目录减量（约降 90%）。
- Worker：`worker-ads-directory` prefork 1 → 4，`worker-ads-reporting` 1 → 2；任务以等待 TikTok 为主（目录 Pod 约 360 MiB、CPU 约 0.07 核），资源申请/上限不变。PG 为多项目共享实例（max_connections 200、本项目约 18 连接），新增进程预计 +10–15 连接，发布后复核。
- `TIKTOK_CALL_POLICIES.base`：`app_max_inflight` 4 → 8、`endpoint_max_inflight` 2 → 3、`tenant_max_inflight` 4 → 6；速率不变（应用 10/秒、每操作 3/秒），对应官方 Basic 每应用 10 QPS / 600 QPM / 864000 QPD，超过每分钟限制会封禁 5 分钟。在发布前写入运行 Secret 并同步本地 `.env.production`，由发布流程统一重启九角色加载。


2026-10-16 追加：目录 Worker 独占消费 `ads-directory,ads-publish`，并发保持 1、prefetch 1；Redis 使用 `round_robin`。`ads.sync_step` 负责抓取，末页持久化后投递 `ads.publish_step` 到 `ads-publish`，让本地发布穿插读取积压，避免可见结果再次排到万条读取后面。扫描器批量识别已暂存末页；丢失/过期消息换代后选发布队列，旧同键投递不改名覆盖。冻结发布时动态取消该 Worker 的两个订阅并正常排空；素材/结果 Worker 不消费新队列，九服务数量及素材吞吐配置不变。


### 2026-10-17 待发布：操作后定向回读与全量目录隔离

`ads.targeted_step` 使用现有 `ad-management` 队列和单槽 Worker，执行带明确对象 ID 的目录读取、分页与发布。全量目录仍由 `ads-directory,ads-publish` 消费；未调整 Worker 数量或节点并发，也无新队列或开关；回读与全量读取可能同时占用共享调用准入额度。新任务入队与目录运行同事务；恢复器优先恢复定向运行，原消息由已有租约/代数隔离。`ADS_SYNC_ENABLED` 仍同时控制这些读取，不改变关闭语义。冻结发布继续排空广告管理 Worker。发布后须核对新任务路由、原全量队列继续消费，以及真实启停后目录/报表状态回读；本节代码本地验证不代表已经上线。广告管理单槽仍有自身排队及平台限流，不承诺即时状态。


### 2026-10-18 已发布：实时/补数/定向读取分槽

本节取代 10 月 17 日的定向任务队列合同。`ads.targeted_step` 进入独立 `ads-live-directory` 队列（prefork 4）；`reporting.backfill_step` 使用 `ads-backfill`（prefork 1）；实时 `ads-reporting` 为 prefork 4。全量 `ads-directory,ads-publish` 保持单槽，广告管理单槽仅承载写任务。部署从 1 API + 7 Worker + 1 Beat 扩为 1 API + 9 Worker + 1 Beat，全部使用同版本与配置。新 Worker 申请/上限分别为 backfill 512Mi/1Gi、live-directory 1Gi/3Gi；其余角色资源合同不变。

首次升级先按完整旧九角色冻结、排空、备份和隔离恢复验证，完成后才迁移管理队列中的精确 `ads.targeted_step` 消息、应用正式数据库迁移并以零副本创建新角色。迁移保留正文、ID、优先级与队列内 FIFO，只修正 broker 路由；未匹配管理写任务保留原字节。数量与校验摘要绑定备份清单，以 Redis 原子凭据及私有文件留存。恢复前须区分原九角色或原十一角色：broker 迁移之前可恢复原合同；进入 broker/数据库迁移后禁止自动启动旧版，完成候选后才恢复十一角色。完整边界见 [发布手册](../../deploy/release/README.md)。

四个已确认开关保持 true，共享调用配额保持应用并发 8、端点 3、租户 6、应用 10/秒、操作 3/秒；并发进程不扩大平台限额。新两个父进程与新增七个 prefork 子进程会增加 RSS 和数据库连接占用，不能以旧 Pod RSS 简单相乘宣称容量已验收。全部 Celery Worker 显式设置 `DATABASE_POOL_SIZE=2`、`DATABASE_MAX_OVERFLOW=1`，18 个 prefork 子进程的连接池合计最多 54；API 保留共享配置（默认 5+10），单进程最多 15，另需给控制父进程、迁移和其他共享项目预留连接。设置是按角色覆盖，不修改共享 Secret 或扩大 API 池；验收逐角色对环境摘要及实际 Settings 池值，不能漏掉任一 Worker。实际仍需结合 NullPool/短事务和共享 PG 200 连接预算实测。发布后记录每角色 RSS、OOM/重启、活跃/空闲连接、锁等待、队列最老时间与实时覆盖；生产旧快照约 reporting 493Mi、directory 552Mi、本项目 PG 18 连接只是基线。本地通过不代表已发布。

## 2026-10-18 实时同步可靠性整改（已发布，连续周期验收中）

当前日广告/素材事实每两分钟规划，实时报表四槽与历史补数一槽独立；定向目录四槽和广告管理一槽独立。新/缺失广告及父链立即补读，规则覆盖账户的旧状态两分钟刷新，普通账户旧状态三十分钟刷新，现有平台配额不变。分页断点、冻结范围合并、发布水位及跨租户轮转防止大任务反复占用实时容量。

每分钟以账户当地今天检查当前事实五分钟覆盖、目录和关联缺口、原管理动作积压及完整筛选证据；未知覆盖及监控停滞均降级，状态/事件持久保存，缺口每两分钟有界重建。界面显示最旧有效时间并在空闲时自动接收新金额。五分钟排期仅开放给PAUSE；上线后仅原三条止损改频率，恢复规则保持原一小时和所有条件。

本次9→11、0037和broker精确迁移已发布为 c80421c3。用户因紧急实时性故障明确豁免本批耗时 PostgreSQL 全备及隔离恢复：保留已验证 #28 完整备份，完成当前 Redis/程序/配置等快速快照；这是一批授权例外，不更改后续常规完整备份要求。不清队列，四开关全部保留true，Worker连接池2+1，API/Beat5+10。前三条原止损已改5M，恢复保持1H。实际证据和剩余缺口以[本轮记录](../validation/2026-10-18-realtime-sync-reliability.md)为准。


### 2026-10-18 全量规划去重及本地评估容量

已有同BC、执行人及冻结授权的全量运行时，不重复规划该账户的新全量周期；目录扫描独立SAVEPOINT，每条SQL最多8秒，取消时仅延期目录，保留本次已规划的热/素材任务。历史全量运行继续消费，不清队列。

ads-reporting prefork从4调至8，实时领取窗口仍4、补数仍1；TikTok应用并发8、端点3及每秒限额不变。增加的是本地规则评估与事实的并行余量，共享队列不等于硬预留四槽。该Pod资源保持申请1CPU/2Gi、上限2CPU/4Gi，原四槽RSS实测786Mi；发布后核对实际8槽、RSS及事实延迟。二十二个业务子进程池最多66连接，加API15和其它当前54连接，仍低于PG200；实际以连接与锁等待观测为准。

## 2026-10-19：素材报表本地限流调参

在原有官方 SDK/Redis 准入中，仅对 `TIKTOK_CALL_POLICIES.endpoints.reports.material_breakdown.endpoint_calls_per_window` 设置 `5`（此前继承 base 的 `3`），窗口仍为 1000 ms。素材操作最大在途仍3；应用仍10次/秒、最大在途12；租户10、账户4、租约960秒及其他端点策略均不变。作用范围是本应用全部租户/BC 的素材拆分读取（含关联定位使用的同操作），不涉及广告写入或新开关。

依据：实时素材五分钟约完成414个大池账户，覆盖需求585；去除已确认空桶重复请求后仍约160–170账户过期。隔离只读探针在同一个应用10次/秒保护下完成10个真实SDK请求，2.425秒、单秒峰值5，全部成功、无平台错误；正式进程尚未变更。按既有流程先保存前后参数、更新运行 Secret 的单一策略键及本地私有 `.env.production`，再执行完整备份、排空与统一重启发布，发布后核对11角色实际策略及业务吞吐。私有证据和恢复参数留在本轮运行目录；验收见 `2026-10-18-sync-simple-recovery.md`。此段记录目标值，最终是否生效以发布验收为准。

## 2026-10-19：同步架构及配额作用域修正（待发布验收）

本轮目标迁移0039_sync_service_class：目录用途贯穿历史/实时依赖和父链，负证据按账户+具体查询范围存储，完整基础报表支持持久分页断点。实时目录8槽只消费ads-live-directory；后台3槽消费ads-backfill,ads-directory；发布1槽只消费ads-publish。三者总进程数12不变，历史事实领取窗口由1改3以填满后台容量；后台/实时目录内存合计request2.5Gi/limit5Gi不变；其余队列、数据库池、四个true开关保留。

生产配额变更必须在可恢复备份后执行：保留base应用12、租户10、账户4在途和1000ms窗口、960秒租约及通用/MCP/写入覆盖；增加OFFICIAL_API限定的报表/目录十个逻辑操作覆盖，每秒10次、端点在途6。物理读取端点独立速率，BC/普通integrated报告共桶，跨BC/令牌不拆分。TIKTOK_REPORT_DAILY_CALL_BUDGET从691200显式改为0，关闭非平台要求的合计日预算。无新增业务开关；参数含义及限流来源见部署手册。本节为已批准实施目标，最终值、版本、备份和业务结果必须在发布后回填。


2026-10-20后续快速修复：生产c553c99b已正常完成精确非Spark MID映射与Spark桥接重查；连续17:47–17:53 UTC两个BC实时报表、目录、素材关联五项指标为0。下一候选增加旧Spark帖子内容摘要/大小与同账户同广告唯一视频的精确解析，每步至多一物理请求；无schema/队列/开关变化，按用户授权直接验证、排空、仅切镜像与运行验收，跳过备份和恢复演练。发布后只将受影响且未持有效租约的依赖等待任务提前到期，仍由正常Worker读取及发布，不改完成状态或手填VID。

2026-10-20历史关联路由快修：18:07 UTC实际发现ads-backfill空闲，84个已到期关联仍共享实时池。候选将当地今天material_links保留实时4槽，历史material_links进入既有ads-backfill，与历史事实共享3槽（事实保底2、关联保底1，空闲互借）。队列注册、Worker并发、数据库结构和配额不变；正常排空旧消息再切换，按用户快速修复授权豁免备份/恢复演练。0039后台Worker同时消费ads-directory，实际共享吞吐仍须线上检查。

18:19 UTC补数执行/投递实测表明3槽领取无法覆盖outbox空档。下一候选后台领取窗口为6（3个实际prefork的两批候补），历史事实/关联各保底3且空闲借用；历史Worker仍3进程，其他并发、开关、数据库、API额度不变。不是提高平台限额。

后续宽账户读取修复：范围已知且超过200广告时先定位实际缺失旧帖并做精确内容解析，未定位才继续概览；无结构迁移或队列变化。配套将OFFICIAL_API:materials.get_videos、OFFICIAL_API:materials.search_videos、OFFICIAL_API:scene.get_identity_video增加10QPS/6在途覆盖，按已核实物理SDK端点独立计速率。来源为官方Basic每端点10QPS规则；通用/写入/MCP、全局32/租户32/账户4及四开关不变。用户已授权根治本地错误限流，按快速发布豁免备份/恢复演练，排空后CAS仅更新该策略字段并验收11角色摘要。
