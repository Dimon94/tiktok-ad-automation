# JB-9 原生 Series 预览与冻结

起点 `e4ddf3c25a7e2edb13440c147f96dce5dde21d79`；仅用户指定集成树 `projects/tiktok-ad-automation.wt/feat-jb5-native-series-m1`，分支 `feat/jb5-native-series-m1`。本记录所在提交为交付源码 tip。复用集成树的用户授权覆盖 implement-spec 逐票新树/merger步骤；使用 implement-spec、tdd、git-commit，未派下级代理、更新 Linear、切换checkout、推送、PR、部署或读取真实平台业务目录。

## 可复现用户流

1. 既有新建工作区选择 NATIVE_SERIES_DIRECT_VIDEO、一个账户、精确Series、两层身份、非空地区及视频来源，选择 CBO / Target ROAS / ENABLE 策略。
2. 沿 prepare/continue 驱动现有 SceneJob、素材 distribution/covers owner。SceneJob 现在增加既有 CTA 资源读取；旧 COMPLETE 且缺 CTA 的任务可补读该资源，不重建身份目录owner。
3. 工作区 READY 后点击生成预览。后端在草稿锁内重新取真实草稿 native_checkpoint，要求当前 scope/revision/route 的目的地与同N同序素材全部就绪。缺项、过期、无CTA、超M1结构或冲突返回明确错误，不能产生冻结行。
4. 预览页显示Series、账户、双身份、地区、CBO金额、ROAS、ENABLE、每条实际文案、完整动态CTA及全部VID/封面分配。证据展开区保存完整路由/授权代数、SceneJob/素材证据、策略与约束revision、seed及digest。目录观察保留范围限制，可前往既有广告目录请求更新；不开启 ADS_SYNC_ENABLED。
5. 修改草稿任一执行输入沿既有revision触发器将旧预览变为OBSOLETE；历史正文和digest不变。当前原生提交入口明确报 `native_execution_not_available`，不创建任何广告，执行归JB-10。

## 实现与持久合同

- 入口仍为 `previews.generate_preview/get_preview_summary/load_frozen_unit`；原生适配在 `native_preview.py`，没有第二套组合算法。`plan_structure` 与 `get_copies` 原样复用固定/按素材、SHARED/SEQUENTIAL_AVERAGE、基础广告×creative_count和seed去空/去重抽样。素材位置使用独立创意单元ID，保留明确重复选择。
- 同一个 BuildPreview / PreviewContract V2 / BuildUnit / PlannedGroup / PlannedAd / PreviewCreativeUnit 系列保存最终计划，完整VID/封面证据另随native request冻结。创意摘要使用既有 `creative_content_digest`，逐广告分页可经原详情owner读取。不伪造ProviderDrama、PromotionLink或Mini链接。
- `frozen_requests.FrozenRequestRevision` 的 discriminator 为 `kind=NATIVE_SERIES_DIRECT_VIDEO`，revision 为 `native-series-v1`。正文保存在 `BuildPreview.config.native_request`，并通过 FrozenUnit.native_request 和 PreviewSummary.native_request读取。Mini V1/V2正文、decoder及执行编译保留原样，新增字段默认None。该revision是完整执行意图，**不是可以直接发送的官方wire正文**。
- digest使用完整payload的规范JSON：Series/双身份/地区、实际分配、CTA、精确Decimal字符串、route/授权代数、证据和策略revision、占用观察。没有预览随机ID/批次号混入内容摘要。相同输入只产生一个冻结预览；草稿锁序保留既有并发合同。
- additive迁移 `native_preview_freeze`，父为 `native_material_readiness`。只扩展既有 `check_copy_source_references`：原生允许无版权方/链接引用，并拒绝捏造这两项；Mini及复制分支条件保留。既有预览正文不可变和revision失效触发器不改。隔离库实际upgrade head；没有降级当前库。downgrade显式拒绝未经审查的合同回退；生产迁移尚待整个M1审查。

## 占用观察边界

复用 `ads.directory.list_objects` 的当前账户授权与 AdObject，按精确 `native_series_id` 及官方地区目录映射得到的 `location_id` 实际交集读取 Campaign/Adgroup；不按剧名或身份匹配，不推断国家/子地区/ZIP层级交叠。保留remote_id、原始operation/delivery状态、逐对象observed_at/published_version。

只承认显式 `delivery_status=ACTIVE/INACTIVE` 标记及明确已删除状态；ENABLE、DISABLE/暂停、审核中和其它枚举均不推导完整active语义。显式ACTIVE交集阻断冻结，错误定位对象及地区。**当前官方材料未证明实际返回哪组状态等价于占用规则的active；隔离测试的显式ACTIVE标记仅验证门禁，不是实际平台状态映射验收。**

返回 visible_conflicts / coverage / observed_at / published_version / scope_limits，并附当前同route/操作者目录发布行的原始coverage/query/kind。只观察最新一项发布查询不能证明Campaign及Adgroup全范围完整，所以汇总保守为PARTIAL；超过既有每日活动目录校准间隔为STALE，无发布证据为UNKNOWN。空交集不承诺跨账户/BC或观察时间后的无冲突；不新增全域禁投门禁。目录传输归既有owner，本票占用测试以隔离数据库目录事实作为fixture，没有真实平台目录验收。

## 本地验证

Python命令均从本树backend运行，前缀：

```sh
python3 /tmp/tkada-native-series-20261112/implementation/run-test.py python -m pytest
```

锁定venv、专用真实PG/Redis；平台素材、身份、Series、地区、CTA读取替身在官方SDK传输处。`prepared_native_workspace`真实运行工作区prepare/continue及SceneJob，不mock整个business service。目录占用事实为隔离fixture。

| 命令（上述前缀之后） | 结果/日志 |
| --- | --- |
| `tests/modules/builds/test_native_preview.py -q --tb=short` | **16 passed / 92.63s**，`jb9-final-preview.log`；包含完整工作区、1×6、2×3、30×1、顺序分配、OBSOLETE、占用正负向、缺VID/CTA、多组拒绝、双事务并发、digest敏感性及150种结构/seed属性组合 |
| `tests/modules/builds/test_native_preview.py tests/modules/builds/scene_jobs/test_native_series.py -k 'not material_group' -q --tb=short` | 31 passed / 11 deselected / 109.34s，`jb9-final-restored.log`；后续仅冻结详情摘要修复及新增覆盖测试由最终专项覆盖 |
| `tests/modules/strategies/test_structure.py`（与原生专项合跑） | 18项通过；复用 `jb9-suite.log` / `jb9-final.log` 的策略结果，没有修改结构算法 |
| `tests/modules/builds/test_previews.py tests/modules/builds/test_structure_preview.py tests/modules/builds/test_frozen_routes.py tests/modules/builds/test_preview_workspace_api.py -k 'not batch_cap_blocks_before_any_account_expansion' -q --tb=short` | 84 passed / 4 failed / 1 deselected，49.65s，`jb9-mini.log`；4项起点动态基线见下文，不能报告全套green |
| frontend: `PLAYWRIGHT_PORT=5191 bun run test --project=workspace tests/build-preview.spec.ts tests/build-preparation.spec.ts -g '原生冻结预览\|原生准备就绪' --reporter=line` | 2 passed / 5.4s，`jb9-ui.log`；API显示替身，验证无Mini门禁及六条完整明细，与真库业务链证据分开 |
| frontend: `bun run generate-client`; `bun run build` | 客户端仅增加两个native_request字段；TypeScript、Vite production/demo通过，`jb9-client.log` / `jb9-build-final.log` |

测试开发记录：首个完整工作区预览检查对起点失败于 `minis_links_unavailable`，`jb9-red.log`；最小实现后green。后加公开逐广告详情检查实际抓到新快照摘要未复用canonical算法的问题，已改为原owner。并非所有后加分支都声称实现前首红。占用门禁近似错误探针实际跳过ACTIVE阻断，检查因DID NOT RAISE失败（`jb9-probe.log`）；恢复文件并cmp字节一致，再跑同检查1 passed / 4.09s（`jb9-probe-restored.log`）。未运行形式验证器。

Mini新增发现的4项失败动态基线：从本树根运行锁定venv的 `jb9-baseline-check.py`（在 `/tmp/tkada-native-series-20261112/implementation/`）。脚本git archive起点e4ddf3c到独立临时源码、新建独立随机测试库，仅进程内加载私有环境，运行相同4项后删除独立库。结果同为4 failed / 5.08s，`jb9-baseline.log`：三项历史SQL夹具违反已有ck_build_draft_structure_object，另一项旧 `_digest_units` 替身仍为两参数、现有owner已是三参数。本票未修改这些旧测试或产品合同。原已知batch-cap失败依用户指示未重复归因或修改。

JB-8未变的素材distribution/covers 176项及既有上传证据可复用；不重复全跑。所有日志在上述implementation临时目录，不含真实凭据。没有用preview工具自验；Playwright由项目测试runner驱动。

## JB-10交接及Unknown

JB-10从同一FrozenUnit.native_request读取 `native-series-v1`；使用已冻结campaign/adgroup、逐base_ad_no/creative_no的ads、materials、text、creative_identity、完整CTA、route、checkpoint及digest。逐条广告仍对应一个基础广告的一份文案版本，预算不乘广告数量。发送前核验当前授权、账户、原生场景及当前draft revision，不能重新抽文案/分配素材。必须扩展既有typed意图/decoder/官方wire编译和执行链，才能移除本票原生执行门禁。

真实平台广告接受性、实际active状态枚举映射、跨账户/BC占用、签约/经营资格、真实目录覆盖与最终准入仍Unknown。没有真实平台创建、回读或恢复验收；没有形式证明。JB-10/11/12、最终独立审查、集成及整个M1验收未完成。

收尾：变更Python Ruff check/format与git diff --check通过。提交前按明确路径暂存并检查完整暂存差异，node_modules软链不暂存。新增公开详情与覆盖测试的首次3项失败分别定位到canonical创意摘要与等价时区表示；源码改用已有摘要owner，时间断言比较实际时刻，最终16项全部通过。

最后复用既有目录校准常量ACTIVE_INTERVAL代替同值的一天字面量：STALE公开检查1 passed / 15.32s，`jb9-age-owner.log`；未重复全套。
