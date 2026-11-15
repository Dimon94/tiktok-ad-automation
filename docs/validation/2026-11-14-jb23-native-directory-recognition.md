# JB-23：原生目录识别本地候选

起点 `4ddb3e0031b53f1168b22a7185399285f0daa457`，分支 `feat/jb21-native-series-m3`，唯一写入树为本目录所在的 `projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3`。原 `.venv/node_modules` 链接保留。候选供 root 独立 Standards/Spec 审查，不代表 M3 放行；不 push、PR、deploy、改 Linear、合 main 或删除树。

## 用户行为与 owner

目录及报表详情的设置区共用 `DirectoryConfiguration`，原生广告组可直接识别 Series ID、组身份三元组、版位和 TikTok 子版位；广告可识别真实父组、Series、逐 creative 身份及平台素材 ID。空字符串渲染为 `""`，空数组为 `[]`，未知为“未提供”。未覆盖关系和未知完整性保留原因。普通 Minis 保留通用配置；不按对象名判断原生。

- `ads.directory.list_native_creatives`：复用当前租户/BC/账户授权与父广告定位，读取现有 AdCreative；不从报表、素材名或数组位置反推 creative。
- `reporting.detail`：唯一归一化 owner。父组取真实 parent_ref；creative 配置和素材事实按相同 creative ID 连接。目录子对象配置优先，内嵌 creative_list 只补同 ID 尚无独立配置的条目；无 ID 条目独立保留。顶层广告原配置完整保留，包括同 ID 的内嵌观察。素材名只匹配完整素材身份，歧义返回未知。
- DTO 保留父配置、逐创意配置、对象/父级/创意/素材观察时间与版本。现有 AdCreative 没有来源连接列，素材关系来源连接/渠道明确返回未知，不能借用广告观察冒充素材来源。创意配置来源取实际承载该配置的对象。
- `DirectoryConfiguration`：只格式化同一 HTTP DTO，两入口共用；`configFields` 补默认字段映射，未映射字段保留。原目录导航、报表管理授权与 snapshot 含义不变。

未改 SDK adapter、同步/发布实现、持久模型、迁移、同步调度或部署配置；没有 Series 实体/缓存/名称补读、资金、资格或管理动作。现有模型没有广告级全部创意已覆盖的证明，所以即使有已观察关系也返回 `creative_coverage:unknown`，没有关系时 creatives=None，不伪造确定空清单。

DRY record：scope=原生详情只读投影；searched=ads.directory、AdCreative/creatives、reporting.detail、SettingsTab/configFields、两个详情入口；reused=原授权/父引用/已发布目录/创意素材身份与同一配置组件；remaining duplication=目录与报表加载上下文各自保留，事实投影和展示不重复。

按 model-the-domain 使用只读 DTO，按 subtract-before-you-add 移除两入口内直接拼装设置的重复展示，按 minimize-reader-load 保留一个事实 owner 和一个共用展示组件，没有额外 view-model/cache 层。blast-radius 的关键安全事实已实跑：原报表详情/层级/只读行为仍通过；目录没有管理动作，撤权后不显示旧原生数据。

## Seam 与隔离

复用 M3 runner `/tmp/tkada-native-series-20261112/m3-implementation/run-test.py`，从任务树 backend 执行；仅独立 PostgreSQL 测试库，不读取生产配置，不安装/修改共享依赖，不 FLUSHDB。

`test_native_directory.py::native_published` 使用真实官方 Python SDK 的 urllib3 transport 受控响应，真实 `SdkAdsReadOperations.read_primary_page` 解析，逐层经过现有 `stage_directory_page` 和 `publish_directory` 写入 PostgreSQL，再由真实目录列表/详情 HTTP 和 FastAPI/Vite/Playwright 工作区读取。实际授权用数据库的 `verify_route`，浏览器不 intercept/route/fulfill 业务响应，业务 service 不 mock。只替换 SDK transport；认证操作者与 DB 依赖绑定测试宿主。SDK 请求使用合成 token，物理请求替身只允许/断言 GET。

主对象 c1→g1→a1→cr1/cr2 来自该传输/发布链路，不以直接 seed 发布表冒充同步。测试复用 JB-22 导航宿主补充分页根对象；缺失/旧观察/多素材组合另在已发布测试事实中显式构造，不能称为真实平台返回。使用同步模块的真实暂存/发布 Interface，未启动 Celery broker/Beat 或远端 Worker；未验收调度器运行能力。

## Spec Testing 1–10 矩阵

| Testing | 本票证据 |
| --- | --- |
| 1 完整识别 | `test_native_transport_published_http` + `test_native_workspace_live_http`：SDK transport→同步/发布→PG→真实工作区逐层导航；Series、两层身份/授权 BC、版位/空子版位、两个 creative 与共享平台素材 ID 默认可见。来源/观察/版本由同一详情 HTTP 提供。 |
| 2 同名不同 Series | SDK 返回两个同名组 g1/g2，HTTP 分别断言 series-1/group-1 与 series-2/group-2；列表身份沿 JB-22 不按名字去重。浏览器从同一父级分别打开 g1/g2 并核对两组 Series/身份。 |
| 3 两层身份 | HTTP 与浏览器同时断言 group-1/group-bc-1、creative-1/creative-bc-1、creative-2/creative-bc-2；组身份借用探针实际失败。 |
| 4 精确关系 | 内嵌 creative_list 顺序为 cr2/cr1，独立创意为 cr1/cr2，同视频属于两个 creative；组合检查 cr1 的两个平台素材，未关联的 missing/匿名创意不借素材。错误 creative ID 连接探针失败。不读取 report creative facts，主路径无消耗报表。 |
| 5 缺失/空值 | `test_native_group_and_unknown_fields`、`test_partial_observations_and_exact_creative_identity`：Series/身份/子版位未知、空字符串/空数组、缺父级/creative ID/关联/素材、普通 Minis、原配置保留；共享页面将 None 与真实空值分别格式化，完整主路径同时验证缺 Series 名称时仍可识别。 |
| 6 部分/旧观察 | 部分事实保留；父观察 2020、素材观察 2021、素材版本99，响应不被广告/creative版本覆盖。页面明确非实时/非完整/非整树同版本；缺素材名称未知。 |
| 7 权限 | 复验 `test_directory_navigation.py` 与原 `test_directory.py` 的跨租户/BC/账户、分页撤权；原生浏览器实际撤权后下一详情拒绝、隐藏来源和内容。 |
| 8 零消耗 | 真实工作区账户选择→目录分页→c1/g1/a1/cr1→父级返回；没有报表行依赖，原生主路径重复执行，同页仍无管理按钮。 |
| 9 完整 HTTP | 对象/父配置/creative配置/来源/观察/版本/关系与限制；当前事实与有效旧publication版本的 snapshot 请求得到同一 native，证明 snapshot 只是读取上下文，不是历史目录快照。 |
| 10 映射 | `ads-detail-config.spec.ts` 新增默认原生映射与 unknown_native 保留断言；其余配置映射回归全部通过。此检查不替代 SDK→页面主 Seam。 |

组合缺失场景主要在 HTTP 验证，浏览器使用完整代表场景和已有撤权导航。没有为每一种缺失组合重复浏览器场景；root 可按风险决定是否补充。

## 红绿、探针与最终结果

独立预期来自 root 定稿 Spec Testing 1–10 与 JB-23，不从当前实现生成预期。

- 初始 HTTP RED：`KeyError: native`。新增投影后检查发现夹具已有 cr1，原测试错误预期 creatives=None；按前票真实夹具改为 cr1 身份未知，未更改产品来隐藏该事实。
- SDK/发布夹具最初缺 checked_at，被真实授权拒绝；补测试授权证据。SDK ApiClient 没有 close 方法，去掉无效清理；发布使 ORM 观察过期，导航宿主复制 fixture 前显式 refresh。均为夹具修正，不放松产品规则。
- 初次字段映射预期将单项数组渲染为 JSON，但既有 configSections 的公开约定是单项文本；保留该约定，原生共用详情仍用 JSON 表达精确空值。几次脚本 cwd/路径错误没有产品写入，不算行为 RED。
- 仓外备份真实源码，运行 `jb23-probes.py`：组身份复制到创意 → `test_native_transport_published_http` 失败；把所有 creative 连接第一条素材事实 → `test_partial_observations_and_exact_creative_identity` 失败。finally 恢复、字节相等。日志 `jb23-probe-group-identity.log`、`jb23-probe-wrong-creative-id.log` 位于上述 runner 目录。

从任务树 backend：

```bash
python3 /tmp/tkada-native-series-20261112/m3-implementation/run-test.py python -m pytest tests/modules/ads/test_native_directory.py tests/modules/ads/test_directory_navigation.py tests/modules/ads/test_directory.py tests/modules/reporting/test_detail.py -q --tb=short
```

25 passed / 35.40s，日志 `jb23-tests.log`。探针恢复并收紧真实 SDK 发送前授权后，仅重跑改动相关 native 文件：4 passed / 17.48s，`jb23-final.log`；新增 snapshot 断言定向 1 passed / 1.59s。原生最终文件也包含该 snapshot 断言。同名双 Series 浏览器补验 1 passed / 19.20s，`jb23-two-series-browser.log`。原有未改授权/导航证据复用。warning 为既有 uvicorn/websockets/Python 弃用提示。

从任务树 frontend：

```bash
PLAYWRIGHT_PORT=5200 bun run test --project=workspace tests/ads-detail-config.spec.ts tests/ads-detail.spec.ts -g '原生字段|点名称打开详情|层级路径可多层|只读成员详情|切 BC 关闭' --reporter=line
PLAYWRIGHT_PORT=5200 bun run test --project=workspace tests/ads-detail-config.spec.ts --reporter=line
bun run build
```

报表/新增映射 5 passed / 6.5s；完整配置映射 5 passed / 2.7s。TypeScript、正式/demo构建通过；日志 `jb23-frontend.log`、`jb23-config-final.log`、`jb23-build.log`。Ruff changed files 通过；Biome changed files 无 error（现有中文索引写法产生 info）；diff check 通过。构建产物和 openapi 中间文件不入提交，仅提交所需生成类型。Vite 共享依赖字体 allow-list 提示保留，未修改共享依赖或配置。

## 精确缺口与交接

本地实现候选已完成，root 独立 Standards/Spec review、全 M3 接受/统一收口、集成尚未执行。调度器/完整 Celery worker 宿主不是本票新增验证范围；同步数据链使用真实 SDK adapter 与原暂存/发布函数。真实 TikTok 字段返回、Series 名称、覆盖完整性及生产页面验收均 **Unknown**，没有真实平台或生产调用。不得把本地 transport 合成响应或 SDK 定义解释为生产支持；不得自行放行 M3。
