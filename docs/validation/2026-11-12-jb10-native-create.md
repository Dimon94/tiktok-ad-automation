# JB-10 原生 Series 直接 ENABLE 创建

起点 `312468f7ae2188b3f7fb6917612004fe7100d23f`，指定集成树 `feat/jb5-native-series-m1`，单写者完成。仅本机隔离 PostgreSQL/Redis 与官方 SDK urllib3 传输替身；没有真实 TikTok、授权或版权方接口读写，没有更新 Linear、推送、PR 或部署。SDK 沿用固定 pin `f809c396520df2d7b201a9ccc5378d822b728ed3`。

## 执行合同

现有 `submit_preview → expand_submission → ExecutionStep → process_step → gateway.builds.create` 接线原生 `FrozenUnit.native_request`。新增 `native_execution.py` 是该执行器的本地冻结准入/编译模块，没有新入口、HTTP Gateway、执行器或激活步骤。

- 沿用 SubmissionRequest advisory lock、draft row lock、唯一提交、稳定 UUID5 步骤与 write-once attempt。步骤仍以冻结单元、账户和 PlannedAd 的基础广告/creative_no 区分；同一预览多键并发只受理一次，不同预览复用键冲突。
- 受理及每次 SDK 物理发送前检查 FROZEN、完整摘要、draft revision、冻结 route/授权代数、账户币种/时区、当前原生检查点及整组素材。当前 VID/封面、地区映射、CTA 改变停止；不重抽文案或重排素材。
- 冻结或新可见的显式 ACTIVE Series×location 冲突阻断；本提交已创建的对象不与自身冲突。PARTIAL/STALE/UNKNOWN 覆盖不升级为全局禁投，也不被称作无冲突。实际 active 枚举映射仍 Unknown。
- CTA → Campaign → Adgroup → API Ad 沿已有依赖围栏；失败父级不发送子级。1基础广告×6文案产生6个独立 API Ad，同素材、同 CTA、一份 Campaign 预算。
- 新增 NativeAdGroupCreate、NativeAdCreate typed union 分支，历史 Mini DTO 和正文仍可解码。Campaign APP_PROMOTION/MINIS、动态日预算 CBO、ENABLE；Adgroup NATIVE_SERIES、顶层 Series 身份、native_series_id、地区/版位/排期、VALUE/AD_REVENUE_VALUE/VO_MIN_ROAS/ZERO_DAY/BID_TYPE_NO_BID、精确 ROAS；广告逐视频 SINGLE_VIDEO、封面、创意身份、一条冻结文案和 portfolio ID。
- 直接模式省略 landing_page_url_list、distribution_link、call_to_action_list、interactive_add_on_list、minis_id。Campaign app_promotion_type=MINIS 保留。沿用 create_arguments/exact_json_number 数值规则，SDK 字典正文实际发送验证预算1000.01。
- 原生明确无副作用拒绝在现有 record_rejected 保存证据后终止，Mini 原有有界拒绝重试不变。其他未证实无副作用的业务错误保持 UNKNOWN；不因 HTTP 200 或错误文案推断无对象。未知发送结果沿原账本停止重发，已有 ID 和迟到回执保全仍归原 owner。

## JB-11 消费合同

每个创建步骤保存 request_body、request_body_digest、attempt、实际 remote_id、实际返回状态（缺失仍为 None）与 StepEvidence。CTA 的真实 ID 写入各 API Ad 正文；Adgroup/Ad 的真实父级 ID 在冻结请求编译后保存。PlannedAd 的 base_ad_no/creative_no 与完整 native-series-v1 意图保留，不新增内容抽样。

目前原生聚合如果仅凭成功回执得出 COMPLETED，改投影为 NEEDS_REVIEW。JB-11 必须用三级与逐创意全 MATCH 替换这一明确门禁，不能直接删除后恢复回执即完成。没有在本票实现原生读回比较、UNKNOWN 原生唯一认领或通用取消入口；这些分别归 JB-11/JB-12。现有取消/超时/租约和不可变回执机制保留。

## 验证命令与证据

所有命令从指定树 backend 运行，使用私有配置 runner，不输出配置：

```sh
python3 /tmp/tkada-native-series-20261112/implementation/run-test.py python -m pytest tests/modules/builds/test_native_execution.py -q --tb=short
```

工作区 prepare 和冻结不 mock service；仅替换 SDK urllib3 传输及有界 worker 宿主检查。测试从真实草稿和 SceneJob 开始，穿过数据库锁、Redis admission、官方 SDK 字典序列化，核对 ENABLE wire、全部 ID、父子依赖、六条文本和逐视频身份/封面。

初次测试编写曾有导入和 fixture 引用错误，不作为 TDD 证据。有效首红和近似错误以末尾实际运行记录为准。未运行形式验证器，未声称真实平台接受或整个 M1 完成。没有 UI 变化，未运行无关前端构建。

### 实际结果

- 原生执行/准入12项 + 原生预览16项：**28 passed / 107.65s**，`jb10-final-native.log`。包括初次提交在真实PG双线程竞争、不同键复用同一提交、键绑定不同预览冲突、FROZEN/摘要数据库不可变约束、OBSOLETE、目的地/revision/授权代数改变、当前可见ACTIVE冲突、明确拒绝、父子依赖和UNKNOWN消息重放。
- 相关回归命令：`tests/modules/builds/test_native_execution.py tests/integrations/tiktok/test_build_adapters.py tests/modules/builds/test_carousel_compiler.py tests/modules/builds/test_execution.py tests/modules/builds/test_execution_state.py tests/modules/builds/test_submissions.py -q --tb=short`。该轮 **134 passed / 2 failed / 5 errors / 150.10s**，`jb10-regression.log`。两项失败为新增测试夹具（摘要不可变抛ProgrammingError而非IntegrityError，及AdObject缺ad_type），均已修正且上述28项最终专项全绿。扣除当轮10项原生通过，既有SDK/Mini/编译/提交/执行状态检查 **124 passed**。
- 5个 `test_execution.py` 错误共享同一旧 executable 夹具：在 `_generate_preview → require_preview_identity` 以 `identity_catalog_unavailable` 停止，尚未触达提交/执行。本票在起点 `312468f7` 用 `git archive` 取独立源码、新建隔离测试数据库，运行该文件 `-xq --tb=short`，首项动态复现同错，`jb10-baseline.log`；未改身份产品合同迎合旧夹具。其余四项同夹具未逐项起点重跑，不声称该文件通过。
- 起点源码加同一新检查回放：`test_native_six_creates[None]` **1 failed / 8.71s**，准确失败于 `native_execution_not_available`，`jb10-valid-red.log`。此为实现后起点回放，不冒称初次编写测试就得到有效首红；实施中“回执误判COMPLETED”的业务断言也曾实际先红后修复。
- 近似错误：临时将原生 Campaign 编译为 DISABLE，真跑同一 SDK 集成检查，**1 failed**，明确断言 `DISABLE != ENABLE`，`jb10-probe.log`。finally恢复文件，与仓库外备份字节比较一致；恢复后重跑见下。

所有日志、起点源码归档和探针备份位于 `/tmp/tkada-native-series-20261112/implementation/`，不进入提交。当前数据库 head 仍为 `native_preview_freeze`，本票无迁移，也没有降级当前库。前票已知4个Mini预览旧夹具/替身失败没有重复运行或修改。

恢复源码后补跑 `test_native_six_creates[None]` 与 `test_native_six_creates[revision_after_cta]`：**2 passed / 25.02s**，`jb10-restored.log`。正常案例进一步经现有 process_unit/finalize_submission 收尾，并真实停用操作者后再次内部收尾，数据库仍为NEEDS_REVIEW；后者在SDK的CTA响应边界改变draft revision，保留CTA实际ID，Campaign及所有子级均未发送。该补跑覆盖最后新增dispatch收尾围栏；上述28项中的正常案例重复一次，其余专项未重复。

最终改动文件Ruff check/format、git diff --check及暂存diff检查通过。所有验证均为本地代码与隔离依赖验收；Mini旧夹具错误、真实平台接受、三级/逐创意MATCH及完整原生恢复/取消是明确剩余边界。
