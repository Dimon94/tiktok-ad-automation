# Native Series M1：R1 修复验收

修复起点 `2dce3bc7d19f132209a6d3c6a8ba1bbd87619af2`，分支 `feat/jb5-native-series-m1`，唯一写树为本树。实现者单写者，无下级代理。本文所在提交为修复 tip；R2 两轴独立审查由协调者执行，本文不作最终 Review PASS 判断。

## Finding 对照

Standards 原判 BLOCK（P1=2/P2=1），Spec 原判 BLOCK（P1=3/P2=1），严重度保留。Standards 报告未分配 ID，以下按其出现顺序编号。

| Finding | 修复 | 实际验收 |
| --- | --- | --- |
| Standards P1-1 / Spec P1-1 原生网页无提交入口 | 原生正文作为共用 PreviewWorkspace 内容分支；保留 usePreviewSubmission、PreviewSummaryBar、未知受理查询、已提交任务导航和退出围栏；移除“执行尚未开放” | `test_native_browser_submission_timeline` 真实浏览器点击、受理响应丢失恢复、重开预览返回任务；真实库唯一 Submission；同一后端随后 SDK 创建并全 MATCH，普通与内部聚合均 COMPLETED |
| Standards P1-2 / Spec P1-2 独立封面/重复位置围栏 | 既有 cover_execution 最终围栏增加原生分支，消费当前 native_checkpoint 的 scope/revision/route/映射正证据并逐位置比较实际 wire。MATERIAL 消费已核实原生位置，不再准备显式封面视频的默认图 | `test_native_selected_cover_execution[explicit,no_default,repeated]`：完整 prepare→freeze→submit→SDK，默认 D 与独立 C 不同、无默认 D、同视频不同封面位置，均保留冻结顺序/数量；`test_native_final_material_fence` 拒绝撤权、错route、变映射、wire换封面/丢位置、准入后撤权，无 AD 到传输层 |
| Spec P1-3 实际禁用字段丢失 | parse_page 保存实际 `drama_selling_setting.distribution_link`、call_to_action_list、interactive_add_on_list 等非空直接模式禁用字段至 BuildRecord.observed_direct_fields；明确观察为 MISMATCH，缺字段仍沿 INCOMPLETE；差异路径与事实一起持久化 | 独立 observed_group/ad oracle 三项起点首红；精确 READBACK 三个负向实际库保持 NEEDS_REVIEW（含内部聚合）；UNKNOWN 组分销/广告附加项不能唯一认领，原 body/digest 保持 |
| Standards P2-1 / Spec P2-1 TTL 无同 revision 刷新 | 过期正证据区别于资格否定，接原 scene_refresh_required；刷新仅允许依赖准备，发送仍要求完整 READY。SceneJob 接冻结 native_selection，能力刷新由原 owner 排除过期 COMPLETE；等待受既有 Scene TTL 上界约束 | Campaign 已建后 Scene/能力过期→真实 SceneJob/CapabilityJob 与 SDK GET 刷新→原计划子级成功。draft revision/content digest 不变、只有原9次创建；CTA 改变/身份撤权不续跑；等待超限 FAILED，父级不重建 |

## 完整 UI 验收方法及边界

入口命令（从本树 backend）：

```sh
python3 /tmp/tkada-native-series-20261112/implementation/run-test.py python -m pytest tests/modules/builds/test_native_execution.py -k browser_submission -q --tb=short
```

`prepared_native_workspace` 在真实隔离 PostgreSQL/Redis 中保存草稿、通过现有 SceneJob/素材 owner 准备，随后真实 generate_preview 冻结。`native_browser_http.py` 仅为 test runner 启动本机临时 FastAPI 宿主，挂生产 builds/submission 路由与真实数据库 session，认证依赖绑定隔离操作者；没有产品测试开关、mock 广告接口或假业务 service。宿主端口动态分配并在 finally 退出。

宿主通过项目 Playwright workspace runner 启动浏览器，打开真实冻结预览，展示全部原生正文并点击“创建并立即启用”。浏览器把 build-previews、submissions、submission-requests 请求转发至真实宿主，响应原样返回；首个 submit 在真实 API 返回202并落库之后只丢失浏览器响应，验证“查询原提交结果”沿同一 request ledger 恢复并进入真实任务详情，再重开预览点击“返回任务详情”。没有替换 Submission API 响应。数据库断言只有一条 Submission，并继续并发提交复用检查、真实 expansion/process_step、官方 SDK 创建/回读和数据库 COMPLETED。

浏览器壳的租户/BC导航使用现有 buildsBoundary fixture；生产路由的认证依赖为隔离用户。保存草稿、prepare、freeze 在同一测试的真实后端完成，未用浏览器逐字段录入这些前置表单。SDK urllib3 传输响应为本地官方字段 oracle。此验收证明本地网页提交到真实后端闭环，不证明真实 OAuth、真实平台接受、审核或经营结果；没有形式证明。

## 运行结果与回归边界

全部后端命令经指定 run-test.py、锁定 venv、专用真实 PG/Redis 与当前源码 PYTHONPATH；不输出 test-env.json，不降级现有库、不改 SDK pin/依赖/生产配置。日志在 `/tmp/tkada-native-series-20261112/implementation/`。

- `r1-readback-red.log`：独立 nested distribution/static CTA/addon 三项均首红 MATCH。
- `r1-covers.log`：默认路径及 explicit/no_default/repeated **4 passed / 63.07s**。
- `r1-timelines.log`：浏览器真实链已通过；初次新增负向发现明确禁用字段被当 missing 导致 UNKNOWN；已改为实际观察字段。Scene 两项因测试 detached ORM 行引用失败，修正为在 session 内保存 revision；能力刷新首次失败揭示原 owner 复用过期 COMPLETE，已修。此日志不是全绿证据。
- `r1-negative.log`：**6 passed / 1 failed**；UNKNOWN 两项、精确 READBACK 三项及 Scene 正向通过；能力项跑在 owner 修正前，后续已补跑。
- `r1-restored-timelines.log`：**10 passed / 92.87s**；浏览器全链、Scene/能力/改变CTA、发送围栏相关检查通过。
- `r1-races.log`：**3 passed / 24.99s**；新鲜身份撤权、等待耗尽、准入后撤权。
- `r1-ui-regression.log`：**12 passed / 3 failed / 1 skipped**。三项是两个历史背景色断言及原提交404切BC对话框断言；未修改布局/切BC产品规则。`r1-ui-base-probe.log` 临时恢复起点 PreviewWorkspace，三项同样失败，并且新增原生可提交断言也失败；恢复后逐字节相同。独立API显示替身用例与上述真实宿主 UI 链分开。
- `r1-build-final.log`：TypeScript + Vite production/demo build 通过。

最终探针、恢复后回归和静态检查结果见本文末尾补充。

## 交接

不写 Linear，不推送、不建 PR、不部署、不写 main、不做真实广告/授权/版权方操作；保留原 untracked node_modules 软链。已知历史 Mini 目录夹具、旧SQL/签名、UNKNOWN baseline 依六票 validation 记录，不把新失败自动归为旧基线。不重复无关176/173全套。真实 active 完整枚举、身份经营资格、跨账户/BC覆盖、真实平台接受仍 Unknown。R2 应审修复 diff 与受影响的共用能力/围栏/投影/刷新路径。

## 最终补充

- `r1-near-probes.log`：近似错误分别忽略 nested distribution、跳过实际封面比较、忽略刷新后CTA变化，**3 failed / 2 passed**，失败均定位目标公开结果；两个未变的 CTA/addon oracle 仍通过。随后三份源码逐字节恢复。
- `r1-old-guards-red.log`：临时恢复旧封面围栏和过期阻断语义，独立封面完整创建及 Campaign 后TTL刷新均首红，**2 failed / 20.53s**；finally恢复、逐字节比较一致。
- `r1-final-regression.log`：**185 passed / 129.78s**。包括全部原生字段消融、UNKNOWN扫描、Mini provider-number/空回读、execution_state/channel_recovery、账户 capability service、官方 Mini SDK场景，以及上述四个探针恢复后的完整执行检查。初次命令路径拼写错误未运行测试，已改用实际 integrations/tiktok/test_native_minis_scene.py 后执行本结果。
- 原生广告重试的既有 `retry_ad_covers` 只接续该冻结广告选择默认封面的视频；显式图片位置不会触发无关默认视频封面任务。未新增图片上传/封面或资格 owner。Mini 条件未变，原176项素材回归按既有证据复用。
- 修改 Python 的 Ruff check/format、前端四文件 Biome check、git diff --check 通过。正式/演示 build 通过。没有全量应用测试、生产验收或形式验证声明。
- `r1-final-ui-ttl.log`：**3 passed / 39.82s**；补强真实结果页标题可见断言，并核对Scene/能力TTL刷新前后已建父级 remote_id/request_body/request_body_digest/attempt/attempt_id 完全相同，digest/revision保持不变。仅两条uvicorn/websockets弃用警告，无验收失败。
