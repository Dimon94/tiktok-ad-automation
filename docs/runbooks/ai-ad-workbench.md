# AI 投放工作台 v1 运行与接入

适用于 `codex/ai-ad-workbench-v1` 的正式 React/FastAPI 实现。产品允许每租户多位优化师，每位绑定单一投放策略版本与多条已有自动规则。自动选剧、复制放量、多方案及 A/B 不在 v1 范围。

## 应用与数据库

本版本迁移 head 为 `0128_material_production_origin`。新增优化师时区/复盘时间、素材配置及独立素材订单台账，不可作为无迁移快修直接发布。部署按主部署手册选择数据库迁移方案、正常停止写入和排空；本文不表示已取得生产发布或真实投放授权。

Beat 的 `ai_workbench.dispatch_due_plans` 每 30 秒领取到期剧单，搭建交给 `builds` 队列；`ai_workbench.scan_material_orders` 每 30 秒交接/查询素材订单，订单任务在 `control`；`ai_workbench.schedule_reviews` 每 60 秒检查当地复盘时间。Worker 继续沿用现有队列，无新增常驻 Python Worker 或部署级布尔开关。

排期默认 `Asia/Shanghai`，可按优化师设置 IANA 时区。用户输入是当地墙上时间，台账转换为 UTC。未来排期不提前冻结；发生记录或素材订单创建后，行内容只读，修改其他行保留原行 ID。当天截止后不自动跨日补搭建，未知平台结果不重建。

创建、准备、预览、提交和回读继续走既有搭建服务。准备尚未就绪时等待并按原草稿/请求号恢复，不把正常异步等待记成永久失败。优化师暂停同时约束已投递的搭建任务；恢复后仍沿用原发生记录。

## 素材订单

先通过既有素材制作开户流程为租户建立 `ready` 绑定，服务端使用该租户加密保存的公司 `owk_` Key；浏览器和 DSH 都不会收到公司 Key。

在优化师编辑页填写本租户已启用的素材制作策略 ID 和飞书回写表格链接/token。`GET /api/tenants/{tenant_id}/ai-material-options` 提供租户策略目录。剧单勾选“下单素材”只保存意图；缺配置时不会猜测策略或发单。配置齐全后 Beat 提前处理已保存的未过期剧单，不必等到搭建时间。

v1 使用文档约定的素材库工作流：`GET /api/strategy-catalog`、`GET /api/material-library-production/capabilities`、`POST /api/schedules`、`GET /api/jobs/{job_id}`。不传集数字段，在整剧与默认集数能力下均遵守上游合同。成片推送仍复用现有素材回传/入库，搭建阶段按剧匹配，不将勾选下单等同于素材已匹配。

订单在外部请求前持久化冻结请求体与 `startTime`，幂等键为 `tkada-ai:<订单 UUID>`。超时/未知按上游同键同体幂等合同恢复；拿到 job ID 后只查原 job，不再提交新单。逐剧状态及 delivery 记录保留在日报证据中。日报明确区分意图、获得 job ID 的已下单、等待、未知、失败和实际搭建匹配。

## 独立 DSH 接入

当前灰度只对平台管理员（`is_superuser`）开放导航、页面及全部工作台/知识 API；租户管理员不在开放范围。机器领取接口使用独立服务凭据，不使用浏览器管理员身份。

部署方在 TK-ADA API/Worker/Beat 共用运行 Secret 中增加 `AI_DSH_SERVICE_TOKEN`，为空时不接受机器领取、不启动自动复盘，前端显示未配置。此项是服务凭据，不是业务功能开关。普通已有四开关保持原值；首次配置须核对目标、范围和实际加载值，不能将本地模板当作服务器已启用。

凭据至少 32 字节随机值，与浏览器登录、TikTok OAuth 和素材推送 Key 分离。不要在日志、命令行参数或源码中填写值。在 DSH 侧将相同凭据放入仅服务用户可读的 `0600` 文件，并注册本仓库 `integrations/dsh-ai-workbench` 原生插件。插件使用 DSH `0.2.2-rc.1` 的 agent、llm、session、tools、skill、scope 接口；依赖由 DSH 宿主提供，不修改 DSH 核心，也不直接复用旧本机投手服务。

插件配置对象（按宿主现有插件注册机制加载）：

```json
{
  "url": "https://<本环境 TK-ADA 域名>",
  "tokenFile": "/run/secrets/tkada-ai-review-token",
  "stateRoot": "/var/lib/dsh/tkada-reviews"
}
```

必须配置 DSH 默认模型；也可通过插件 `modelSelection` 显式选择已配置的模型。`url` 只允许 HTTPS 纯 origin；本机隔离验收可用 `http://127.0.0.1:<端口>`。生产域名、token 文件和状态目录由部署方明确提供，不能推断或复制测试凭据。

### 独立生产容器

`integrations/dsh-ai-workbench/deployment/` 提供锁定依赖和原生入口，使用 Node `24.22.2`、DSH `0.2.2-rc.1` 和插件 `1.3.0`。Dockerfile 在带编译工具的阶段安装依赖，再复制到 slim 运行镜像。必须构建目标集群的 `linux/amd64` 镜像；本机 ARM 启动成功不能替代目标架构容器检查。

从已推送公司 main 的干净提交构建配套镜像 `test/tiktok-ad-automation-dsh:<完整提交 SHA>`，Dockerfile 为 `integrations/dsh-ai-workbench/deployment/Dockerfile`，`VCS_REF` 使用同一 SHA。`NODE_IMAGE` / `NODE_BUILD_IMAGE` 可以指定已核验的官方镜像副本；2026-11-13 实际构建使用 `docker.m.daocloud.io/library/node:24.22.2-bookworm-slim` / `24.22.2-bookworm`。

启动配置如下，全部凭据由私有文件或 Secret 注入，不放进镜像：

- `TKADA_API_URL`：本环境 HTTPS 纯 origin。
- `/run/secrets/provider.json`：`{provider, model, config}`；`config` 按所选 DSH provider 配置，模型 Key 只填写 `apiKeyEnv` 引用。对应环境变量从独立模型 Secret 注入。
- `/run/secrets/tkada-ai-review-token`：与 TK-ADA 运行 Secret 的 `AI_DSH_SERVICE_TOKEN` 相同；Kubernetes 使用 `0440`、服务组读取，普通文件使用 `0600`。
- 持久化 `/var/lib/dsh`：`home/` 保存独立 profile，`workspace/` 为工作区，`reviews/<租户>/<优化师>/<任务>/<fence>/` 保存任务状态。不复制个人 DSH 会话和完整个人凭据库。

原生入口复用官方 `@deepseek-ai/dsh-web-app` 的 preset registry 和 standard preset，禁用个人插件管理、热更新及会话遥测。部署清单为 `deploy/k8s/ai-dsh.yaml`，渲染 `__DSH_IMAGE__` 后应用；模型 Secret 名为 `tiktok-ad-automation-dsh-model`。独立 app 标签保持原十一角色发布清单不变，20Gi PVC 满足当前云盘最低容量。管理入口由共享 HTTPS Ingress 的 `/dsh/` 提供，Service 仅暴露管理员代理 9081；9082 仅供 Pod 内官方 WebUI，`9080` 仅供集群健康探针：`/health` 表示进程在线，`/ready` 必须成功领取接口后才返回200。

镜像验收须在只读根文件系统、非 root 用户下运行原生测试。DSH 原生模块会将随包的库复制到临时目录加载；Docker 验收的 `/tmp` 若使用 tmpfs，需显式 `rw,exec`，默认 `noexec` 会出现 `failed to map segment`。生产清单使用可加载原生库的磁盘 emptyDir，保持根文件系统只读。

现有 Jenkins 仍发布 TK-ADA 十一角色，DSH 为单独配套组件。修改插件协议或方法时须同步构建/切换 DSH 镜像并核验配套版本；普通业务修复无需重启未变更的 DSH。模型配置变更后须重建 DSH Pod，不能把 Secret 文件更新等同于进程已加载新模型。生产实际版本与验收见 [2026-11-13 发布记录](../validation/2026-11-13-ai-workbench-production.md)，以下“未部署”描述保留其对应开发阶段的验收边界。

运行链路为：用户手动发起或 Beat 创建复盘 → TK-ADA 冻结优化师、策略、规则、计划、搭建、报表、知识和历史 → 插件领取并首次绑定方法包 → 返回五分钟任务级 JWT → 原生 DSH 独立会话通过 `skill` 读取主方法及相关专项 → `tkada_complete_review` 提交结构化结果 → TK-ADA 校验及发布。领取服务凭据不进入模型提示；会话仅开放受限 `skill`、`tkada_search_knowledge`、`tkada_read_knowledge`、`tkada_backtest_rule` 和 `tkada_complete_review`，不能执行 shell、读取凭据或调用平台。每任务目录按租户/优化师/任务/fence 分隔。

### 方法、插件与领域系统边界（2026-11-13）

- DSH 宿主负责模型、Agent 会话和原生 SkillRegistry。无需修改或嵌入 DSH 核心。
- 插件 `integrations/dsh-ai-workbench/skills/ai-delivery-review/` 是主 Skill 和五个专项方法的唯一源。插件按任务 scope 注册方法，通过受限的同名 `skill` 工具调用原生 registry，隔离宿主个人 Skill 和其他任务的方法。初始提示仅提供方法目录；主方法必须加载后才能正常完成，专项按需加载，结果记录 `method_usage`。
- TK-ADA 负责数据、知识/经验、历史反馈、授权、参数 Schema、提案校验和正式发布。业务后端不再读取或维护方法文件；任务中保存的正文/版本/哈希是插件首次绑定的归档副本，重试不随插件升级改变。
- 创建时业务输入上限 216,000 字节，为方法归档预留 96,000 字节、按需知识正文及元数据预留 200,000 字节，总上限 512,000 字节。领取时只绑定方法，不能再次截断已冻结业务输入。`input_budget` 描述业务输入部分；方法归档有独立容量校验。
- `/claim` 请求体现在必须是 `{main, references}` 方法清单，每项为 `{key, description, version, sha256, body}`。`main.key=ai-delivery-review`，references 固定 builds/rules/materials/business/scale，正文 UTF-8 哈希经服务端校验。插件不复制策略、规则执行器或知识数据库。

升级时 API 与插件 `1.1.0` 必须配套更新；旧版空请求体领取不再支持。切换前取消仍在运行或已冻结旧格式方法的待处理复盘，再重新发起新任务；历史已完成结果保留。这个边界调整无新增迁移，但首次交付知识库仍需此前的 `0127_ai_knowledge` 迁移。这里是发布准备说明，本轮未部署。

开发方法复用指南：[从业务工具到 AI 工作台](https://gz-junbo.feishu.cn/docx/BWDudD226o6A5cxEjvscVVOZnSh)。

机器端点为 `POST /api/integrations/ai-workbench/reviews/claim`，及任务级 `/{task_id}/heartbeat`、`/{task_id}/complete`。服务身份不能直接发布配置；任务 JWT 固定租户、优化师、任务、fence、audience 和有效期。每次完成/心跳重新核验成员权限与优化师状态。取消/重启立即使旧任务无法回传生效；原生插件心跳失败后中止模型会话。超期 RUNNING 可由新 fence 恢复，同一优化师复盘串行，等待模型不持有数据库事务或业务 Worker。

人工模式的全部提案逐条待确认；拒绝不改版本。自动模式只发布已授权字段与数值范围内的变更，其余留待人工审阅。预算/消耗/ROAS 使用绝对调整上限，搭建结构与规则条件/动作/时间分别授权。人工采纳是对当前具体变更的确认，仍受租户角色、账户交集、不可变基线、共享实体保护及现有策略/规则 Schema 约束，不扩大长期自动授权。金融数值须用十进制字符串。

## 验收边界

本地真实 PostgreSQL/Redis 验证与外部传输模拟分开记录，见 [v1 验收记录](../validation/2026-11-12-ai-workbench-v1.md)。没有配置真实模型、公司 Key、版权方和 TikTok 资产时，不把协议测试或本地页面显示视为真实下单、广告搭建、模型复盘或经营效果证明。


### Agent 主动知识检索（插件 1.2.0）

知识仍由 TK-ADA PostgreSQL 的版本表管理。创建复盘时只提供最多 20 条知识元数据目录，不注入整批正文；目录不完整也不限制后续搜索。插件提供两个任务级只读工具：

- `tkada_search_knowledge({query, offset?})` → `POST /api/integrations/ai-workbench/reviews/{task_id}/knowledge/search`。按标题、正文、适用条件做转义后的关键词子串匹配（空格分词，任一匹配），每页 10 条，返回 `next_offset`，不返回正文；不是语义检索，也不自动获取外部最新政策。
- `tkada_read_knowledge({knowledge_id})` → 同前缀的 `/knowledge/read`。读取被允许的具体版本，返回正文、来源、适用条件、有效期、验证状态、冲突提示及哈希，并归档到当前任务的 `evidence.knowledge`。重复读取返回同一副本，引用 ID 可用于提案、分析建议和经验候选。

两者只使用该任务短令牌，服务领取密钥不进入工具或模型。每次请求重新核验角色、任务 fence/有效期、运行状态、优化师版本及账户授权；任务行锁保护并发正文归档。任务完成、取消或范围改变后不能继续读取。

检索的知识版本截至任务 `captured_at`：公共知识 + 本租户知识 + 当前 BC 案例，排除候选/退休版本。截止后的更新只在下一轮使用，历史任务不会漂移。政策的业务生效日期需结合正文适用条件核验；`created_at` 是知识版本发布时刻，不代表政策生效日期。过期/来源未核实的条目标为背景，不能当作自动调整或到账依据。

读取每轮最多 20 条、正文 32,000 字符，总 JSON 仍受 512,000 字节限制；超额明确返回预算错误，不截断已冻结源数据。可以新增 `policy`、`experience` 标签，未引入向量库、对话录入或新的数据库迁移。实际赔付/补贴政策须通过知识管理接口录入和审核，接通检索不等于已有完整政策库。

部署需 API 与插件 1.2.0 配套更新；旧格式待处理任务按前述切换说明取消并重建。本轮只完成本地代码与协议验证，未部署或进行真实模型业务质量验收。

### 公共知识预置包导入

通过 `scripts/import-ai-knowledge-pack.py --help` 查看参数。管理员凭据只通过 `--token-file` 传入私有文件路径；远程 API 必须为 HTTPS 纯 origin，脚本拒绝重定向。导入会查询同 key 公共资产：相同正文/元数据跳过，变化则追加同资产版本；重复资产需人工整理。文件预置不代表已导入任何环境。

发布前阅读 [全分支上线审查](../validation/2026-11-13-ai-workbench-release-review.md)，其未完成业务项仍适用。

### 复盘事实与只读回测（插件 1.3.0）

API/Worker/Beat 与插件 1.3.0 配套，新增迁移 head `0128_material_production_origin`。升级前取消旧格式待处理复盘并重建；旧历史不补造指标。规则新执行会保存完整候选对象、父子关系、判断时原始指标、账户/窗口/版本及摘要；真实状态回读继续通过 TK-ADA 只读队列执行，模型不直接访问平台。

- `data_pack.reporting` 每账户/维度分为 D0、D3、D7 各自七个成熟完整日。D7 窗口覆盖当地今日前 8–14 日；另核验来源在成熟后已重新观测。复用既有 32 日每日回刷；未回刷、缺覆盖或平台不支持则降级，`revenue_total` 不替代 D3/D7。
- `rule_evidence[].execution_observations` 包含实际对象、期望/实际值、回执、时间、是否有效与差异；`submission_receipt_summary` 只代表受理。UNKNOWN/ACCEPTED/未核查均不能当作实际执行成功。核查失败保留未知，既有有限重试不重发写请求。
- `historical_replay` 提供有界候选/未命中指标和确定性重放；`tkada_backtest_rule` 对冻结的 `evidence_id` 与完整 `candidate_definition` 比较命中变化。任务范围/配置/令牌校验同知识工具，每轮十个不同候选，结果归档且可引用。超出原范围、指标窗口、数据缺失或截断会给出限制，不能称完整回测。不得推导实际节省或因果收益。
- `attribution` 只按本优化师真实成功 Campaign 身份关联投放策略版本；手工/外部广告仍进入账户及规则分析，不按同名剧强行归属策略。
- `material_orders` 冻结订单策略、排期、job 状态和失败原因，隐藏回写凭据/下载链接；订单成功不代表素材已用于广告。

素材制作策略归属需要生产方在既有已签名素材回传的每个素材中携带：

```json
{"material_id":"<生产文件唯一 ID>","file_name":"示例.mp4","url":"<下载地址>","production_origin":{"order_id":"<TK-ADA 订单 UUID>","job_id":"<原生产 job ID>","strategy_id":"<该文件实际制作策略 ID>"}}
```

TK-ADA 订单 UUID 可由发单 `Idempotency-Key: tkada-ai:<订单 UUID>` 对应，job/strategy 由生产方真实记录提供。服务端核对同租户订单及冻结 `payload.selectedStrategyIds` 后保存不可变推送修订，再关联实际库文件及验证的视频 ID。该声明来自已鉴权生产方，仍是观察关联，不证明因果；同视频多个策略来源有冲突时排除归属。**生产方尚未回传这些字段、旧历史无谱系或文件尚未成功入库时，明确不可按制作策略归因，不能由 Agent 补造。** 本轮仅完成 TK-ADA 接收、校验及读取合同，未改动未知外部生产工具、未执行外部联调。

## DSH WebUI（平台管理员入口）

DSH `0.2.2-rc.1` 已随包提供官方 `web` profile、模型设置页和 WebSocket transport；`headless` profile 是无 HTTP 的一次性任务模式，不能作为管理界面。部署入口复用官方 Web profile：原生 Web listener 仅绑定 Pod 回环 `127.0.0.1:9082`，管理代理监听 `9081`，Service 只暴露代理端口。代理每个 HTTP 请求和 WebSocket 升级都回查 TK-ADA `/api/admin/dsh/authorize`，只有 `is_superuser=true` 且 active 的平台管理员通过；不向浏览器暴露 DSH launch token、模型 Key 或原生 cookie。平台管理员从 `/api/admin/dsh/entry` 换取 15 分钟、`HttpOnly; Secure; SameSite=Strict; Path=/dsh/` 的短会话后进入 `/dsh/`。

共享 `test-https` Ingress 增加唯一的 `/dsh` Prefix 路由，后端为 `tiktok-ad-automation-dsh:9081`。先只读生成并审查补丁，再显式应用：

```bash
python3 deploy/k8s/dsh_web_ingress.py --kubeconfig .runtime/company-production-20261010/ack/kubeconfig --kubectl .tools/bin/kubectl-1.36
python3 deploy/k8s/dsh_web_ingress.py --kubeconfig .runtime/company-production-20261010/ack/kubeconfig --kubectl .tools/bin/kubectl-1.36 --apply
```

不要把 9082 加入 Service/Ingress；它只供 Pod 内官方 WebUI 使用。模型配置页写入 `/var/lib/dsh/home/profiles/tkada/cordis.patch.yml`，启动脚本保留管理员写入的 provider/model 设置，仅重写部署拥有的 Web listener、管理员代理和任务插件行。变更模型后重启 DSH Pod，确认 `/ready`、`/dsh/`、管理员与普通用户 403，再记录实际模型配置和 Ingress resourceVersion。


## 2026-11-14 大范围复盘合同（已生产发布）

应用/DSH `1ce9cf35`、schema0131已发布，原50账户真实复盘完成；来源时效与经营深挖范围仍须明确披露，详见[验收记录](../validation/2026-11-14-ai-review-scale.md)。

任务创建只保存范围、版本和复盘日期，返回 `PREPARING`，不在 HTTP 请求内计算全部账户报表。`ai_workbench.prepare_review` 在现有 `ads-reporting` 队列逐账户保存来源，进度与下一次 outbox 同事务提交。Beat 修复遗失投递，重复消息按 cursor/generation 幂等处理。全部账户准备结束后才进入 `PENDING` 供 DSH 领取；失败账户必须明确列为缺口，不能当零或声称全覆盖。

本轮新增 `0131` 独立证据来源、已读凭证和分析/笔记产物表。来源与读取账本不再随初始任务 JSON 发送给模型；模型输入大小不等于任务可保存证据总量。Agent 使用带账户/维度/成熟窗口筛选的目录查询、明细分页、全范围确定性聚合、排名和任务笔记。只有实际交付的读取、分析、知识和回测 ID 可以引用，笔记永不获得证据资格。

复盘窗口以请求时各账户当地日期固定；报表在逐账户准备时冻结，披露每个账户实际 capture/source observation 时间。它不是跨BC/全账户同一时刻数据库快照。来源冻结后重复读取保持原事实；规则/版本/授权/截止/成熟资格仍独立核验。金额按币种、时区、归因及成熟窗口分组，不能把平台广告收入当经营净利润或因果增量。

插件版本 `1.5.1` 必须与本应用配套部署。心跳在当前授权和版本仍有效时续签五分钟任务令牌，单会话最多两小时；取消/重启变更 fence，旧令牌不能读取或完成。Agent 每个主要发现写任务笔记，恢复后核对笔记与冻结证据，不必从头把全部明细再次送给模型。宿主 shell、网络、数据库和个人文件工具仍不开放给业务会话。

本改造不增加部署级开关、Worker 并发、数据库池或平台调用额度。发布使用已审查增量迁移和新旧消费者协调：停独立DSH领取、正常冻结排空现有应用、迁移、新版消费者启动、API与Beat恢复、配套DSH启动。普通发布按用户偏好不备份/不恢复演练；现有备份原样保留。已完成历史保留，旧未完成复盘取消后用新合同重新发起，不将旧任务结果改成成功。

1.5.1领取时附source_catalog（仅类别、来源/账户/行数），空business摘要不等于data_pack_business也为空。分析返回确定性覆盖百分比。首次完成工具仅返回核验清单，Agent核对数据口径、比例、同币种不同时区/窗口分组、经营明细及因果措辞后再次提交才落正式结果；这提高自检机会，不证明所有模型结论自动正确。
