# M3 整体独立审查 R1 — Spec 轴

## 范围与基线核验

- 唯一任务树：`/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3`。
- Head：`d6e209c73f6af27637e7733185a5b92fb10bf1f4`；base 与 merge-base 均为 `55ee37146d7275b8c308161bce1986f9fbbb1670`。
- 正式 diff：`git diff 55ee37146d7275b8c308161bce1986f9fbbb1670...d6e209c73f6af27637e7733185a5b92fb10bf1f4`，19 文件，2229 additions / 27 deletions；非空，路径与 bundle 一致。
- 三提交：`2701fbfa feat(ads): navigate directory objects without report facts`；`4ddb3e00 docs(ads): record JB-22 validation and JB-23 contract`；`d6e209c7 feat(ads): expose native Series directory recognition`。
- 依据：[唯一正式 Spec](/tmp/tkada-native-series-20261112/m3-native-series-directory-spec.md)、两份批准票 `m3-tickets-draft/01-zero-spend-directory-navigation.md` / `02-native-series-directory-recognition.md`、review-evidence-r1.md、code-review skill。只审 Spec，未派生代理。
- 工作树前后均只有原有 untracked `.venv`、`node_modules` 链接。未改源码、提交、合并、推送、部署或操作 Linear；自己的检查与报告均在仓外。

## Findings

### [P1] 可发布的未知创意字段使整个广告详情返回 500

**位置：** [detail.py:175](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3/backend/app/modules/reporting/detail.py:175)、[detail.py:190](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3/backend/app/modules/reporting/detail.py:190)，后者进入 [detail.py:96](/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb21-native-series-m3/backend/app/modules/reporting/detail.py:96)。

**Spec 原句：**

- [Spec:18](/tmp/tkada-native-series-20261112/m3-native-series-directory-spec.md:18)：“字段缺失显示‘未提供’，关系缺失按证据显示‘未同步’或‘覆盖不完整’。”
- [Spec:162](/tmp/tkada-native-series-20261112/m3-native-series-directory-spec.md:162)：“字段缺失为 `None`，真实空集合为 `()`，原因进入 `coverage_reasons`。”
- [Spec:194](/tmp/tkada-native-series-20261112/m3-native-series-directory-spec.md:194)：“当前报表来源的详情入口保持原样”。

**触发与可达反例：** 已有原生广告组 g1、广告 a1 和已发布 creative 后，目录观察收到下列任一部分响应：

1. Smart+ 广告 a1 的 `creative_list: null`。现有 `material_usages` 对非 list 返回未知覆盖，主目录仍能发布广告原始配置；新投影在 175 行直接遍历 None。
2. 独立 creative cr1 的顶层身份/素材字段未返回，同时保留 `creative_info: null`。creative 读取使用非 Smart+ 内嵌解析，能发布该配置并保留既有关系；新投影优先选中子对象配置，然后 `raw.get("creative_info", raw)` 返回 None，`_identity` 对 None 做成员检查。

**可观察影响：** 两者均使 `GET /api/tenants/{tenant}/ads/ad/a1?bc_id=...&advertiser_id=account-a` 返回 500，而非保留已观察 Series、父组、创意与未知原因。目录详情和报表详情调用同一 HTTP owner，因此该新增投影也破坏既有报表设置详情。属于有效部分观察下必现的公开详情合同破坏，不是仅缺少真实平台验收证据。

**动态证据与基线：** 仓外检查使用现有隔离 runner、真实官方 Python SDK（仅 urllib3 transport 替身）、真实 `stage_directory_page` / `publish_directory`、PostgreSQL、FastAPI HTTP。两个载荷均通过 SDK 和发布；HTTP 均为 500。将 `git show 55ee3714:backend/app/modules/reporting/detail.py` 原文加载为仓外独立模块，在相同已发布数据上调用基线 `get_ad_detail` 均成功；当前实现分别抛 `TypeError: 'NoneType' object is not iterable`、`TypeError: argument of type 'NoneType' is not a container or iterable`。基线复用未改变的授权/定位 owner，没有切换或改写任何任务树源码；因此确认是本次引入，按 P1。

**同类追查：** 已沿新增投影检查 group 配置、独立子配置、内嵌 `creative_list`、identity、素材关系及两入口；上述两个分支合并为同一个 finding。修复应让已接受的未知嵌套字段保留原配置和具体覆盖限制，并继续返回可用详情；不能用确定空清单掩盖未知，也不能修改同步合同来规避部分对象。

## 审查覆盖与证据边界

- 已追踪账户目录入口→根/子列表→目录详情；分页 scope 包含 tenant、BC、账户、kind、parent，HMAC 校验及每页授权复用现有 owner；没有报表行/假金额/假 snapshot 依赖，也不触发新同步。
- 已追踪真实父组、组/creative 独立身份、按 creative ID 连接、完整素材身份匹配名称、空与未知、父/creative/素材观察时间和版本、来源、原配置保留，以及两个详情入口共用投影。除上项外未确认其他 P0/P1；不将未确认猜测列为 finding。
- 已读源匹配的 JB-22/JB-23 验证文件及测试源码；bundle 的 25 passed、root 的 4 passed 与浏览器/build 结果作为提供的既有证据复用，未宣称是本 reviewer 重跑。
- 本 reviewer 实跑 `git diff --check` 通过。新增仓外行为检查最终为 **2 failed / 7.23s**，两次失败均为上项预期 200、实际 500，非夹具失败。检查只替换 SDK transport 并写隔离测试事务，不运行源码变异探针。
- [仓外复现文件](/tmp/tkada-native-series-20261112/m3-implementation/test_review_r1_spec.py)、[最终日志](/tmp/tkada-native-series-20261112/m3-implementation/review-r1-spec-check.log)、[基线详情原文副本](/tmp/tkada-native-series-20261112/m3-implementation/baseline_detail_r1.py)。命令（显式 cwd 为任务树 `backend`）：`python3 /tmp/tkada-native-series-20261112/m3-implementation/run-test.py python -m pytest /tmp/tkada-native-series-20261112/m3-implementation/test_review_r1_spec.py -q -s --tb=short -p no:cacheprovider`。
- Spec 缺口：本轮没有另列可确认的双读法缺口。
- 未验边界：没有新的全量测试/build/浏览器复跑；没有运行 Celery/Beat/远端 Worker；没有真实 TikTok 请求或生产验收。平台是否实际返回这里的合成部分响应、真实原生字段、Series 名称和完整覆盖仍 Unknown。上述 Unknown 不单独构成 finding 或 blocker；P1 来自现有读取/发布合同已接受的数据导致详情失败。

## 最终结论

**verdict: BLOCK；数量：P0=0，P1=1，P2=0，P3=0；未解决 P0/P1=1。**

**未验边界：真实平台字段/覆盖、Celery 宿主、生产页面及新一轮完整浏览器/全量回归未验；不以这些缺口自动 BLOCK。本轮 BLOCK 仅由已复现且基线不存在的 P1 决定。**
