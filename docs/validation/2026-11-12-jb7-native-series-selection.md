# JB-7：原生 Series 选择与 scoped 准备

> 最新验收修复见文末「验收修复」。该节取代下文初版的全局 UNVERIFIED 与未验证浏览器/基线结论；初版保留为历史。

## 源码与交付状态

- 源码提交：`5f66a6f65ceba042a2ddfc441f98a8ebd717be60`。
- 本票起点：`77baaf04b745b0506372dbc3845aec2adf0bae0c`；集成主线基准：`da190b59d66bc08b1e67890e31a250af50a5a7d2`。
- 工作树：`projects/tiktok-ad-automation.wt/feat-jb5-native-series-m1`，分支 `feat/jb5-native-series-m1`。本地候选，交 root 审查；未写 Linear、推送、部署或真实广告/授权/版权方接口。
- 不宣称 M1 完成。原生组合仍为 UNVERIFIED；目标视频和封面逐条准备归 JB-8，预览/执行/回读归后续票。

## 实现与 Interface

- `CreateDraftRequest` / `PatchDraftRequest` / `DraftSummary.scene_selection` 为判别联合。Mini 的链接仍由原 PromotionLink/MiniTarget owner 保存；身份三元组统一由 scene_selection 保存，旧独立身份列退休，展示名称仍为展示元数据。
- 原生选择保存精确账户、Series、两个独立角色、非空地区、有序视频/封面来源 UUID。预算、ROAS、文案与 CTA 继续引用既有不可变策略版本；结构覆盖可随创建或原生修改一笔保存。没有新增素材分配或文案组合算法。
- 未选定身份或 Series 时使用显式 null，允许先保存账户/地区、准备真实身份目录，再选身份准备 Series，最后保存精确 Series。没有伪造剧目、Mini 链接或占位资产。未配置状态产生 BLOCKED/选择必填原因，不能就绪。
- 原生修改复用 mutations `_apply`、草稿行锁、revision CAS、幂等回执；完整替换目的地，并使账户/准备失效。草稿重放不增加 revision，冲突不覆盖已保存事实。旧省略目的地请求摘要保持原文。
- 复用 SceneJob / SceneJobPage / outbox / 当前能力 owner / route 围栏 / 首次观察 TTL / 有界错误恢复。basis 包含完整原生输入及冻结 route（租户、BC、连接、授权与 binding 代数）；不引入平行目录或资格服务。
- `OfficialScenesGateway.read_page` 增加原生资源：两个身份类型各自真实页码流，以及固定 `GET /identity/native_series/get/` 的 cursor/count=20/has_more 流。复用 SDK ApiClient、OfficialReadRequests、gateway read 能力和物理配额注册。Series 只映射真实资产字段；身份能力保留 AVAILABLE/Series/视频/GPPPA 的原始响应值。
- `GET /build-drafts/{draft_id}/native-checkpoint` 返回当前 revision、scope、route、真实页 evidence IDs、观察与到期时间、完整目录及原因。`read_scene_context(..., native_selection=..., draft_revision=...)` 投影相同事实，supported 保持 false。地区来自账户/版位工具，不伪造 Series 地区字段。
- 当前没有已证实的直接模式身份组合授权证据，完整可见目录仍返回 `native_series_identity_combination_unverified`，同 ID 不推定 READY；明确不可用身份、不可见资产与地区分别阻断。MCP 无已核实的 Series 工具，原选 route 明确不支持，不切换连接。
- 创建工作区增加目的地表单、两个角色、精确资产/真实目录选择与来源输入；准备页显示独立检查点。OpenAPI 客户端由当前 app schema 生成。

## 迁移

1. `native_series_selection`，父 `0118_sync_schedule_service_age`：增加草稿/SceneJob JSONB 选择、原生来源互斥约束和逐页资源枚举。
2. `native_series_identity`：把旧 Mini 身份三元组迁到 scene_selection，迁移旧读取、选择与复制调用方，删除三个旧可变身份列。冻结 Preview/ExecutionRequest 不修改。

历史独立数据库验证升级和降级：旧身份可逆还原，草稿其他字段及冻结预览 JSON 原文不变。存在原生选择/任务时第一步降级拒绝丢弃数据。发布仍需 root 复核这两条迁移及发布方案；没有运行生产迁移。

## 验证证据

所有 Python 命令从当前树 backend 运行，统一前缀：

```sh
python3 /tmp/tkada-native-series-20261112/implementation/run-test.py python -m pytest
```

wrapper 使用专用真实 PostgreSQL/Redis、锁定隔离 venv、APP_ENV_FILE=/dev/null 和当前 checkout PYTHONPATH；未读取主仓 .env。SDK 替身在 urllib3 传输处，测试禁止外网业务请求。

| 验证 | 结果与边界 |
| --- | --- |
| 保存首红 `test_native_series_selection.py` | 1 failed / 1 passed：合法原生创建因 schema 不支持 scene_selection 失败；混合输入被拒绝。随后最小实现与旧 drafts 合计14 passed。 |
| 保存近似错误 | 临时让修改保留旧 scene_selection，真实检查失败；恢复并 cmp 字节一致。 |
| 游标近似错误 | 临时移除 has_more 时 cursor 必须前进的校验，真实检查失败；恢复并 cmp 字节一致。目录检查不是每个分支均在实现前写完，不能声称逐条完整先红。 |
| 最终原生/迁移专项 | `tests/modules/builds/scene_jobs/test_native_series.py tests/modules/builds/test_native_series_selection.py tests/modules/builds/test_native_series_migration.py -q --tb=short`：11 passed，14.54s。 |
| 原生专项行为 | 真实 workspace 创建→prepare→能力→SceneJob→SDK→checkpoint；保存/幂等/CAS；TT_USER 51条两页 + BC_AUTH_TT 空终页；Series两页同名不同ID；错误cursor；空目录/重复ID/缺has_more；待选择状态不查假Series；scope改变不复用旧目录；素材始终非READY。 |
| 原生 SceneContext 接线 | 增加投影后5 passed，9.19s；冻结 route、精确 Series 和 revision 原样保留，supported=false。 |
| Mini场景/任务回归（身份退休前） | `test_drafts.py scene_jobs scene/test_scene_route_versions.py scene/test_scene_concurrency.py test_draft_catalog.py -m 'not mcp'`：129 passed / 9 deselected，170.89s。 |
| 身份退休后 owner 回归 | `test_native_series_selection.py scene_jobs/test_native_series.py scene_jobs/test_draft_bootstrap.py test_previews.py test_native_series_migration.py -m 'not mcp'`：91 passed / 1 failed，126.80s。失败见下文；之后原生专项11 passed。 |
| 官方 Mini合同回归 | 与原生、迁移专项一起16 passed，17.81s（身份退休前的记录）；没有真实平台接受性声明。 |
| 前端 | `bun run build`：真实 TypeScript + Vite生产和demo构建通过（最终含待选择状态）。 |
| 浏览器代表交互 | `PLAYWRIGHT_PORT=5187 bun run test --project=workspace tests/build-preparation.spec.ts -g '原生草稿原子' --reporter=line`：1 passed，7.4s。真实 Chromium、API显示替身，只证明双身份/Series/地区保存表单；不代替业务链证据。复用软链字体有Vite allow-list警告，不影响此交互。 |
| 静态 | 改动Python Ruff检查/格式通过；TSX定向Biome格式；git diff --check通过。未运行全仓unsafe lint。 |

## 未解决的验证缺口

- `tests/modules/builds/test_previews.py::test_batch_cap_blocks_before_any_account_expansion` 预期抛出“拆批”但未抛出。本票未改预览上限算法、该测试主体及其输入；`previews.py` 工作树 blob 与本票起点完全相同。这仅支持“未修改该owner”的静态判断，不是整个旧HEAD动态复现，不能宣称已确认基线失败。保留给 root 审查，不篡改组合算法或放松断言。
- 首次目录发现→选择身份→选择Series→刷新回显的完整浏览器路径尚未独立跑完；代表浏览器验证仅覆盖最终原子保存表单。业务目录及准备链在真实PG/Redis和SDK传输替身下已运行。
- 未对每一种原生权限撤销/TTL/并发竞态追加独立测试；共享owner已有回归已运行，不能把复用回归描述为所有原生交错均已验证。没有运行形式验证器。
- 没有真实业务目录/组合支持/平台广告接受性证据，不存在真实投放验收或全素材READY。

## JB-8可消费事实

- 原生不依赖 ProviderDrama/PromotionLink；来源在 `scene_selection.video_sources`，每条为 material_id 与可选 cover_material_id，不是目标账户 VID 或封面READY证明。
- 策略/结构、单账户、精确Series和两身份及地区已经持久化；完整场景目录/页证据有当前scope。组合缺证据的 UNVERIFIED 必须继续保留。
- 后续素材层可沿当前 mutation/revision/准备链补逐条目标视频和封面事实；不能由目录COMPLETE、ScenePreparation内部ready或无链接推断整份草稿READY。


## 验收修复（2026-11-12）

起点 `46e15d575110ab19ecffae0199d16846040b6953`。只写原集成树，保留 untracked node_modules 软链；没有派下级代理、更新 Linear、推送、PR、部署、读取生产凭证或调用真实业务接口。使用 implement-spec / tdd，按用户批准复用集成树，覆盖逐票新树与 merger 步骤。

### 正向资格合同及来源

直接模式官方[搭建指南](https://business-api.tiktok.com/portal/docs/create-an-upgraded-smart-mini-series-feed-campaign/v1.3) Step 2 Identity / Drama Series 要求有效承载身份及从该身份可访问的精确 Series；Step 3 Identity 指向账户内有效 Spark 身份，没有要求额外的“组合认证”字段。[身份读取](https://business-api.tiktok.com/portal/docs/get-the-identity-list/v1.3) 明确 AVAILABLE、can_use_series（Series 访问）、can_push_video（创建/编辑视频）、is_gpppa（为 true 不能创建 Spark Ads）的语义。[可用 Series 读取](https://business-api.tiktok.com/portal/docs/get-the-available-tiktok-series-within-an-ad-account/v1.3) 限定账户、身份类型/ID/授权 BC。实际复核原始官方快照 `/tmp/tkada-research-identity-roi2-20261112/` 的相应 Markdown/JSON；SOP revision 4230 的 Series 与普通视频授权分离要求见 `/tmp/tkada-native-series-20261112/official-sop-baseline.md`。没有重新调用真实 TikTok 目录。

由这些合同形成的有界实现推断：同一完整身份三元组可同时承担两角色，但 READY 必须联合当前账户 can_build / scope_verified / 未过期能力、冻结 route 的当前授权核验、完整双身份分页、AVAILABLE、非 GPPPA、Series 访问与视频发布权限、按承载身份查询到的精确 Series、账户 TikTok 版位国家目录及未过期场景证据。单独 ID 相同不能放行。不同身份组合缺特定支持证据仍 UNVERIFIED；没有自动换身份、同身份全局限制或业务剧名地区唯一性。

明确不可用、不可见资产、无效地区、过期证据分别阻断；能力字段缺失保留 UNVERIFIED，不将缺省值当许可。SceneContext.supported 反映上述场景支持；materials_ready 始终 false，完整草稿仍报 native_materials_not_prepared，等待 JB-8。READY 是本地目的地/身份检查点，不能证明平台已接受广告、签约/审核/经营资格、占用无冲突或素材就绪；这些未返回事实不伪造成门禁字段。

### 浏览器修复

Series 目录身份匹配改为逐字段比较完整三元组并匹配目标账户，消除 JSON 属性顺序导致的目录隐藏。真实 Chromium 走新建未选身份草稿 → 准备发现身份 → 选择 TT_USER 承载身份和 BC_AUTH_TT 创意身份 → 保存并准备 → 选择承载身份下同名但精确 ID 的 Series → 保存 → 刷新回显。Select 增加可访问名称。浏览器使用 API 显示替身；下面真库/SDK 业务验证独立提供业务链证据，二者均不是实际 TikTok 联调。

### 动态历史基线

命令：从集成树根运行 `/tmp/tkada-native-series-20261112/implementation/venv/bin/python /tmp/tkada-native-series-20261112/implementation/baseline-check.py`。脚本 `git archive 77baaf04` 到独立临时源码目录，通过私有 test-env.json 仅在进程内加载本机配置、新建随机 `_test` 数据库，将 PYTHONPATH 显式指向归档 backend，以锁定 venv 的 `python -m pytest tests/modules/builds/test_previews.py::test_batch_cap_blocks_before_any_account_expansion -q --tb=short` 运行，结束后删除独立库。当前集成库没有降级。

旧源码：1 failed / 1.95s，`DID NOT RAISE DomainError`；当前相同输入：同样失败（与原生专项合跑为11 passed / 1 failed，23.25s）。日志：`/tmp/tkada-native-series-20261112/implementation/jb7-baseline.log` 与 `jb7-green.log`。归因：既有提交 `82277b082b6f9569319a064e0c594dca9574abb7` 已删除最终广告总数上限，structure.py / previews.py 注释明确不再按总数拒绝，旧单测仍期待100组×20广告触发“拆批”。这是本票之前已存在的断言与产品规则冲突；本票不修改该断言、组合算法或总数限制。

### 修复验证

- 新正向检查首红：2 failed / 9 passed，19.54s；READY 与 workspace 素材边界断言拒绝原全局 UNVERIFIED 实现，日志 `jb7-red.log`。
- 官方SDK urllib3传输替身、真实PG/Redis：11项原生行为通过，涵盖完整多页、精确资产、同身份联合证据 READY、缺 Series 权限 BLOCKED、缺视频权限字段 UNVERIFIED、不同身份 UNVERIFIED、场景过期、变目的地不复用及工作区准备到素材边界。账户能力证据过期另补当前消费检查。
- 浏览器：`PLAYWRIGHT_PORT=5187 bun run test --project=workspace tests/build-preparation.spec.ts -g '原生目录首次' --reporter=line`：1 passed / 8.2s；启动前 lsof 确认端口无监听，没有终止他人服务。存在既有软链字体 Vite allow-list 警告，不影响交互。
- `bun run build`：TypeScript、Vite生产和demo构建通过；OpenAPI客户端重新生成后仅 NativeCheckpoint.state 增加 READY，生成器空白噪音已清除。

- 准备链/Mini/route/并发/迁移回归：`tests/modules/builds/scene_jobs tests/modules/builds/scene/test_scene_route_versions.py tests/modules/builds/scene/test_scene_concurrency.py tests/modules/builds/test_native_series_selection.py tests/modules/builds/test_native_series_migration.py tests/modules/builds/test_drafts.py tests/modules/builds/test_draft_catalog.py -m 'not mcp' -q --tb=short`：141 passed / 9 deselected，185.76s，`jb7-regression.log`。
- 最后增加账户能力过期消费及重新排队刷新后：原生 SceneJob、选择、迁移专项14 passed，24.29s，`jb7-final.log`；准备沿同一能力 owner 刷新，不额外重复目录读取。
- 近似错误探针：临时把缺权限字段当许可，missing_permission 实例失败；恢复旧 JSON.stringify 身份比较，完整浏览器在精确 Series 下拉缺失处失败。两者均恢复并 cmp 字节一致，日志 `jb7-probe.log` / `jb7-ui-probe.log`；恢复后专项14 passed、浏览器两项2 passed / 7.5s（`jb7-browser-restored.log`）。没有为所有权限交错声称逐条变异或形式证明。
- Ruff check / format 与 git diff --check 通过。锁定 venv、真实PG/Redis、SDK传输边界替身规则沿用前文；所有日志在 `/tmp/tkada-native-series-20261112/implementation/`，不含私有环境值。

判定：本地 JB-7 三项缺口已关闭，可交协调者验收；本节所在提交是修复源码 tip。仍需协调者独立审查。JB-8负责素材逐条READY；预览冻结、执行、回读、恢复分别归后续票。真实平台最终准入/经营资格及任意不同身份组合未验收，JB-8至12未完成，不宣称 M1 完成。
