# M5a 原生启停：本地接受与集成依据

## 范围与结果

用户批准的是 JB-59 → JB-60 两票验收及验收暴露的必要修复，直接归属 Epic JB-19；不是新增执行器或强制预览政策。保留工作区直接启停、异步准备、原 SMART_PLUS SDK、定向目录回读、UNKNOWN 只读核查及显式反向恢复。三层对象、两种方向、精确 ID、当前授权与冻结原值沿用既有 owner。旧 Kimi `management_preview_required` 草案没有获批，不作为本次要求。

起点 `9d51cc9b37faf1b6a830fa025cbd377ace716048`。JB59 源码 `20709eb5`、阶段接受 `2e3a52b6`；JB60 源码 `c6694ca8`、阶段接受及整片固定审查 HEAD `d1bcbe60e46380ad5abf79b1792975120af4b62a`。两个实施上下文及两个独立审查上下文均为用户指定 Codex gpt-6.1-sol/high。使用已批准的单集成树、串行唯一写者，没有子树并发写入。

实际产品修复：启停原值以文本回传；平台受理与新鲜目录观察分开；恢复方向按冻结原值/发送目标判断，避免硬编码 DISABLE。Root 另修复 Standards S1（JB-61）：BC 无启用目录同步计划时，保留 ACCEPTED/请求 ID/原值/目标，把观察及聚合任务置为既有 NEEDS_REVIEW，原因 `targeted_refresh_unavailable`；不再产生无效刷新唤醒，不伪造观察或重发写入。已有未来消息可以退出；正常同步计划仍沿原链路取得发送后的同连接事实。任务页将原因显示为“目录同步未启用，需检查当前状态”。无迁移、依赖或部署开关变化。

无计划时的检查状态不会自动变成观察成功，也没有新增自动恢复框架。运营需检查连接与目录同步配置、另行核对当前事实；原任务回执可读，原值保留供既有显式恢复流程使用。

## 验收与独立审查

C01–C10 完整矩阵在 [JB59 记录](2026-11-15-jb59-native-status-readback.md) 和 [JB60 记录](2026-11-15-jb60-native-status-recovery.md)。两个阶段已由 root 验收；不把不同轮次同一用例重复累加为覆盖数量。

- JB59：root 亲跑 12 个 HTTP/原 Worker/PG17/Redis 场景；六条真实页面、119 个既有回归及六类源码拒错证据按源码匹配继承。
- JB60：root 亲跑 33 个混合结果/20 个恢复组合/12 个撤权场景（同一集合）；五条最终真实页面、其余 UNKNOWN 轮次和六类源码拒错证据继承。有限验证 skill 为真实 owner 输入枚举，不是 TLC 或数学证明。
- fresh [Standards R1](native-m5a-review/review-r1-standards.md)：PASS，P0/P1=0、P2=1。fresh [Spec R1](native-m5a-review/review-r1-spec.md)：PASS，各级为0。两轴报告明确亲跑静态/纯函数与继承动态证据的区别。Root 处理小项 P2；没有把修复后的源码冒称为两位审查者已重新审查。
- JB61：真实 HTTP/PG17/Redis/原 Worker、仅 SDK transport 替身；Campaign/Ad × 计划缺失/停用/发送后停用，共6例。断言需检查状态、原回执、原值/目标、旧目录不改、只有一次 POST，以及三次重复消息不新增派发。原源码先复现 RUNNING；删除可用计划分支的实际源码探针再次业务 RED，finally/外部备份/cmp 恢复。
- 最终相关回归与集成结果见下方收口记录。前端 TypeScript 检查通过；Ruff/差异检查通过。

## 夹具隔离与失败记录

Root 首次合跑出现6个 REFRESH_PENDING 断言失败，15通过、10排除后中止以读取诊断。精确审查 HEAD 的 `execution.py` 在同一旧证据库上独立复现 ENABLE/Campaign 同错；并非先假定为本次 P2 修复引入。实际查询发现旧夹具累计目录任务占有100个读取、12个发布窗口，新增用例的目录仍 QUEUED。旧夹具只消费本租户消息，却可为历史租户领取全局调度窗口，未在退出时释放。

保留原专用证据库，不清库、不删除原业务事实。新建独立 UTF8 PG17 验证库；最初新库沿集群默认 SQL_ASCII，连接初始化报 bytes 类型错误，改为 template0/UTF8 后继续，不记产品失败。Native 夹具在断言、页面和线程退出之后，仅取消自己租户的未结束目录任务并停用/释放自己调度头；事实、冻结结果、请求回执保留。没有改产品调度、配额或扩大窗口来满足测试。这一隔离修复解释了本轮可复现的窗口阻塞；此前每次早期时序异常是否同因仍为 Unknown。

保留 START 的已知 SQL 数量断言失败：10748项受理为26条、旧断言要求≤25，已在旧起点复现；本次不改阈值、不声称管理全套全绿。

## 边界

真实本地浏览器/HTTP/PostgreSQL17/Redis、原 Worker 函数与持久 outbox payload 有证据；没有独立 broker/Celery 常驻进程投递验收。认证、时间干扰和 SDK 返回为合成输入，没有真实 TikTok/R2/授权/广告写。平台资格、生产稳定性、任意并发交错及无界活性均未证明。没有推送、PR、部署或生产迁移；Epic JB19 仍有其它阶段，不因 M5a 完成关闭。

## Root 收口记录

JB61 修复提交 `f7d83f83`。修复后在新 UTF8 专用库运行两份 Native 套件（排除 workspace）：**55 passed、10 deselected / 143.46s**，包含本次6例、三层双向回读、失败刷新、混合结果、UNKNOWN、20个反向恢复组合及撤权。10个既有页面场景不重复计入这55项；其已匹配的页面证据仍按前述记录继承。

Ruff check/format、TypeScript `tsc -p tsconfig.build.json --noEmit`、差异检查通过。最初从 frontend/node_modules 直接寻找 tsc 未找到，改用已存在的上层共享依赖，不安装或改动依赖。只修改导入顺序的 Ruff 修正不改变运行行为。没有运行全仓测试或重复全站构建。

Root 本地接受 M5a 与 JB61 修复，可按项目规则快进本地 main，并对原工作区做一个代表性页面合入检查。Issue 完成状态、最终 main SHA、专用服务停止及工作树回收的实际读回由外部 `m5a-implementation/closeout.json` 记录，不把尚未执行的动作冒称为既成事实。无远端发布承诺。
