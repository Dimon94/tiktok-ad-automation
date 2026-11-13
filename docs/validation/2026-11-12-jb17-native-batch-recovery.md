# JB17 候选：M2 UNKNOWN 名称定位修复与只读恢复

状态：**未完成，不能放行 JB18**。实现起点 `48adee03771e8647b6b7a5d34ff46c23141bf951`；代码及模型候选 `d4b63a34b4366a4abbd00269e02bc6c51386916b`。唯一树 `/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb13-native-series-m2`，分支 `feat/jb13-native-series-m2`。未切树/分支、派代理、改外层协调状态或Linear；未push/PR/deploy/调用真实平台。原未跟踪`.venv`、`node_modules`保留。没有新增迁移。

读取用户权威Spec/JB17票、JB16证据及形式skill；应用tdd/git-commit/create-formal-verification-skill。Nowledge定向查询未得到相关历史，不以不相关结果替代源码。项目无本地GLOSSARY，读取外层业务词汇；实际执行词汇依据Spec及原owner。

## 实际缺陷与修复

`native_preview.expand_plan` 的M2名称为 `Series-…-R<draft hex>:<plan UUID>`，原 `reconciliation_scan._candidate` 只接受M1的 `-R<draft hex>`（以及G/Ad后缀）。真实SDK发送丢响应后，即使返回唯一完整原对象，仍被名称guard排除。真实HTTP用例在unique分支复现UNKNOWN≠SUCCEEDED；修复原扫描器，允许完整冻结plan UUID后缀，仍精确比较完整名称、账户、正文和全字段，不截掉计划键，也不改变冻结名称或新建执行器。

## 运行事实

复用真实两账户workspace准备/冻结/Submission执行用例，SDK transport唯一业务外部double。第二账户Campaign物理POST后Timeout，第一账户经三级真实readback完成。没有直接写UNKNOWN/READY。创建基线7次（含原CTA请求），初始整批NEEDS_REVIEW、一计划COMPLETED。

新增 `test_native_batch_recovery.recover_batch` 经真实HTTP `/submissions/{id}/reconcile` → `process_recovery` → `deliver_step(reconcile=True)` → SDK GET →原step/Unit/evidence→原聚合。先由既有finish_delivery完成原消息并做首次空扫描；随后在同一Submission依次空、多、缺budget、唯一全字段候选。前三者UNKNOWN；唯一认领原Campaign后RUNNING（T15），后续Group/Ad未在扫描中创建。每轮恢复请求同key重放、recovery任务重投、旧revision及同revision读消息重投；请求体/digest/Unit、成功兄弟ID/digest保持。全程创建列表与7次基线相同，transport拒绝一切非GET。调度clock仅在已有lease非空时推进，不改业务状态。

这是真实PG/Redis及原任务函数接线；并未声称启动完整Celery prefork。认证为隔离fixture身份，bounded-worker宿主元数据沿用现有测试。此次**没有浏览器恢复**，JB16页面成功证据不替代本票该AC。

命令（在唯一树backend）：

```sh
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest tests/modules/builds/test_native_batch_execution.py -k recovery -q --tb=short
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest tests/modules/builds/test_native_recovery_scan.py tests/modules/builds/test_native_readback.py -q --tb=short
```

最终HTTP恢复1 passed/17.86s（34 deselected）；M1扫描及字段回读101 passed/0.12s。后者不是M1整条运行时恢复证明。Ruff check五文件通过；format已执行；diff check通过。无前端改动，未重复构建。

过程失败：初次测试文件写入路径错误导致ModuleNotFoundError，不计业务RED；原dispatch未收尾导致HTTP409，补真实finish_delivery后解除；测试时钟给无lease行设expires_at导致ck_step_lease，改为只推进已有lease。随后unique真实业务RED定位名称格式缺口。一次编辑脚本SyntaxError使修复未应用，早期源码probe恢复绿失败，该轮不计有效闭环；最终探针使用专属断言标记，完整红→恢复绿成功。保留失败事实，不称全程一次绿。

## 形式与真实源码破坏

同一Submission模型增加Recover，候选0/1/2、完整性和scope，保持sends/body/原对象身份；Publish消费恢复后的同一事实。工具版本/SHA沿既有skill，不重新下载。实际一计划3层：6410 generated/1034 distinct；两计划3层：1954200/175454；Authority3585/1456。破坏模型：Recovery86/45、WriteOnce631/187、Completion14/14，均指定不变量反例。

真实 `advance` 计数guard变异为允许多候选，实际HTTP用例在 `R10 candidate many` 处FAILED（错误SUCCEEDED）；runner仓外备份/finally逐字节恢复/cmp后17.86s绿。日志：`/tmp/tkada-native-series-20261112/m2-implementation/jb17-source-final/`；模型/反例：`…/jb17-formal-final/`。完整一命令在项目formal skill JB17节。未重复旧11variant+10probe。R9/R13随恢复模型复验，R12复验原Authority；其他owner语义不变复用JB16证据。

原子边界：claim锁定原source/step并保存扫描身份；SDK GET在事务外；finish重新检查claim和当前读路由，在事务中追加证据、认领原对象。模型Recover抽象结果提交，未证明扫描期间任意scope/lease交错；full代表真实比较器结果，不证明其任意字段或重复creative算法。没有全系统refinement、liveness或真实平台证明。

## 必须继续的核心AC

- 同一后端真实浏览器“待核查→点击恢复→结果页”，并将前置真实页面批准与丢响应连接起来。
- T16全部MATCH、T17部分MATCH、T18明确negative evidence/原已知拒绝解析的真实恢复链；空列表不能消除UNKNOWN。
- 恢复扫描的AdGroup/Ad/creative缺字段、错误配对、重复creative、返回重排、distribution/staticCTA/addon及关键tenant/BC/connection/actor-generation变化的实际纵切。M1纯比较检查和JB16写围栏不能替代这些接线AC。
- 查询指纹对完整plan/Series/幂等键/冻结路由/原批准代数的系统性映射审查；本轮仅验证原Campaign正文/digest/Unit及成功兄弟不变，不能宣称全scope已验。
- 所有恢复终态的同/不同提交key及事件回放；本轮复用恢复前提交唯一性与读消息重投，不冒充恢复后全终态证据。
- M1完整UNKNOWN运行时扫描/事件回写回归，以及上述增量模型negative-evidence映射。

JB18消费合同仍是原Submission/Unit/Step唯一owner；扫描零写、原批准stamp不可刷新、UNKNOWN不可被取消掩盖、原对象与风险持续可见。必须先由root补齐/审查JB17关键AC，再放行18；本worker不标Done。

## Fresh-context 运行补验（2026-11-13，仍待 root 决定）

起点 `d982b5030346c34902b2deee889adadc10c47b10`。唯一树/分支不变，原 untracked `.venv`、`node_modules` 保留。读取 canonical Spec、JB17完整AC、JB16最新六性质边界、coordinator/state、外层domain/GLOSSARY、tdd/git-commit与项目 CONTRIBUTING。Nowledge定向查询无相关结果。没有改产品 owner、迁移、冻结快照、授权 stamp、formal模型或sync commit；没有更改外层协调状态/Linear、派代理、push/PR/deploy或真实平台调用。

### 新增运行证据与 owner

- `test_native_batch_execution` 新增 recovery-complete / recovery-partial / recovery-browser；沿既有两账户准备、冻结、提交与 SDK physical POST。第二账户第二条（最后一条）Ad 的真实传输保存请求后抛 TimeoutError，原 owner 写 UNKNOWN；没有直接SQL写UNKNOWN/MATCH。
- T16：原两份 INCLUDED 全部三级/逐创意 MATCH 后 COMPLETED，创建基线10次（含CTA）。T17：第一账户广告组原 SDK 明确拒绝，保留Campaign ID；第二账户最后Ad UNKNOWN唯一恢复后完整MATCH，整批PARTIAL，创建基线8次。认领成功后仍驱动原独立 READBACK；只认领不跑这一步只能RUNNING，不能冒充完成。
- 新 `recover_terminal` 通过 HTTP reconcile→process_recovery→deliver_step(reconcile=True)→原SDK GET→原 READBACK 读取。传输断言一切调用均为 GET。完整原 body/digest/Unit、原 Submission.authorization_stamp、兄弟步骤 ID/状态/digest 保持；RECONCILED事件保存。恢复任务与读消息重投无额外读取/创建；恢复后同key/不同key回原Submission和当前状态。无需重复JB16双session全矩阵。
- Ad传输代表例：缺creative、错误video、错误cover、错误identity、额外重复creative、静态CTA `call_to_action_list`、错误text、addon、错误account均不能认领，原状态保持UNKNOWN/NEEDS_REVIEW；最终完整返回允许creative数组倒序，写回原对象。独立封面与完整字段 oracle 仍复用M1已有101项，不重写oracle。
- M1完整运行时间线定向复验 unknown_ad、unknown_ad_addon、unknown_group_distribution、unknown_revoke：原请求扫描/唯一MATCH/原证据和回读保持；实际AdGroup distribution与Ad addon经过SDK GET传输，扫描中连接撤权保持UNKNOWN。这不是只跑纯比较器。

浏览器：复用 `native_browser_http.submit_in_browser` 和原 Playwright workspace宿主。原工作区页面准备/冻结、确认两份INCLUDED和一份EXCLUDED生成原Submission；SDK丢响应后重新打开该原Submission结果页，真实按钮“核查待核实项”调用真实reconcile路由；测试驱动端只执行原恢复任务和只读READBACK；刷新原结果页看到两项COMPLETED及消耗风险。没有mock Submission/恢复响应、matcher或service。前后使用两次浏览器会话、同一数据库/Submission（宿主分别绑定临时端口），不是持续同一个tab。认证和账户导航壳沿M1合成；业务路由、PG/Redis真实，Celery使用原任务函数和bounded-worker测试元数据，不声称完整prefork或真实平台。截图在仓外 `…/m2-implementation/jb17-recovered.png`。只改浏览器测试，产品前端未改，复用既有正式/demo构建。

### 指纹及授权映射（源码审查，不冒充全scope穷举）

| 事实 | canonical owner |
|---|---|
| 原Submission/Step/Unit/tenant/actor | reconciliation.py `_locked` L142：按tenant读取原step，核对source与readback同Submission/Unit，核对原actor和Unit所属preview |
| 完整冻结名称/plan_key/Series | native_preview.py L235/L348/L515：完整planUUID进入冻结名称与Unit；reconciliation_scan.py `_candidate` L57接受该格式且精确全名称/account，最终 compare_record 全正文比较；Series属于原native_request及组正文 |
| 原幂等键/正文/digest | reconciliation.py `original_create_attempt` L211 / `source_intent` L237取原尝试，核对原request_id；`_claim` L422重算已保存body SHA256并核对账户；不产生第二快照 |
| 冻结route与当前read授权 | routes.py `verify_unit_route` L126消费原BuildRouteContext；recovery.py READ_ACCOUNT L112核对原connection、binding revision、authorization revision和read权限；reconciliation `_authorize` L199在claim与finish复核 |
| 原批准stamp与新写 | submissions.py INSERT保存authorization_stamp；dispatch.py L118、execution.py L143、native_execution.lock_send_authority分别围住入队/物理发送；只读恢复不刷新stamp。本次运行逐字典断言stamp不变，旧stamp/actor regrant写围栏复用JB16 |
| 唯一认领/原对象与聚合 | reconciliation_scan.advance / reconciliation._finish；execution_state.ensure_native_readback；native_completion.unit_native_matched与batch_results。扫描不执行创建；后代新写仍交原执行器双围栏 |

实际wrong-account传输、M1扫描中连接撤权与旧任务重投补接线；没有新增tenant/BC/每种代数笛卡尔矩阵。JB16冻结不可变、范围锁和旧授权围栏证据保持适用。未证明任意lease/撤权交错或模型composition。

### T18 精确合同缺口（未验，不放行宣称）

`reconciliation._recover_known_rejection` L366仅消费**原**RESULT_UNKNOWN中保存的明确业务码；排除41021，接受 `build_results.NO_EFFECT_REJECTIONS` 中广告组40002、Ad40002/51002。该owner用于历史误分类回执。当前SDK `sdk_creation_envelope` 已把这些明确拒绝分类为NO_EFFECT，新的真实请求会直接走明确失败/有界重试，而不会先产生带该码的UNKNOWN。丢响应的TimeoutError则没有这个明确拒绝回执，空GET也不产生该事实。

因此当前授权的“当前SDK发送丢响应→只读明确失败→无计划完成”时间线没有可用的官方负证据输入。不能从空列表/缺字段编造confirmedabsence，也不能SQL种植历史UNKNOWN或注入私有service回执。本轮没有为造绿修改分类器、导入历史数据或发明平台负证据。**T18及其FAILED恢复后重放仍未满足**；需要root明确采用有来源的历史原拒绝回执导入合同/验收边界，或提供官方可验证的负证据查询合同。T13普通明确失败不是T18。

形式：没有negative-evidence/聚合owner改动，复用已跑R10模型、R9/R12/R13和既有多候选probe；sync commit仍为d4b63a34，不重新跑11旧变体/10旧探针，不把缺T18说成形式已闭合。

### 运行命令、失败与边界

均从指定树backend，用既有私有runner及Python3.14依赖，不读/打印环境文件、不重建venv、不触碰共享服务：

```sh
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest tests/modules/builds/test_native_batch_execution.py -k 'recovery-complete or recovery-partial or recovery-browser' -q --tb=short
python3 /tmp/tkada-native-series-20261112/m2-implementation/run-test.py python -m pytest tests/modules/builds/test_native_batch_execution.py tests/modules/builds/test_native_execution.py -k 'recovery-complete or recovery-partial or unknown_ad or unknown_group_distribution or unknown_revoke' -q --tb=short
python3 /tmp/tkada-native-series-20261112/m2-implementation/jb17-terminal-probes.py
```

首次终态三项3 passed/55.12s（含浏览器）；初版传输代表例与M1定向6 passed/61.55s。过程修复：最初误用actor_authorization_stamp属性名，改为实际authorization_stamp；独立READBACK漏驱动导致RUNNING，补原只读任务，未改产品状态；初次probe被静态CTA fixture路径错误提前打断，不算有效probe；既有合同规定根call_to_action_list，修正后重新做源码探针。最终增量结果见下文。

JB18消费合同：继续复用原Submission/Unit/Step/READBACK；零候选UNKNOWN不消失；恢复只补原事实、不能刷新原批准stamp、新建或再次ENABLE；已创建父级/广告ID与风险不丢。R11尚未实施。JB17仍InProgress，root审查T18合同缺口及本轮证据后决定17/18，不由worker标Done。

最终源码probe：`missing-readback`删除reconciliation中的ensure_native_readback调用，17.21s在R13完成断言真红；`wrong-match`强制conclusion比较为MATCH，18.60s在错误video候选（index1）R10断言真红。每轮仓外备份、finally恢复、cmp逐字节一致。随后含全部9个Ad反例的T16/T17 **2 passed/35.62s**；最终浏览器含stamp/兄弟事实/重放断言 **1 passed/24.53s**。日志分别为 `missing-readback-red.log`、`wrong-match-red.log`、`jb17-terminal-green.log`、`jb17-browser-final.log`，均位于上述m2-implementation目录。三Python文件Ruff check/format通过；git diff --check通过；产品backend/app diff为空。未再次执行无变化M1 101项、形式模型或前端产品构建。提交前仅本轮4个测试文件和2个文档改变，原两untracked软链不动。

## T18 历史条件路径补验（2026-11-13，fresh context）

本节按 root 本轮明确批准的历史输入特例，取代上文“T18 未验”的结论；不改变当前
Timeout/空 GET 必须保持 UNKNOWN 的边界。起点 `9fed527926c795fda1d42baa529d2aa09e056be3`。
无产品代码、前端、数据库 schema 修改。

输入来源为可复现的**合成历史 fixture**，不是生产旧数据，也不是当前 SDK 自然产生的 UNKNOWN。
原 HTTP 准备/冻结/提交和正常 worker 生成两 INCLUDED 的真实 Unit/Step/attempt，两个 Campaign
成功并正常 readback。两次 ADGROUP 原 SDK POST 各获 transport double 明确 40002 回执；当前
分类器照常按拒绝处理。正常 finish_delivery 释放投递后，fixture 关联原 REMOTE_REJECTED
的 evidence ID、request_id、attempt、原 body/digest，追加标注 synthetic-historical-v1 的
RESULT_UNKNOWN，预置源 UNKNOWN；不改原证据、body、父级 ID、计划归属或批准 stamp。
数据库原约束保持启用，没有私有 matcher/service mock，没有直接调用恢复私有函数。

正式 HTTP reconcile 202 → process_recovery → deliver_step(reconcile=True) → 原历史恢复
owner，将两 UNKNOWN 解析 FAILED，并持久 REMOTE_REJECTED_RECOVERED，回执 request_id 与
原拒绝完全一致。Submission HTTP GET 显示 FAILED（UI 既有“无计划完成”状态），两计划均无完整
MATCH，两个 Campaign ID 和拒绝原因仍可见，不能解释为未创建任何远程对象或无消耗风险。
本轮验证 API/持久事实，未重跑无变化浏览器；沿用前轮 24.53s 浏览器证据，不冒称本轮看过 T18 页面。
恢复阶段 transport 一旦调用就断言失败，因此新增平台 GET/POST 均为零。重复 recovery 消息、
读消息与事件 GET 保持相同回执/事件数；同 preview 的相同/不同 key 经 HTTP 重放均为原 sid/FAILED。
原 body/digest、attempt/attempt_id、Unit、父级 ID、approval stamp 全部不变。

缺负证据对照复用同文件 recover_batch 的真实 Timeout→empty/many/missing 保持 UNKNOWN 实证，
不把此历史特例替代当前 Timeout 纵切。允许码仍由 NO_EFFECT_REJECTIONS 定义并排除 41021；
没有新增 importer、自动重试、平台否定查询或广告写权限。

形式实际结果及命令见 formal skill 新 T18 节：单/双计划正常安全性质通过（1,628 / 437,408
distinct states），保留 R9/R10/R13，新增 NegativeProof / HistoricalPreservation；四个指定故障
变体均产生对应反例。双计划可达 witness 40,642 distinct states 命中 NoHistoricalTerminal，
显示该条件路径非空。R12 Authority 与其他未改模型/旧探针复用；没有全系统 composition 证明。

运行证据位于 `/tmp/tkada-native-series-20261112/m2-implementation/`：
`t18-second.log` 初次通过 1 passed/11.19s；`t18-no-closure-red.log`、
`t18-lost-request-red.log` 是实际源码故障断言失败，外备份 `t18-reconciliation.backup.py`，
`t18-probes.py` 用 finally 恢复并执行 cmp 后再 green。`t18-formal/` 保存全部模型输出。
初次 seed 未 ACK 导致 recovery_no_candidates 409，修复测试 seed 收尾；首次 no-closure 探针
先触发事件缺失断言，调整断言顺序后命中精确 T18 终态断言；均非产品缺陷，未调整业务期望。

本票核心 T18 与 FAILED 恢复后重放缺口已补齐，可交 root 验收；本轮未发现剩余真实 core 缺陷。
JB17 不标 Done，是否放行 JB18 仍归 root。原 T15/T16/T17、9 creative 变体、M1 timeline 6 pass、
R10 multipleclaimprobe 和浏览器证据复用，无前端构建、push、PR、deploy、Linear 修改或真实平台调用。

最终串行恢复后 `t18-final-green.log`：**1 passed / 12.41s**；no-closure 命中
`T18 historical rejection must terminate`，lost-request 命中原 receipt request_id 关联断言。
Ruff check/format 与 git diff --check 通过；backend/app diff 为空。提交前原 `.venv`、
`node_modules` 两项 untracked 保留，仅上述测试、formal 文件和两个文档纳入提交。
