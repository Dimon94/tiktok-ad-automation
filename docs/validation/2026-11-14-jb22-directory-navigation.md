# JB-22：无报表目录导航本地验证

## 版本与结论

- 起点：`55ee37146d7275b8c308161bce1986f9fbbb1670`。
- 实现与测试源提交：`2701fbfad95c316d8d5a49d8918fafdb2164edf4`。
- 唯一写入树：`projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3`（外层工作区下），分支 `feat/jb21-native-series-m3`。既有 `.venv`、`node_modules` 软链保留。
- JB-22 本地 AC 已验证，作为同一 M3 MMF 的实现候选交给 JB-23；不是 M3 完成或独立发布结论。未做独立 Standards/Spec review。
- 无 push、PR、main 合并、Linear 写入、部署、真实平台业务请求或生产数据操作。

## 实现与数据归属

原链路由 ReportRow 驱动详情，关联对象继续查询报表行；目录的 PostgreSQL `list_objects` 和无 snapshot 的详情 HTTP 已存在，但页面缺少入口。本票复用原查询/详情 owner，删除零调用的 `_parent_public` 占位函数，没有新 workspace、同步器、Series 实体、报表行或平行 service。

目录入口形状为 `DirectoryDetailEntry = { source: "directory", ref: EntityRef }`。真实账户选择复用 `DirectoryPicker` 和现有账户 HTTP；现有广告工作区内可进入系列、广告组、广告、创意，沿父引用返回。目录详情直接请求原 `ad_detail`，无 snapshot 参数，也没有管理回调或可冻结选择。已有报表详情、关联跳转和管理入口保持原合同。

新增 `GET /api/tenants/{tenant_id}/ad-directory`：

- 必需 `bc_id`、`advertiser_ids`、`kind`；可选 `parent_kind`、`parent_remote_id`、`parent_advertiser_id`、`cursor`、`limit`（1–100）。单账户下父账户可省略。
- 返回 `items`（真实 `ref/name/parent/observed_at/published_version`）与 `next_cursor`。
- SQL 复用 `list_objects`，按 `(advertiser_id, remote_id)` 排序和 keyset 继续读取；同名或跨账户同 ID 不合并。只取 limit+1，不加载完整清单再切片。
- HMAC 游标绑定 tenant、BC、完整账户集合、kind、完整父范围。复用仓库签名分页模式；账户现有单 ID 游标与素材 UUID 游标不能表达此复合键，故编解码留在目录 owner 内。
- 每页仍由原 `list_objects` 重验当前授权，父对象经原 `locate` 定位；签名不代替授权。越界对象/账户/BC 返回原拒绝或 404；游标不匹配返回 400。
- 不承诺跨页同步更新下的原子快照；每个对象保留自己的观察时间与版本。页面明确“当前页不代表平台完整清单”。

原详情 DTO 新增 `published_version/source_connection_id/source_channel`，保留 configuration、parent、materials 和既有报表 snapshot 语义。目录报表页签明确无报表上下文；创意通用详情保留原配置。原生 Series、两层身份及素材关系投影仍属 JB-23。

## 隔离与最高 Seam

依照 `config/README.md`，仅使用本机独立 PostgreSQL `tkada_native_m3_20261114_test`；不修改共享数据库。复用 `/tmp/tkada-native-series-20261112/implementation/venv` 的已安装依赖。M3 环境 runner 位于 `/tmp/tkada-native-series-20261112/m3-implementation/run-test.py`，私有测试参数不入库、不输出。未读取部署凭据、未 FLUSHDB、未重新安装依赖。

主 Seam 是实际 `/tenants/{tenant}/ads` 工作区 → Vite 的真实 HTTP 代理 → 原 FastAPI routes → PostgreSQL。测试只绑定认证操作者和测试数据库依赖；浏览器不 route/intercept/fulfill 业务响应，业务 service 没有 mock。测试驱动撤权端点只存在于测试宿主并在 finally 移除。目录事实直接写入独立测试库的现有发布表；本票不调用 SDK、不模拟目录同步，也不将其说成平台同步验证。JB-23 的原生字段端到端仍须补官方 SDK transport 场景。

## AC 与证据

| 验收 | 已运行证据 |
| --- | --- |
| Testing 7：读取边界与撤权 | 新 HTTP 检查无认证、跨 tenant/BC、越界父账户和授权账户集合变化；原目录测试复用 membership/grant/connection/account 撤权。浏览器在已浏览详情及第二页后撤权，下一详情和下一页显示拒绝并隐藏旧数据。 |
| Testing 8：全部无报表的父子链 | 真工作区从账户选择开始，22 个同名根对象分页，打开 c1→g1→a1→cr1，再从父链接返回；另以两个父范围及跨账户同 ID 检查不会串页。 |
| Testing 9 本票范围 | 列表与原详情 HTTP 返回真实对象/父引用、原配置、来源连接/渠道、观察时间与版本；仅目录请求后该租户 QuerySnapshot 和 AdDirectoryRun 仍为空。 |
| 稳定且绑定范围的分页 | 重复名称按复合身份排序，下一页无重复、终页 cursor=None；篡改游标、改 kind/BC/账户集/父范围均拒绝，跨账户同 ID 保持独立。 |
| 报表入口与管理边界 | 真实目录请求不带 snapshot；目录报表页签显示无上下文、无管理按钮。旧报表详情打开、页签、层级返回、只读和切 BC 回归通过。 |

独立预期来自 JB-22 的 Testing 7/8/9、scope-bound cursor 和只读入口 AC，不从实现生成预期值。

## 初始失败与修复

- 相关基线为 18 passed。新 HTTP 检查先 RED：目录列表 URL 返回 404，证明缺失公开入口；增加原 owner 的列表契约后 GREEN。
- 首次测试环境准备误用系统 Python 加载 Python 3.14 binary，改用既有 venv 的解释器；路径/cwd 错误均不算行为 RED。
- 浏览器账户选择器初用 button 定位，实为 combobox；随后夹具缺 BCDefaultRoute/BCConnectionBinding 被原账户 API 正确拒绝。修正定位器并补真实数据库绑定，没有改产品授权规则。
- 跨账户测试初缺 currency/timezone，被原读取授权拒绝；补齐测试账户元数据，不放松原授权。
- 旧报表测试的“目标 ROAS”文本同时命中 dt/dd；按可见 term 定位，保持原用户预期。修复后四条回归全绿。
- 初次 TS 构建不支持 Array.at，改用现有 target 支持的索引。正式/demo 构建最终通过。

错误实现探针实际执行：将 keyset `>` 改 `>=`，两条分页检查失败；使 `_authorize` 提前返回，跨 BC 的详情原应 404 却返回 200，被检查拒绝。两次均仓外字节备份、finally 恢复、cmp 成功，再跑 GREEN。脚本与原始日志在 `/tmp/tkada-native-series-20261112/m3-implementation/` 的 `probes.py`、`repeat-page-probe.log`、`authorization-probe.log`。

## 命令与最终结果

所有 backend 命令从任务树的 `backend/` 执行：

```bash
python3 /tmp/tkada-native-series-20261112/m3-implementation/run-test.py python -m pytest tests/modules/ads/test_directory_navigation.py tests/modules/ads/test_directory.py tests/modules/reporting/test_detail.py -q --tb=short
python3 /tmp/tkada-native-series-20261112/m3-implementation/run-test.py python -m pytest tests/modules/ads/test_directory_navigation.py -q --tb=short
```

第一条 21 passed/14.30s（含浏览器）；随后扩展浏览器缓存撤权检查，第二条 3 passed/16.82s。未变的原有 18 项复用第一条结果。日志 `final-tests.log`、`browser-revocation.log`。三项 warning 来自现有 uvicorn/websockets/Python 弃用提示。

任务树 `frontend/`：

```bash
PLAYWRIGHT_PORT=5200 bun run test --project=workspace tests/ads-detail.spec.ts -g '点名称打开详情|层级路径可多层|只读成员详情|切 BC 关闭' --reporter=line
bun run build
```

报表回归 4 passed/6.7s；TypeScript、正式与 demo Vite 构建通过。Vite 开发模式对共享依赖中的字体有 serving allow list 提示，未阻断导航断言；未扩展任务修改共享依赖或 Vite 配置。相关 Python Ruff、前端 scoped Biome 和 `git diff --cached --check` 通过。后续纯文档记录不新增测试。

## JB-23 消费合同与边界

沿本树和本实现提交继续，不新建 checkout。复用 `ads.directory.read_directory_objects`、原 `reporting.detail.get_ad_detail`、生成客户端、`DirectoryDetailEntry` 与 `DirectoryDetailBody`，在原详情 owner 增加 native 投影并在目录/报表两来源展示；不把创意变成报表维度。`DirectoryObjects` 只读本地发布事实，不能承担同步或原生关联推断。

JB-23 需完成原生 Series/组与创意身份/版位/父组与创意素材关系、缺失和覆盖限制，以及全 M3 主 Seam 的 SDK transport→发布事实→页面验证。保留来源、版本、原配置与各层时间；仍不承诺整棵目录的原子视图。真实 TikTok 字段支持、覆盖完整性和生产验收 Unknown。M3 两轴独立审查与最终集成归协调者，本票未执行。

DRY record：scope=目录入口/分页/通用详情；searched=ads.directory、reporting.detail/api、accounts resolver/material repository 游标、DirectoryPicker、AdsDetailSheet；reused=原授权/目录 SQL/详情 HTTP/账户选择器/生成客户端/设置页签；remaining duplication=范围签名沿已有 HMAC 模式但复合身份不同，不共用账户或素材专属编码；无第二业务 owner。

Blast radius 的关键安全事实已实跑：目录来源无法形成 ReportRow 或冻结管理选择，而原报表来源仍保留操作与层级读取。此证据限本地 HTTP/PG/浏览器，不代表部署或平台验收。
