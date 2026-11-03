# 发布与部署

素材工具联合开户与免密入口的配置及验收见 [素材制作接入](../integrations/material-production.md)。新增 `MATERIAL_TOOL_PROVISIONING_KEYS`、`MATERIAL_TOOL_DEFAULT_KEY_REF`，API/control/Beat 使用同一私有配置；没有新增布尔开关。空配置会拒绝新联合开户，已有业务不受影响。本次含 `0040_material_production` 迁移和 Beat 恢复任务，发布须走完整迁移流程；本地测试不代表真实开户/免密联调。

ACK 本地发布和备份入口见 [本地一键发布](../../deploy/release/README.md)：统一检查、推送公司 main、构建上传、备份、发布和验收；本机只保留最新一份恢复验证通过的备份，不需要 Jenkins 或云备份存储。

## 发布主分支

`main` 是唯一长期维护和服务器发布的主分支。功能与修复完成相关验证后合并到 `main`，推送仍需用户明确授权；发布选择已经推送、验证通过的 `main` 完整 40 位提交 SHA，按目标环境手册执行备份、迁移、切换和验收。公司 Gitea 的 `main` 推送会触发 Jenkins 自动发布；本机 `python3 deploy/local.py deploy` 保留为应急入口，同一版本由一处发布流程执行，避免重复构建及争抢集群锁。

历史集成分支 `feat/platform-implementation` 于 2026-09-16 在本地原位更名为 `main`，业务代码基线保持 `4f571406cab2690d04a2be16d18df042ef70ffe7`。远端主分支切换及旧分支清理状态见[整理记录](../validation/2026-09-16-branch-cleanup.md)。

新加坡独立测试服务器使用无 Docker 的 systemd 部署，操作遵循 [staging-singapore.md](staging-singapore.md)。该服务器始终按测试环境管理；2026-10-10 已作为迁移源停用，生产状态见专用手册。

Kubernetes 资产位于 [`deploy/k8s/README.md`](../../deploy/k8s/README.md)。该方案保留 PostgreSQL，使用集群外可达的 PostgreSQL/Redis 和目标环境明确配置的 R2/回调，通过外部运行环境文件和 MCP 注册 Secret 注入；它不把测试密钥提交到 Git，也不在应用 namespace 内创建数据库或 Redis。

用户已授权迁移新加坡测试服的密钥、队列和授权。2026-10-10 提供 `singaporeconfig` 后，生产目标更正为阿里云新加坡 ACK `c19dbd82590a34b9f87cebb8c1a858796`，最新指定 namespace 为 `test`，不再使用部署 skill 默认的腾讯云集群。2026-10-10 已迁移业务数据并部署九角色，新域名 DNS/HTTPS 已通过，用户 OAuth 仍待验；具体状态、已知缺项和授权边界见 [production-junbo.md](production-junbo.md)。旧地址与旧“生产验收”仍不代表该生产实例。

其他环境的首次配置和宿主机命令见 [bootstrap-deployment.md](bootstrap-deployment.md)。下文为通用 staging 方案，补充版本、素材存储和升级顺序。

## 每次发版的配置与备份规范

**2026-10-29 现行规则**：普通发布默认不创建备份、不执行恢复演练、不检查近期备份，也无每日自动备份。备份与验证按需人工执行；显式 `--full-backup` 才在发布中运行完整流程。数据库/任务合同/部署变化在停服前要求人工选定方案，不自动备份。下文备份清单仅适用于明确选中的备份操作，覆盖旧的“每次发版”与timer要求；详见[发布说明](../../deploy/release/README.md#当前规则普通发布默认不备份)。

首次部署、新增功能开关及日常升级，均须先执行下方[功能开关清单与发布确认](#功能开关清单与发布确认)。模板默认关闭不等于用户选择关闭；缺少配置导致功能不可用时，不能只交付“服务启动成功”。

本节适用于测试及生产的每次实际部署，包括仅前端、配置、依赖变更和没有数据库迁移的发布；仅提交文档而未部署不属于发版。首次部署没有旧版本或历史数据时记录不适用项，不伪造备份成功。

发布前按目标环境手册核对以下配置，只记录存在性、非敏感地址及检查结果，不记录密钥值：

| 配置范围 | 核对内容 |
| --- | --- |
| 运行环境 | 固定 Git SHA、构建方式、服务用户/目录、域名/端口、TLS、反向代理、前端 origin 与 CORS；API/Worker/Beat 同版本且仅一个 Beat |
| 数据与密钥 | PostgreSQL/Redis 连接与隔离、凭据加密密钥保持可解密既有数据、备份路径及可用空间 |
| 素材 | API/Worker 一致的 S3/R2 配置、私有桶、签名 URL 可达性、CORS、原件保留策略；外部对象不包含在项目归档中 |
| API 授权 | 已获批 App 的服务端配置、准确的 API callback；缺失时该通道保持不可用 |
| MCP 授权 | 独立客户端注册文件、issuer/resource、MCP callback、服务用户读取权限；注册配置与租户授权分别验收 |
| 能力与调度 | 实际通道配额和归属域、上传服务策略、功能开关、Worker 期限与正常排空时间；不套用合成测试配置 |

人工选择完整备份时，冻结新写入、停止调度并正常排空任务；按环境手册创建同一批次的私有备份：

1. PostgreSQL 自定义格式 dump，及与之同一停写窗口的 Redis RDB；保留队列/outbox 和待核查状态。
2. **当前运行项目的独立归档**：后端/前端源码、迁移、依赖锁文件、部署脚本与实际前端构建产物。记录归档根目录、旧/新 SHA；保留旧 release 目录仅作为便捷回退。可重建的 node_modules/虚拟环境/缓存可排除，但须记录工具链版本及可重建依赖来源。容器环境另保留不可变镜像 digest 与可恢复镜像。
3. 私有运行配置、凭据加密密钥、MCP 注册文件、该项目所有生效的 systemd/Compose/反向代理配置、证书和备份脚本；与项目归档分开受控保存，严禁入 Git 或公开下载。
4. 批次清单：时间、目标环境、运行 SHA、数据库 current/head、归档内容/排除项、文件大小及 SHA-256、恢复步骤与验证结果。完成全部检查后才标记本次发布备份完整；仅数据库备份脚本的 COMPLETE 不足以证明项目归档已完成。

检查 dump 目录、RDB 完整性、项目/配置归档可列出和校验和；在隔离临时数据库及独立 Redis 实例验证恢复，项目归档解压至独立目录核对源码和构建产物，绝不覆盖运行目录或原 AOF。备份缺项、失败或恢复验证失败时停止发布，处理后再继续。通过后才迁移、切换并验收；完成或中止恢复处理后恢复 timer。

验收记录注明备份目录、所有归档与恢复结果、当前版本/head、服务状态及真实联调边界。保留旧版本和本次完整备份直至恢复要求得到满足；未经核实不自动清理。异地复制是否已配置须如实记录，同机副本不能代替异地恢复能力。

## 首次部署的 Worker 容量评估

首次部署不能直接复制另一台服务器的 Celery `--concurrency`。先记录实际 CPU 核数、可用内存、swap、磁盘余量、PostgreSQL/Redis 是否同机、API 进程数和数据库连接预算，再核对 `TIKTOK_CALL_POLICIES` 的共享额度域、请求速率与最大并发。Beat 固定只能 1 个，Control Worker 从 1 槽开始；Resource、Result、Build 分开评估，不能只看总 Worker 数。

在目标 Linux 主机采集基线，命令输出不得包含私有环境变量：

```bash
nproc
free -h
swapon --show
df -h / /opt /var/lib/postgresql /var/lib/redis
systemctl show tt-ada-<env>-api tt-ada-<env>-worker tt-ada-<env>-results tt-ada-<env>-builds tt-ada-<env>-control \
  -p MemoryCurrent -p MemoryPeak -p TasksCurrent -p NRestarts
```

并发上限取以下四项中的最小值：

1. **内存**：先为操作系统、PostgreSQL、Redis、API 预留实际稳态用量，再额外保留至少 25% 主机内存安全余量；可分给某类 Worker 的内存除以代表性批次测得的单槽 `MemoryPeak`，向下取整。不能用空闲启动时内存代替峰值。
2. **CPU**：视频探测、摘要和本地编译等 CPU 工作的并行数不超过可用核心；网络等待任务只有在内存、数据库和上游边界都有余量时才允许高于核心数。
3. **数据库**：API 最大连接、所有 Worker 槽、Beat/运维连接总预算低于 PostgreSQL `max_connections`，并保留至少 20% 运维和恢复余量；连接池等待或锁等待持续增长即停止加槽。
4. **上游额度**：同一共享额度域的有效槽不超过 `app_max_inflight`，单操作不超过 `endpoint_max_inflight`，请求速率不超过窗口限制。超过额度的进程不会提升有效吞吐，只会增加等待、租约和重试。

用与预计业务相当的代表性批次从保守值开始，每次只给一种 Worker 增加 1 槽，至少观察一个完整批次。记录每阶段完成数/分钟、队列长度斜率、P50/P95、超时/限流/重试、PostgreSQL 活跃连接与锁等待、各服务 `MemoryPeak`、swap 和 `NRestarts`。吞吐继续提高且没有持续 swap、OOM、连接逼近预算、锁等待或限流上升时才保留；任一指标恶化即回退一级。最终值、测量批次、策略上限和回退值写入该环境专用手册。

测试环境当前 `Resource=2、Result=1、Build=1、Control=1、Beat=1` 是低配置新加坡测试机的保守结果，不是推荐生产默认值。未来生产服务器提供后，必须按上述过程重新测量；不得因测试值稳定就复制到生产，也不得只增加 Builds 并发掩盖依赖重复投递。

## 目录大数据量发布与容量（2026-10-16）

ACK 目录 Worker 在 `deploy/k8s/tk-ada-workloads.yaml` 固定启动并发为 1，与现有 `ADS_DIRECTORY_WORKER_CONCURRENCY=1` 对齐；Pod 重启、重新部署仍为 1，不依赖 `pool_shrink` 临时命令。增加并发前按容量评估验证数据库锁等待及吞吐，不能单改环境变量后认定启动参数已改变。

目录分页采集与本地发布分别占用一次任务期限：末页和发布续步在同一事务持久化，后续任务识别已暂存末页后直接发布，不再次调用平台。领取/发布事务保留锁 20 秒、单语句 30 秒、空闲事务 10 秒限制，并增加总事务 45 秒限制（生产 PostgreSQL 18）；任何异常 invalidate 物理连接，先于 Celery 55 秒软期限退出。发布按最多 500 条分批 UPSERT，名称审计与父目录检查同样分批，仍在单一事务内保持原子性，保留旧观察和不完整素材防覆盖规则。

报表同步只提前领取 2 个未过期任务，与当前 ACK 报表 Worker 两个执行槽配套；余下历史任务留在数据库，补位时重新按租户轮转和优先级选择。全局事务 advisory 锁保护窗口检查，专属 `report_dispatch_cursor` 按最久未派发租户跨轮轮转（不复用其他队列的发布游标），Worker 真正开始时续租 240 秒，结束后通过 outbox 唤醒 Control 的仅派发路径；补位不再次读取平台或筛选账户，30 秒 Beat 为中断兜底。调整报表执行容量时同时核对 `REPORT_DISPATCH_WINDOW`，不恢复每轮抢领 100 条的行为。

本次需迁移到 `0036_drop_rule_safety_settings`：0036 按已完成的自动规则产品修改删除冷却期与每日涨幅设置，旧自定义值只能通过发布备份恢复；0035 新增报表专属租户轮转游标，领取/outbox/游标同事务提交，保证单槽补位仍跨轮公平；0034 新增 `ad_directory_published_version_seq`，从对象、素材及目录运行三个来源的最大历史版本之后开始，后续分配不再扫描业务表。序列允许回滚后跳号，不复用已分配版本。发布前停写、排空、全量备份和隔离迁移演练仍必需；恢复需包含序列状态。旧程序可读取新版业务数据，但回退须先冻结消费者并按一致性恢复规则核对，不直接在线降级。

## 预览单条素材隔离升级

包含单条素材隔离功能的版本需执行 `preview_skipped_materials` 迁移。新表保存按账户冻结的排除素材、文件名及原因，旧预览不回填、不改摘要；上线后需由用户修改草稿生成新预览才应用筛选规则。完整备份/隔离恢复须包含此表。存在排除记录时禁止降级删除，也不能只切回不识别排除集合的旧程序，避免旧素材重新进入已确认广告；优先前向修复，必要恢复须停写并使用一致备份。API、Worker 与恢复进程必须同时更新。无需新增开关，现有值保持。验证范围见[单条素材隔离记录](../validation/2026-09-17-preview-partial-materials.md)。

## 素材响应归档部署与恢复

URL 导入的上传及异常恢复查询完整响应保存于 `material_response_archive`；无需另开开关，也不会恢复正常上传后的逐条回查。首次包含该功能的发布必须迁移到 `mat_response_archive`（后续以实际单 head 为准），不能只替换代码。范围、格式与失败行为见[响应留档设计](../superpowers/specs/2026-09-13-material-response-archive.md)。

2026-09-18 起，目标账户 URL 转存及目标详情/库存核验也接入同一归档器，按目标冻结路由与操作独立保存，不复用来源账户的上传归档冒充目标回执。原生共享会话中的源账户读取不绑定目标归档器；图片与共享接口不在本轮新增归档范围。此调整无需迁移或新开关，不增加成功转存后的逐条回查。历史缺失正文仍无法追溯补回，也不改变 UNKNOWN 的重复发送限制。

沿用该环境 `CONNECTION_ENCRYPTION_KEY`，不要重新生成密钥。完整数据库备份须包含归档表，私有配置备份须包含对应密钥；隔离恢复后验证可解密及摘要一致。原件自动清理不删除响应；当前不自动按时间清理归档，监控 `pg_total_relation_size('material_response_archive')` 和备份增长。回退应用前检查兼容，禁止用迁移 downgrade 删除已有响应。

排查时按租户、BC、素材、操作和接收时间定位归档，在受控服务环境调用 `app.modules.materials.response_archive.read_material_response`，传入当前管理员的 `TenantContext`、BC、素材及响应 ID；该函数重新验证数据库权限后返回完整正文 bytes。不要直接在普通日志、页面或工单中展开密文解密内容；必要导出只放私有目录。历史未保存的上传正文不可恢复，查询响应不能冒充上传响应。归档失败错误码为 `material_response_archive_failed`；恢复数据库/密钥后核查原 UNKNOWN 操作，不直接重新上传。

## 跨账户素材分发前提

首次部署不能只验证来源上传：还须通过该租户实际账户的 `file_video_ad_info_get` 获取有效 VID 的预览 URL，验证精确 HTTPS 媒体主机及公网 DNS，再配置 `MATERIAL_REMOTE_MEDIA_HOSTS`。该值为空时，来源上传可成功，但原件清理后的跨账户分发会被 `material_preview_unverified` 阻塞。禁止填通配符；新 CDN 主机须从实际平台响应确认后追加，发版保留现有值，并验证 API/所有 Worker/Beat 一致。

正常来源上传不增加逐条回查。跨账户分发需要获取当前有效媒体 URL 时才查询来源账户；媒体主机配置不代替来源授权、VID/摘要、规格或目标账户上传权限验证。实际分发和封面准备后才能验收广告创建。

## 自动账户权限与推广小程序选择

授权并同步所选 BC 后，系统复用已完整读取的账户角色和授权范围，直接发布上传/搭建权限；无需先上传成功、试建成功或由用户另行“评估能力”。已确认的权限不按 24 小时或其他年龄失效，账户列表、连接展示、上传、搭建及恢复使用同一规则。真实只读角色、撤销授权、账户不可用、缺失首次授权信息仍按实际情况提示；手动同步可更新实际角色和账户目录。

`BC_CAPABILITY_MAX_AGE_SECONDS` 仅保留为未完成目录分页/角色发布任务的完成期限，不限制已完成授权和已发布权限。R2 总量配置 `MATERIAL_STORAGE_GLOBAL_BYTES`、`MATERIAL_STORAGE_TENANT_BYTES` 默认 `0` 表示不限制；两项均支持正数窗口，旧环境须按用户选择显式更新并重载 API、所有 Worker 与 Beat。

小程序不固定绑定版权方应用。准备页自动读取目标账户的可用短剧小程序；链接有明确且可验证的 Mini 标识或已确认目标时优先沿用。2026-10-30 按用户要求，自动获取的 TikTok Minis 链接还会按第一步选定的版权方应用名称，在当前账户完整目录中匹配唯一、完全同名且可投的 Mini；重名、无匹配、手动链接及非 TikTok 链接仍由用户选择，不按唯一候选或短码猜测。一次选择作用于本批次全部剧目，后续继续验证各目标账户是否可用；有不同明确 Mini 指向的链接应分开搭建。系统按租户和精确推广 URL 保存已确认关系，不将版权方包名或短码当 Mini ID。

首次包含此功能的版本需迁移到 `automatic_mini_targets`，不新增环境开关。迁移将历史应用配置转成已有有效链接的确认记录；冲突关系不自动迁移，新生成链接不继承应用配置。旧管理员手填 Mini 接口和入口已移除，操作员在草稿内选择实际名称即可。保存使用草稿版本和原请求 ID；结果未知时查询原修改结果，不重复提交。

发布前完整备份并演练迁移；保留 `build_mini_target`、草稿修改记录和账户目录证据。出现新选择/目录任务后禁止强行 downgrade。既有连接升级后同步一次账户目录，验证权限直接生效，不再追加角色重检；真实缺权限时处理平台授权，不能手改权限位。API 与 MCP 共用此行为。

## 功能开关清单与发布确认

### 外部素材批量推送（2026-09-29）

- 接入说明与签名示例见 [素材推送接口](../integrations/material-push.md)。新增 `MATERIAL_PUSH_CLIENTS` 为私有 JSON 配置，模板/Compose 默认 `{}`，此时没有开放的接入方；管理员配置 key、secret 和专用执行用户。按用户确认，凭证允许所有有效租户，无需租户/来源域名名单；保留公网 HTTPS 和防内网访问校验，按需建立执行成员但不覆盖已撤销权限。它不是要求运营填写的租户凭证，也不替代既有默认连接。
- `MATERIAL_INGEST_ENABLED` 同时控制新外部批次受理、后台原件校验发布和既有 URL 上传入口；关闭后已接收但尚未校验的项会记录失败，需恢复后新 request ID 重推，已发送的 TikTok 结果核查仍保留。相同 request ID 可继续读取原批次，不算新受理。部署时须记录该扩展范围和实际值，不静默改变已有确认值。
- 外部原件固定为 `external`，不受 `MATERIAL_CLEANUP_ENABLED` 删除，不能因关闭/开启清理而删除素材工具的 R2 对象。服务会把原件读到 Worker 临时磁盘作摘要/ffprobe 校验，最大字节和时限沿用现有配置。resources 必须使用有硬时限的 prefork Worker；Beat 新增每 60 秒中断恢复，最多 3 次进程领取后标记失败。
- API、全部 Worker、Beat 重载相同 `MATERIAL_PUSH_CLIENTS`；只留配置一致性结论，不输出 secret、完整签名 URL 或配置全文。key 移除会停止它的后续原件读取，旧平台库存保留。确认服务用户具备临时磁盘空间、ffprobe，来源域名的 HTTPS 公网访问不被代理/重定向替换；上传及封面仍须通过既有通道权限/调用额度验证。
- 迁移 `material_push` 从 `audience_targeting` 升级，新增租户名称唯一约束、租户内默认 BC 外键与四张批次/来源表。先执行重名只读检查；有重名须管理员消除歧义再迁移。禁止自动改租户名称。有推送批次或默认 BC 配置时 downgrade 会拒绝，不能强行丢弃身份或历史；按完整备份恢复方案处理。
- 2026-09-29 已按用户授权发布测试服并配置全有效租户接入，见[发布记录](../validation/2026-09-29-material-push-staging.md)；真实素材尚未上传。首批须用获批租户、BC、真实素材完成 POST 验签、GET 回读有效 VID、原文件名和素材库状态验收，不把 202 当上传成功。

### 当前开关及影响

以下是当前 `backend/app/core/config.py` 中的全部部署级布尔功能开关；`.env.example` 和 `compose.yml` 默认均为 false。这些开关作用于整个应用实例，覆盖该实例所有租户和 BC，不能当作某个账户的独立开关。实际业务仍受各租户/BC 权限及证据检查约束。

| 开关 | 关闭时的实际影响 | 开启前提与实际验收 |
| --- | --- | --- |
| `MATERIAL_INGEST_ENABLED` | 阻止新素材导入受理、上传权限签发及新来源账户 URL 上传；已发送操作的结果核查仍保留。页面可访问不代表能入库 | 配置私有存储、分片 CORS、容量窗口、Linux prefork Worker、Beat 和调用额度；当前租户/BC 的连接与上传权限有效。在用户授权的素材与账户范围验证接收、来源上传成功回执的有效 VID 及页面可用状态；未知结果恢复另外验证 |
| `MATERIAL_CLEANUP_ENABLED` | 不发送新的原件删除/分片终止；已发送删除继续核查。已核实来源的原件会停在 `cleanup_pending / cleanup_disabled`，持续占用容量，窗口耗尽后新文件等待容量 | 私有存储具有相应删除和读取权限，明确接受清理临时原件。成功来源回执、对象归属及无活动使用等由任务再次检查；确认 DELETE/HEAD 或分片关闭证据、页面已清理计数、预算释放及账户素材保留 |
| `ADS_SYNC_ENABLED` | 停止创建和执行目录/报表同步（含手动刷新与已排队任务）；重新开启后按冻结路由与领取身份恢复 | API、目录 Worker、报表 Worker、Beat 加载同一值，并先完成非空调用策略、数据库/Redis 恢复验证；开启后全部有效 API 绑定 BC 自动同步及首次历史回补，MCP-only BC 不同步；开启前确认周期采集影响 |
| `AUTOMATED_RULES_ENABLED` | Beat 不再调度自动规则运行，也不发送新的飞书通知；规则配置与试运行仍可用，已投递的运行仍会完成 | API、control/报表 Worker、Beat 加载同一值；租户已配置并测试飞书 Webhook；开启后所有已启用规则按排期运行。当前阶段只发预警，不改广告 |

清理开启后，已有合格积压也会处理；来源账户上传已确认成功且原件无活动使用后即可清理，没有固定保留几天的期限。后台每 30 秒检查待恢复清理任务，完成时间受任务队列、使用占用和存储响应影响。取消/闲置原件也会经各自证据检查后处理；`MATERIAL_ABANDON_SECONDS` 是闲置候选观察阈值，不是已入库素材的统一保留期。删除临时原件不会删除广告账户里的素材；重新关闭开关不能恢复已删除字节，也不等于完整跨账户分发或广告搭建已验收。

`TIKTOK_CALL_POLICIES`、`MATERIAL_REMOTE_MEDIA_HOSTS`、容量配置、OAuth/BC 绑定、实际账户上传/搭建权限也可能阻断功能，但不是这两个布尔开关。按本手册及 [R2 手册](r2-video-upload.md) 分别核对；不要为排除阻断直接伪造权限或远端能力。

### 何时向用户确认

1. **首次部署**：准备完整开关清单，逐项列明目标环境、作用范围、拟定 true/false、开启及关闭影响、缺失前提，再向用户确认。可以一次确认整张清单；用户尚未选择时，不把模板 false 当作已批准的最终配置，也不宣称相关功能可用。
2. **新增开关或已有开关语义发生变化**：在部署前说明新增功能、默认值、拟定值、是否处理历史积压，以及关闭会导致什么功能不可用，向用户确认后再应用。单纯“发布这个版本”不等于已经选择新增开关的状态。
3. **日常升级或重启，已有开关含义和值不变**：沿用该环境已确认的实际配置，不重复询问、不被模板或过期本地副本重置。已在当前会话明确确认目标环境和开关值的，直接按授权执行；不能因换版本重复要求确认。
4. **拟改变已有开关值**：若用户本次尚未明确授权该变更，说明影响后确认；用户明确要求“开启自动清理”等，已构成对应环境与范围内的授权。首次部署/新增开关缺少答复时，继续完成无需该决定的准备工作，保留依赖该决定的部署步骤待确认，不以超时或默认选项代替答复。

确认应在差异清单、依赖检查和验证方案准备完整后，且在停止服务或应用配置前完成。示例：新加坡测试环境拟启用素材导入及原件清理，作用于实例内所有租户；已核验且无占用的现有原件会开始删除，账户素材保留；列明已具备的前提、尚待真实验收的部分，再请用户确认这两个值。

### 每次部署的执行与留证

1. 对比目标版本的 Settings、`.env.example`、Compose/启动注入和服务器实际配置，列出新增、缺失、删除及语义变化项。每个开关记录“当前生效值 → 本次拟定值 → 用户确认依据”；首次部署的当前值记为未部署，无法核实的记为待核实，不能套用其他环境的值。
2. 完成上节确认后，按完整备份和排空流程变更。部署私有环境文件显式写出已确认的每个开关值，不依赖代码或 Compose 默认值。测试配置位于 `/etc/tt-ada-staging/app.env`；未来生产配置路径须在服务器确认后登记。同步受控副本，禁止模板覆盖真实配置。
3. 测试环境重启 API、所有 Worker、唯一 Beat；未来生产环境按其获批的专用手册重建受影响服务。分别核对所有运行进程或容器的实际值、版本和同一份配置来源，不能只检查磁盘文件。
4. 在各服务实际环境、用户和工作目录中核验 Settings 的最终布尔值；日志只输出开关白名单和一致性结论，不输出整个环境或展开后的 Compose 配置。上述步骤尚为人工发布要求，现有启动脚本不会自动代替用户确认。
5. 按清单中的业务路径验收。开关 true、服务健康、MCP READY 分别只证明各自状态，不能代替真实业务完成；外部操作超出已有授权时记录待验范围。若用户选择保持关闭，交付时明确列出不可用功能、积压/容量影响及后续开启步骤。
6. 发布记录保存目标环境/版本、每项发布前后值、确认依据、备份批次、各服务生效检查和实际业务结果。更新环境手册与实施进度；回退时重新核对配置含义与数据库兼容性，不因切换旧代码静默重置已确认开关。

### 新开关开发完成的文档要求

每次新增、移除、改名、调整默认值或变更作用范围，同一提交同步更新 Settings、`.env.example`、运行注入位置、本节清单、功能专用手册和实施进度；记录对现有环境及积压任务的影响。发布前列出需要用户选择的具体值。开关必须说明作用范围、依赖、关闭表现、开启/关闭后在途任务行为、服务重载方式及成功证据，避免开发后长期停在默认关闭却被当作已交付。

## 素材原生共享（2026-09-13）

- **分发规则由业务代码统一执行，不新增开关**：目标已有素材优先复用；同 BC 使用 `/creative/asset/share/`，跨 BC 使用视频 URL 转存。API 与 MCP 相同，不允许共享失败/超时后自动改为重传。共享需要源 MID，返回后通过目标账户查询取得实际 VID，不能复制源 VID。
- API 通过官方 Python SDK `CreativeManagementApi.creative_asset_share`；服务器 MCP 使用实际工具 `creative_asset_share_get`。共享源/目标必须在同一发送授权下均可操作；跨 BC 则分别冻结来源和目标授权，仅复用同租户可信 SHA-256、MD5、大小一致的库存，素材库的目标 BC 可见范围不扩大。
- 共享迁移为 `material_source_bc`（当前 head 为 `automatic_mini_targets`）：来源的 BC/素材外键与来源冻结路由独立于目标；旧记录按原约束回填明确关系。备份、排空、迁移后再切版本。存在跨 BC 来源记录时，旧约束不能直接恢复，禁止强行 downgrade。
- `TIKTOK_CALL_POLICIES` 必须覆盖 `materials.share_assets`；其工作进程硬限 900 秒，租约必须大于 905000 毫秒。可继承已核实的 `base`，不必创建新开关或覆盖现有配额。部署前后分别检查 API、全部 Worker、Beat 实际环境。
- MCP 工具合同增加共享接口后，旧连接需用已有授权重新读取工具目录。管理员在「账户与授权」的 MCP 连接管理中刷新可见 BC，再同步已绑定 BC；后台对应 `connection_business_centers(refresh=True)` 及既有 BC 同步流程。真实工具校验通过后才更新连接合同版本，无需重新登录授权。目录同步发布时直接复用角色页更新权限缓存，无需另发角色重检。旧冻结请求保留旧版本，未完成任务需按原状态恢复/重新准备，禁止改写已发送请求。

- 新上传/URL 分发原样保留完整文件名，不附加后缀、不截断；不符合上传约束的名称报错，不悄悄改名。原生共享继承平台源名称。新广告的 `video_info.file_name` 从本地原始文件名传入；历史已发送请求和在线广告不自动重命名。

## 调用额度配置与交付门槛

`TIKTOK_CALL_POLICIES` 是系统通过 Redis 执行的请求限流和并发策略，不是付费额度或广告余额。API 与 MCP 都依赖它：为空 `{}` 时业务请求会被 `admission_unconfigured`（“请配置应用调用额度”）阻断，格式或租约不合法会产生 `admission_policy_invalid`。**启用任一通道前必须完成配置和校验，不能只部署代码、等用户授权后才补配置。**

配置由部署管理员写入目标环境私有环境文件：测试 systemd 使用 `/etc/tt-ada-staging/app.env`；未来生产环境按其尚待确认的专用手册指定路径。API、所有 Worker 和 Beat 必须加载相同策略及同一业务 Redis；仅修改本地 `.env` 或文件而未重启已有服务不算生效。Compose 的空值默认用于允许基础系统启动，**不代表 TikTok 集成已经配置完成**。

以下为当前测试环境的完整单行写法，不含凭据；其他环境先根据已核实的通道限制和负载制定策略，不直接当作官方默认值：

```dotenv
TIKTOK_CALL_POLICIES='{"base":{"app_max_inflight":4,"endpoint_max_inflight":2,"tenant_max_inflight":4,"advertiser_max_inflight":4,"app_calls_per_window":10,"endpoint_calls_per_window":3,"window_ms":1000,"lease_ms":960000},"endpoints":{}}'
```

| 字段 | 含义与测试值 |
| --- | --- |
| app_calls_per_window / window_ms | 每共享配额域每 1000 毫秒最多 10 次请求；为本地总量控制 |
| endpoint_calls_per_window | 每逻辑操作每窗口最多 3 次请求 |
| app_max_inflight / endpoint_max_inflight | 每共享配额域总并发 4、每逻辑操作并发 2 |
| tenant_max_inflight / advertiser_max_inflight | 同配额域内每租户/每广告账户各最多 4 个并发请求 |

**生产当前值（2026-10-15 调整）**：`app_max_inflight=8`、`endpoint_max_inflight=3`、`tenant_max_inflight=6`，其余同上（速率仍为每秒应用 10 次、每操作 3 次）。依据：官方 Basic 等级为每应用 10 QPS / 600 QPM / 864000 QPD，触发每分钟限制会被封禁 5 分钟，因此速率上限不变；单次读取约 1–2 秒，原并发 4 只能用到约 2–4 QPS。每租户 6 为其他租户的搭建和管理保留 2 个并发。配合目录 Worker 并发 4、报表 Worker 并发 2，目录任务本身不会因并发准入等待。
| lease_ms | 并发占用租约 960000 毫秒，用于异常退出后的回收，不是请求超时或等待时长 |
| endpoints | 按实际逻辑操作名覆盖 endpoint_max_inflight、endpoint_calls_per_window、lease_ms，不能覆盖共享总量 |

操作键及上传租约要求见下文“准入配置键迁移”；上传租约必须大于 905000 毫秒，候选目录读取租约也必须覆盖其整个请求时限。未知 MCP 主体时 **MCP_SERVICE_QUOTA_SCOPE 不设置**，代码将所有 MCP 连接合并至保守共享域；不能用租户、BC、connection_id 或 attempt_id 拆分额度，也不要写空字符串。只有核实真实上游配额归属后才设置该项。API 使用真实 App ID 的配额域；没有 API App 不影响 MCP 使用共享域。

在目标服务用户、工作目录、解释器和实际环境变量下执行以下无网络、无业务写入检查（不能用开发机或演示配置的通过结果替代）：

```python
from app.jobs.admission import admission_policy

operations = (
    "protocol.initialize", "protocol.list_tools", "accounts.list_bcs",
    "auth_refresh", "materials.upload_video_file", "materials.upload_video_url",
)
for operation in operations:
    policy = admission_policy(operation)  # 同时验证 base 和所有覆盖项
    minimum = 905000 if operation.startswith("materials.upload_video_") else 50000
    assert policy.lease_ms > minimum, "调用租约不足以覆盖请求期限"
print("PASS: 调用额度配置与租约校验")
```

交付检查必须按顺序留证：

1. 发布前完成私有配置、策略解析/租约、Redis 可达性、各服务配置来源检查；缺项先修复。配置更新也须执行完整备份、排空和服务重启，按目标环境手册操作。
2. 发布后确认 API/Worker/Beat 均已重新加载配置、版本一致，执行健康、登录与隔离检查。健康和 MCP configuration READY 不检查完整调用链，不能作为额度已配置的替代证据。
3. 在用户授权范围内完成 OAuth 换码、实际 initialize/tools/list、候选 BC 列表读取；由用户选择绑定 BC，再验账户发现。需要用户登录/选择时保留待验状态，不代其授权或猜测 BC。
4. 记录“已配置并生效”“只读已验证”“尚待用户授权/绑定”“素材/广告未验”等实际结果。额度缺失、解析失败或候选读取失败时，不能交付为 MCP 已可用；上传和广告创建仍须独立验收。

本节是部署执行规范，不能假设现有启动脚本已自动检查所有项目；部署者须完成上述步骤并记录结果。

## 同一 BC 的双通道选择

API（OFFICIAL_API）与 MCP（OFFICIAL_MCP）按独立 connection_id 保存授权，同一租户的同一 BC 可分别绑定两类连接，授权记录不会在系统内互相覆盖。新操作明确指定连接时使用该连接，否则使用租户管理员为该 BC 设置的唯一默认连接；没有默认连接时要求选择，不自动优先 API/MCP，也不采用“最后授权者优先”。

首次 MCP 绑定通过完整目录验证后，如果该 BC 尚无默认路由，发布事务会将当前连接初始化为默认；已有默认不会被覆盖。上述“没有默认时要求选择”指运行时选路规则，不能理解为首次绑定不会初始化默认。授权回调与 BC 接入分开：一次 MCP 授权列出当前账号可访问的完整 BC 目录，管理员可多选接入；只有一个 BC 时自动勾选，仍提交确认。已有授权使用“添加 BC／刷新列表”发现后续新增 BC，无需重新 OAuth。各 BC 独立同步、重试、解绑和设置默认连接；停用整份连接会影响该连接下全部 BC。解绑会使该 BC 原有冻结任务失效，重新接入也不会恢复这些旧任务；普通同步和令牌轮换不改变绑定代数。

任务冻结连接及授权/接口版本后，执行、重试和结果核查沿用原连接。改默认只影响后续新选路，已冻结任务不迁移；原连接不可用时阻断，不自动切到另一通道重复创建。两条连接操作的是同一 TikTok BC 的资产，并非两套广告数据；切换默认本身不会复制广告。应用审核通过后可先独立授权、验证 API，再由管理员将 BC 默认改为 API。

## 固定发布版本

从通过验收的 Git SHA 构建镜像，将私有部署环境的 `APP_IMAGE` 设置为不可变 tag 或 registry digest。API、prestart、Worker、Beat 必须使用同一个值；保留上一个镜像和与之匹配的数据库备份。发布文件为 `compose.yml` 与 `compose.staging.yml`，不要混入本地 override。

在正式部署主机、已配置私有 `.env` 和 `deploy/traefik.yml` 后执行：

```bash
docker compose -f compose.yml -f compose.staging.yml config --quiet
docker compose -f compose.yml -f compose.staging.yml build prestart
docker compose -f compose.yml -f compose.staging.yml up -d db redis
docker compose -f compose.yml -f compose.staging.yml run --rm --no-deps prestart
docker compose -f compose.yml -f compose.staging.yml up -d --no-build backend worker beat proxy
docker compose -f compose.yml -f compose.staging.yml ps
```

使用 registry digest 时提前 pull 对应镜像，省略 build。升级已有业务实例时，先停止接收新提交并正常排空 Worker，再备份和迁移；不要在旧 Worker 仍执行时替换数据库结构。迁移失败时不启动新 API/Worker。已有广告不会随本地服务停止而停止投放。

只启动一个 Beat。Worker 使用 Linux prefork；素材上传任务最长 900 秒，广告创建/核查与资源读取任务最长 45 秒。优雅关机应给现有任务完成时间；异常退出由持久化租约和核查处理。`--pool solo` 仅用于基础本地诊断，业务任务会拒绝缺少硬期限的执行环境。

Beat 每秒触发一次 outbox 发布。单次发布默认最多 20 个公平轮次，或到达 1 秒预算后结束当前轮；每轮单独提交数据库事务，全局最多 100 条、每租户最多 5 条，其中最多一条提交展开任务。没有成功发布时立即结束，保留原消息退避。可通过 `DISPATCH_MAX_ROUNDS`（1～100）及 `DISPATCH_TIME_BUDGET_SECONDS`（大于 0、至多 5）调整本地发布预算。默认单租户的每次调度数量上限为 100 条，这只是数量上限，实际速度受数据库、Broker 和 Worker 影响；TikTok 两通道配额仍由每次真实请求的共享准入独立控制。控制任务有 30 秒硬期限，不能据此假设发布速度等于广告创建速度。

## 私有素材存储

对象桶保持私有，配置 `S3_*` 和原件持久化/备份策略。API 与 Worker 使用相同 endpoint、bucket、region 和凭据；浏览器必须能访问签名 URL 中的存储地址，不能使用仅容器内可解析的 hostname。页面只通过有时效的签名 URL 直传分片和查看原件。

将 [s3-cors.example.json](../../deploy/s3-cors.example.json) 中的 origin 改为实际前端 origin，再通过存储管理界面配置。示例允许 PUT/GET/HEAD 和所需请求头，向浏览器暴露 ETag 以保存分片回执；它不会设置公开读权限。S3 控制台使用 JSON 数组格式；兼容存储以其自身配置格式为准。[AWS CORS 字段说明](https://docs.aws.amazon.com/AmazonS3/latest/userguide/ManageCorsUsing.html)

默认应用工程限制：SDK 单文件上传 256 MiB、同 App 大文件上传并发 1。目标素材成功回执及有效映射不因本地年龄自动失效；上传回执含完整可用 ID 时直接推进，原生共享未返回目标 ID 时才获取缺失 ID。明确负证据、冲突、撤权和结果未知仍各按原状态处理。超过 SDK 限制时保留已上传原件并显示阻断原因；这不是官方平台文件上限。已确认 BC 权限没有时间上限；尚未完成的目录同步和场景缓存各自的期限不是账户授权期限，也不是 API 额度。

## 发布核查

在 backend 容器执行 `alembic heads` 与 `alembic current`，确认单一 head 且匹配版本。对实际 HTTPS origin 运行仓库 `scripts/check-bootstrap.py`，再验证管理员登录、租户切换与 `/api` 同源访问。健康端点仅证明 API 进程响应；数据库和 Redis 分别看 Compose health，消费者用 `celery inspect ping` 验证。

官方 API 的回调地址为 `https://实际域名/api/integrations/tiktok/callback`，取得 API App 后填写实际配置并从租户连接页授权；未配置时的错误响应不等于 OAuth 成功。MCP 独立使用 `https://实际域名/api/integrations/tiktok/mcp/callback`，无需 API App，但必须有已核实的 MCP 客户端注册、固定 issuer/resource 与合法 callback 配置，由租户管理员自行 PKCE 授权并绑定 BC。注册材料保存在受控本地文件，凭据仅服务端加密保存，不复用 Codex token 或浏览器缓存。实际入口含非默认端口时，callback 必须逐字包含端口。

真实 API 与 MCP 能力分别记录在 [live-sdk.md](../acceptance/live-sdk.md) 和 [live-mcp.md](../acceptance/live-mcp.md)，两者互不证明。

故障观察与备份恢复见 [recovery.md](recovery.md)。日志使用应用白名单，不打开 HTTP request body、OAuth query、浏览器登录会话或展开后的 Compose 环境输出。


## 双通道版本发布关卡

2026-09-11 双通道任务的 [离线记录](../validation/2026-09-11-tiktok-dual-channel-offline.md) 已记录本地后端 2203 passed / 9 skipped 与前端 348 passed；此前跳过的 8 个 Linux prefork 用例及 1 个相关回归已在 2026-09-12 测试服务器通过，见 [发布验收](../validation/2026-09-12-staging-mcp-release.md)。真实租户授权及业务协议仍待验；测试环境发布不构成生产发布授权。执行发布前按实际环境逐项补齐证据。

1. 固定审查通过的完整 Git SHA、镜像 digest、迁移路径和唯一目标 head。多 BC 版本 head 为 `mcp_multibc_runtime`；以实际发布版本的 `alembic heads` 为准，不在服务器生成或修改迁移。
2. 冻结新写入，排空当前执行任务；再停止调度与消费者。保留队列/outbox、原请求和所有 UNKNOWN，不能以清空它们代替排空。暂停备份 timer 并等待已经开始的备份自然结束，随后创建并验证可恢复备份。
3. 使用新固定版本执行审查后的迁移。确认 current=head、单 head 和历史证据保留；API/prestart/worker/beat 必须同一 SHA。旧路由无法证明时应保持阻断，不用今日默认连接补历史。默认连接有歧义时由管理员明确选择，不能自动择一。
4. 先验管理员/租户/BC 隔离、连接状态、只读与旧 API 路径，再按已有独立证据开放 MCP 能力。实际 scope、主体或 tools/list/schema 未核实，禁止开启写能力；MCP URL 上传按已接入合同、实际账户权限及原件回读校验，见 [2026-09-13 修复记录](../validation/2026-09-13-mcp-material-upload.md)。不得把测试 SYNTHETIC 注册或策略复制到部署配置。
5. Linux prefork 使用真实 PG/Redis 执行接收后断线/终止及原通道读取恢复，确认总 create 次数为 1。macOS skip 不构成此项通过。读取 CTA 缺实际账户字段时维持 INCOMPLETE，不降低合同来取得绿色结果。
6. 成功切换或完成中止恢复后恢复备份 timer，记录当前版本、head、服务和验证结果。回退应用前先核实 schema 兼容；持久化的冻结 route、原始摘要和回执不能为降级而删除，优先修复前进。

具体命令与停止顺序以目标环境手册为准。当前只有新加坡测试环境具备可执行手册；未来生产服务器确认后必须先补齐专用命令，不能直接运行通用 staging 命令或把候选 `deploy/production-compose.sh` 当作已获批方案。


## 准入配置键迁移

双通道 gateway 使用逻辑操作键查询 `TIKTOK_CALL_POLICIES.endpoints`，当前操作分组为 `accounts.*`、`scene.*`、`materials.*`、`music.get`、`build.*`、`reports.*` 和 `protocol.*`（以发布版本的实际 operation 名称为准）。以上指网关业务/协议键；独立 MCP 刷新仍使用 `auth_refresh`，不将它改名为点分前缀。升级时逐项迁移原 SDK URL 路径覆盖；旧 URL 条目不会命中，也没有兼容回退。共享 base 与配额归属域继续生效，不能通过改键绕过共享额度。

例如旧 `/open_api/v1.3/file/video/ad/upload/` 覆盖需分别配置 `materials.upload_video_file` 与 `materials.upload_video_url`。二者当前 worker hard limit 为 900 秒，lease 必须严格大于 hard limit 加 5 秒，即 **大于 905000ms**；仅保留旧 URL 条目会落到 base，短 lease 会被门禁拒绝。容量/频率/并发值应依据目标环境及实际已核实的通道额度设置，测试里的 970000ms 和宽松 quota 不是官方生产默认。配额配置正确仍不能替代账户权限及实际上传回读验收。


## MCP 多 BC 升级验收

按 [多 BC 设计](../superpowers/specs/2026-09-12-mcp-multiple-bcs-design.md) 发布。迁移 `mcp_multi_bc` 和 `mcp_multibc_runtime` 保留原连接 ID、凭据、账户和不可变任务；旧已完成授权的接受时间从原目录完成回执补齐。旧运行目录任务只从自身冻结路由回填 BC 与版本，无法证明的任务保留证据并标为失效，不能使用当前默认连接补造历史。已有绑定或冻结证据时禁止强制降级删除新增列。

发布后先核对原授权、BC 默认及账户数量，再使用现有连接的“添加 BC／刷新列表”验证实际 tools/list 和当前账号可访问 BC；此步骤不要求用户再次登录。随后对已明确接入的 BC 执行一次同步，确认其他 BC 不受影响、聚合进行中数量最终归零。没有第二个真实 BC 时，应记录多 BC 并发仅由隔离测试覆盖，不写成真实多 BC 联调成功。仅共享授权刷新不代表每个 BC 的账户目录都变新；目录时效必须按 BC 和原授权／绑定版本分别核对。


### 广告管理执行队列

`ad-management` 使用独立 worker，配置 `AD_MANAGEMENT_WORKER_CONCURRENCY=1`。C5 只提供 Compose/systemd 模板，不在本任务部署或启用。

### 报表每日调用预算

`TIKTOK_REPORT_DAILY_CALL_BUDGET=0` 默认不设额外本地每日总预算。非零值为操作者显式选择的成本预算，合计计数达到70%后暂缓历史补数，实时周期保持；它不是TikTok按物理端点的QPD，不能把不同接口合计后误认为平台额度耗尽。生产旧值须显式改为0，不能仅修改默认值。保留Redis调用计数供观测，`ADS_SYNC_ENABLED`仍控制新同步任务。


### 报表存储重做迁移（0023 / 0024）

本次代码只在本地验证。实际发布须先执行本手册完整备份、排空与版本核对流程，再由发布人员运行迁移。0023 新建按账户存储的报表月分区及计划/任务表；0024 删除旧报表事实、覆盖、暂存、观测、运行与调度表，不迁移旧数据，不能直接降级恢复旧表，回滚须使用发布前备份。余额观测、目录及冻结查询快照保留。0024 同时给 BC 计划增加 `screening_progress`，用于大 BC 账户级筛选在额度/期限耗尽后从已完成请求续接；新增 `report_balance_run` 保存余额冻结范围与分页进度，队列只传运行 ID 和页码，完成/失败记录保留七天。

新调度为 `reporting.plan_due`（control，Beat 每 30 秒），读取为 `reporting.job_step`（ads-reporting），过期清理为 `reporting.cleanup_store`（ads-reporting，每小时）；目录仍为 `ads.sync_step`（ads-directory）。余额由 `reporting.plan_balances`（control，每 30 分钟）独立派发 `reporting.balance_step`（ads-directory），每页 100 个 BC 账户、逐页提交并续接，不随报表热刷放缓或筛选退避。移除旧 scan_due/sync_step 报表任务与异步/MCP 报表合同；发布前按排空流程处理旧消息，不把旧任务载荷交给新 Worker。

共享额度配置仅保留同步报表操作 `reports.integrated`、`reports.bc`、`reports.material_breakdown`；人工核对已有策略并移除旧异步创建额度字段 `advertiser_calls_per_window`、`advertiser_window_ms` 及旧报表操作条目。本次开发未修改任何环境的实际配置。报表只经官方 SDK，MCP-only BC 页面显示 API 接入提示；同时确认 `ADS_SYNC_ENABLED`、非空 `TIKTOK_CALL_POLICIES` 与 `TIKTOK_REPORT_DAILY_CALL_BUDGET` 在 API、Worker、Beat 的实际加载值一致。历史回补按已授权账户开户日安排，不能将本地测试或网关健康视为真实联调完成。


2026-10-16 追加：目录 Worker 独占消费 `ads-directory,ads-publish`，并发保持 1、prefetch 1；Redis 使用 `round_robin`。`ads.sync_step` 负责抓取，末页持久化后投递 `ads.publish_step` 到 `ads-publish`，让本地发布穿插读取积压，避免可见结果再次排到万条读取后面。扫描器批量识别已暂存末页；丢失/过期消息换代后选发布队列，旧同键投递不改名覆盖。冻结发布时动态取消该 Worker 的两个订阅并正常排空；素材/结果 Worker 不消费新队列，九服务数量及素材吞吐配置不变。


### 2026-10-17 待发布：操作后定向回读与全量目录隔离

`ads.targeted_step` 使用现有 `ad-management` 队列和单槽 Worker，执行带明确对象 ID 的目录读取、分页与发布。全量目录仍由 `ads-directory,ads-publish` 消费；未调整 Worker 数量或节点并发，也无新队列或开关；回读与全量读取可能同时占用共享调用准入额度。新任务入队与目录运行同事务；恢复器优先恢复定向运行，原消息由已有租约/代数隔离。`ADS_SYNC_ENABLED` 仍同时控制这些读取，不改变关闭语义。冻结发布继续排空广告管理 Worker。发布后须核对新任务路由、原全量队列继续消费，以及真实启停后目录/报表状态回读；本节代码本地验证不代表已经上线。广告管理单槽仍有自身排队及平台限流，不承诺即时状态。


### 2026-10-18 待发布：实时/补数/定向读取分槽

本节取代 10 月 17 日的定向任务队列合同。`ads.targeted_step` 进入独立 `ads-live-directory` 队列（prefork 4）；`reporting.backfill_step` 使用 `ads-backfill`（prefork 1）；实时 `ads-reporting` 为 prefork 4。全量 `ads-directory,ads-publish` 保持单槽，广告管理单槽仅承载写任务。部署从 1 API + 7 Worker + 1 Beat 扩为 1 API + 9 Worker + 1 Beat，全部使用同版本与配置。新 Worker 申请/上限分别为 backfill 512Mi/1Gi、live-directory 1Gi/3Gi；其余角色资源合同不变。

首次升级先按完整旧九角色冻结、排空、备份和隔离恢复验证，完成后才迁移管理队列中的精确 `ads.targeted_step` 消息、应用正式数据库迁移并以零副本创建新角色。迁移保留正文、ID、优先级与队列内 FIFO，只修正 broker 路由；未匹配管理写任务保留原字节。数量与校验摘要绑定备份清单，以 Redis 原子凭据及私有文件留存。恢复前须区分原九角色或原十一角色：broker 迁移之前可恢复原合同；进入 broker/数据库迁移后禁止自动启动旧版，完成候选后才恢复十一角色。完整边界见 [发布手册](../../deploy/release/README.md)。

四个已确认开关保持 true，共享调用配额保持应用并发 8、端点 3、租户 6、应用 10/秒、操作 3/秒；并发进程不扩大平台限额。新两个父进程与新增七个 prefork 子进程会增加 RSS 和数据库连接占用，不能以旧 Pod RSS 简单相乘宣称容量已验收。全部 Celery Worker 显式设置 `DATABASE_POOL_SIZE=2`、`DATABASE_MAX_OVERFLOW=1`，18 个 prefork 子进程的连接池合计最多 54；API 保留共享配置（默认 5+10），单进程最多 15，另需给控制父进程、迁移和其他共享项目预留连接。设置是按角色覆盖，不修改共享 Secret 或扩大 API 池；验收逐角色对环境摘要及实际 Settings 池值，不能漏掉任一 Worker。实际仍需结合 NullPool/短事务和共享 PG 200 连接预算实测。发布后记录每角色 RSS、OOM/重启、活跃/空闲连接、锁等待、队列最老时间与实时覆盖；生产旧快照约 reporting 493Mi、directory 552Mi、本项目 PG 18 连接只是基线。本地通过不代表已发布。

## 2026-10-19 报表关联积压修复

包含`0038_sync_work_indexes`和关联任务归并合同的版本必须完整备份发布（`FULL_BACKUP=true`），不能只更新镜像。关联领取窗口4，当前/历史各保底2并借用闲置份额；现有报表Worker并发12、实时事实窗口24、历史事实窗口1及全部配额/开关保持。旧未领取任务通过业务代码有界归并，保留承接审计，不清队列或直接改成功；在途范围不变，新需求进入后继。尚缺真实映射的任务保持依赖等待，不能把归并数量当作补齐数量。具体合同、测试和生产验收见[本次验证记录](../validation/2026-10-19-sync-backlog-control.md)。


### 迁移后的连续发布与备份辅助 Pod 容量

数据库迁移发布前的冻结备份仍是迁移前结构。即使候选迁移已在恢复副本通过，下一次普通发布仍必须核对备份源结构与当前生产head一致；不一致时先通过既有`backup-online`完成不停服备份及隔离恢复，再执行普通发布，不能绕过schema门禁。

普通发布和在线备份在应用仍运行时创建辅助Pod，必须给备份PVC所在可用区预留一个Pod槽位；只看CPU/内存余量不够。2026-10-19出现节点Pod数上限导致辅助Pod Pending，其他节点不满足PV亲和性。此次仅对本项目空闲、无在途/预取/定时任务的搭建Worker取消消费并连续两次确认空闲，正常删除旧Pod由Deployment重建到其他节点；保留任务队列、部署配置和持久卷，未操作其他项目。后续先核对该约束，避免进入发布后才等待调度。

### 2026-10-19 SDK读取配额作用域修正

已核对的报表与目录SDK读取按应用+物理端点共享速率，跨BC/租户/令牌共桶；`reports.bc`与`reports.integrated`别名必须共同限制。`TIKTOK_CALL_POLICIES.endpoints`可使用`OFFICIAL_API:<操作名>`指定仅SDK的覆盖，优先级为base、通用操作、通道操作，启动仍验证所有条目；MCP和写操作不得套用SDK读取额度。当前已批准读取目标为每操作10次/1000ms、最大在途6；应用12、租户10、账户4的在途限制是本地资源保护。并发上限不是平台已限流的证据。

报表任务progress.last_admission记录source(local/provider)、具体reason和等待时间；Redis每个规范化端点的`:metrics`哈希记录granted、local_denied（具体资源编号）与provider_denied，统计不包含令牌、账户或原始报表。原admission_deferred历史没有来源，不能回溯伪造平台限流比例。工作用途只观测用量，不能在容量空闲时因固定份额拒绝请求。


2026-10-20补充：官方SDK报表内容解析的`materials.get_videos`、`materials.search_videos`、`scene.get_identity_video`分别对应视频信息、视频搜索与帖子视频信息物理GET端点，均按应用+物理接口共享速率，跨BC/连接不得拆桶。生产仅给`OFFICIAL_API:`前缀的这三个操作配置`endpoint_calls_per_window=10`、`endpoint_max_inflight=6`，沿用1000ms窗口与960000ms租约；通用3QPS基线及写入/MCP不变。依据为[官方默认Basic额度与物理端点独立规则](https://business-api.tiktok.com/gateway/docs/index?doc_id=1740029171730433)，不代表申请到了更高级额度；真实40100继续按平台信号退避。

2026-10-29音乐读取补充：`gateway.music.search/recommend/read` 三个只读方法统一使用 `music.get` 授权与准入操作，对应 `GET /open_api/v1.3/file/music/get/` 同一物理接口；没有独立的 `music.search` / `music.recommend` 配额键。默认继承有效 `base`，需要覆盖时使用 `music.get` 或仅 SDK 的 `OFFICIAL_API:music.get`，不得按关键词、账户、BC 或查询模式拆分上游配额。沿用调用方冻结路线与 `RemoteCallBudget`；部署租约须覆盖该任务硬期限及既有余量（预算要求大于硬限加5秒，网关还核对剩余绝对期限加1秒），P1-T4不新增Worker或后台任务。未变更实际环境策略、模板默认值或布尔开关；发布时核对API/实际调用Worker加载的策略。当前音乐接口数字额度、scope与真实账户可用性仍待授权联调，不能照搬上述报表10QPS覆盖作为音乐平台额度证明。MCP不支持音乐读取。



### 2026-10-27 同步积压无迁移补修

候选增加`reporting.check_bc_health`，仍在既有control队列消费；原`reporting.check_health`每分钟仅按BC登记任务。续测载荷只含bc_id与可选cycle，租户/操作者沿用outbox信封，运行时重新核对计划及授权。Redis私有进度使用`reporting:health:progress:v1`，180秒过期；完整观测须在120秒内覆盖全范围，失败不发布部分指标。10秒是单任务续测准入预算，任务仍20/25秒软硬期限。

`reporting.cleanup_store`由每小时改为每分钟，10秒续批预算、首轮各分类一次机会、120秒互斥锁及55/60秒任务期限。分页清理按子表复合主键扫描，游标改`reporting:cleanup:cursors:v2`；旧游标不再使用，重新扫描仍保留原1小时/7天及失败可恢复保护，不需要人工删除Redis键。

后台目录丢失消息判定从6小时缩为10分钟，有效租约仍保护；本地发布暂态数据库失败按下一代两分钟退避，不能重放广告写操作。历史素材每步50页/30秒页边界预算，实时及昨日收尾仍5页；既有平台配额、容量、四个部署开关均不变。按正常排空切换，schema保持0045；上线状态与完整业务验收以当日验证记录为准。

尾部候选补充：现有control队列仅outbox中payload严格为`{"dispatch_only": true}`的`reporting.plan_due`消息显式设priority3；默认0的发送tick先消费。Redis既有0/3/6/9子队列沿用，补投识别覆盖所有子队列；正常排空仍取消逻辑control订阅。不改队列绑定或Worker数量。纯补位领取事务完成后若有新消息，复用原1秒续轮预算drain；失败保留原outbox与退避。

尾部恢复候选：`ads.publish_step`专用180秒事务、195/210秒任务期限、210秒租约；仅此task已发布消息30分钟判丢失。普通读取/targeted仍45秒事务及55/60秒、90秒租约；后台读取10分钟/实时定向2分钟判丢失，单语句30秒和锁20秒不变。正常排空须等待该发布任务自然完成，不强杀。健康续测超时先回滚批次，再缩批并按递增序号重投；单账户冷却60秒，完整周期仍120秒。私有v1进度追加调度字段，原scope/as_of/累计值含义不变。


素材断点候选：仅官方持久Smart+ad运行用主页面读取，material_child_v1私有query子运行沿用creative类型、原冻结scope/workload和目录页/outbox/租约，每步一个1000行素材页；每组最多100广告组且跨主页去重。主coverage保留原响应审计，子完成单独验收。旧镜像不认识私有子query，不可在存在未完成子运行时直接回退；正常排空、精确版本及前向修复要求保持。源页完整且当前授权有效的旧COMPLETE运行可用同服务幂等补建，不能直接改coverage为完整。

本地发布Worker退出宽限240秒（Kubernetes及systemd）；其他服务不变。目录纯补位通知在原事务outbox按tenant/actor/当前UTC五秒桶合并；实际业务任务不合并、不删旧队列。丢失即时唤醒仍靠原Beat恢复，不能将Beat30秒周期写成拥塞下处理期限。


素材子运行冻结明细放自身AdDirectoryPage保留页2147483647（MATERIAL_CONTEXT），不是provider页、无published_version；query.material_child_v1仅保留source/context指针，原parent_ids与workload路由不变。实际分页从1开始，只有经scope核验的子运行允许剥离自身context后校验完整链，普通孤页不忽略。子created_at沿原source逻辑请求年龄以延续公平排队，不能据此当作物理插入时间；新子范围验收可用request_sequence或确定性ID。旧全账户creative仍降后，子素材续页与原请求同序。


### 手动图片入库运行参数（P1-T2）

`MATERIAL_IMAGE_MAX_UPLOAD_BYTES=20971520`（20 MiB）与
`MATERIAL_IMAGE_MAX_PIXELS=16000000`（宽×高）为本服务有限资源准入，
不是 TikTok 上传平台硬限制；平台证据仍见图片轮播验证记录。它们不增加业务开关，
沿用 `MATERIAL_INGEST_ENABLED`、对象容量预算及清理开关。
Settings 拒绝非正数、超过 1 GiB 的图片字节预算或超过 3200 万的像素预算。
调整时按 Worker 实际并发核算 Pillow 解码峰值内存；默认单图 RGB/RGBA 像素缓冲
约 48–64 MB，实际进程峰值可能更高。API、资源 Worker 通过现有环境注入加载相同值，
部署时核对实际值。前端提示默认值，服务端执行最终边界，不把浏览器 MIME 当作验真。
本任务只在专用本地测试库迁移与验证，尚未部署。

只支持手动上传静态 JPG/JPEG、PNG；外部 push/来源 URL 业务入口继续视频限定。
图片经过完整解码、尺寸、摘要和 MIME 格式一致性验证，拒绝动画/多帧、坏图和解码炸弹，
不改写原图。失败原件登记清理意图，删除/HEAD 回读确认前不释放额度；关闭清理开关时
会保留容量等待恢复清理，不能报告已回收。

T2 至 T3 的交接：同 tenant/BC/material/current generation，`MaterialFile.media_type=IMAGE`、
`TemporaryMaterialObject.status=verified`、`IngestSessionFile.status=stored`、`dispatch_id IS NULL`。
该状态表示待图片平台分发，ready_count 不增加。恢复验证只扫描有 dispatch 的记录，
不会重新探测；超期遗弃扫描只保护这一交接组合，用户取消仍按引用和删除证据回收。
T3 必须在持有素材/原件锁、重新核验权限与冻结路线后，从此查询集合登记图片专用任务，
替换 `object_validation.validate_original` 的 IMAGE 交接分支；不得调用视频
`materials.upload_original`、`source_url_uploads` 或封面生成管线。
同时更新 `readiness`、`repository` 视频候选和 source 重试入口的 IMAGE 守卫，
统一接入图片分发状态机，不保留平行上传路线。获得可核验的图片平台回执后，
再按 `object_uses` 和清理证据合同解除原件保护。

## 图片轮播 V2 合同切换（2026-10-30，本地实现未部署）

本次包含0059视频草稿单元迁移、0060冻结合同描述与0061逐广告可执行性。按2026-10-29规则，普通发布遇到合同/数据库变化在冻结前停止；先选定具体迁移方案，不能自动切到备份。详见[本地验收与待发布清单](../validation/2026-10-29-image-carousel-batch.md)。本次没有线上备份、恢复或验收证据。

除了进程检查，还须用候选 deploy/release/probe.py 的 creative-unit-drain 检查持久旧任务。它兼容已知0060前schema和0060/0061混合历史，阻断旧BUILDING预览、未展开/非终态提交及QUEUED/PENDING/RUNNING/RETRYABLE/UNKNOWN步骤；未知head和缺descriptor不放行。显式完整迁移候选含0060时，release工具已在pause后、snapshot/迁移前接线；普通发布和独立备份不变。人工不备份迁移方案复用该guard并如实记录未备份/未恢复演练。

失败时保留任务、队列和UNKNOWN正文；迁移前按原部署正常恢复并排空，迁移开始后禁止自动回旧程序或降级。V1未提交预览只历史查看，新提交报preview_contract_expired，重新生成V2；已有提交幂等重放返回原结果。历史正文/hash不变，原预览可因草稿版本提升转OBSOLETE。产生V2数据后不承诺数据库回退。

四个现有开关及额度保持实际选定值，图片参数不另增开关。发布前按目标租户/账户只读审计material_post_link.main_material_type的distinct值：保留未知原始类型及广告金额，不猜成VID或素材自动规则输入。真实单图/三图/多单元/共享以及审核、播放、价值优化按上述清单逐项另行授权验证；合成演示不代表可投放。


## 统一TikTok额度与批量管理（2026-10-31已发布）

2026-10-31新增直接管理任务及物理请求审计，目标schema `0069_management_batches`。额度分组、SDK应用档位、50请求窗口与资源/平台配额的区别、迁移及验收要求见[统一吞吐说明](tiktok-throughput.md)。生产已按用户确认的默认Basic档位生效；实际迁移、配额及未运行测试的记录见[本次发布](../validation/2026-10-31-bulk-production-release.md)。

## 明略版权方接入（2026-11-03，本地实现，尚未发布）

明略使用现有版权方连接入口，账号/密码验证后发现 TT Minis 应用；每个应用保存分销商和应用上下文，起播集默认 1。验证码由 ddddocr 子进程离线识别，不读取本机 CLI 账号文件，也不支持在 Worker 中等待终端输入。

发布本功能需包含 `0100_minglue_provider` 迁移以及 `uv.lock` 中新增 OCR/ONNX 依赖的新镜像；不能只同步 Python 源文件。按正常发布流程审查数据库约束迁移与新增取链步骤合同，排空旧版在途步骤后同步切换，保留既有功能开关与凭据。登录最长预算 35 秒、单次 OCR 最长 15 秒；上线时核对实际识别耗时、Worker 峰值内存与错误状态。内部 appId 不是 TikTok Minis ID，继续使用既有 Mini 选择流程。

本地测试与真实联调边界见[明略验收记录](../validation/2026-11-03-minglue-provider.md)。当前未发布、未配置真实明略连接、未执行真实建链；发布后的应用发现、授权剧目取链及详情回读须另行记录。有明略连接数据时迁移拒绝降级，不以删数据方式回退。
