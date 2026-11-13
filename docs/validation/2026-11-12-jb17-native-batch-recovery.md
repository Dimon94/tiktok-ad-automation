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
