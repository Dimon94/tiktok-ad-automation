# M5c 本地接受与集成

## 范围与实现结果

用户批准 JB-64→JB-65 两票，直接归属 JB-19 Epic；fresh Codex gpt-6.1-sol/high，单一集成树串行。固定基线 `26ea29fd374848d687b35a3c086838202aff533a`，审查候选 `c81ec608817f673d59e66f6865a2a4556c7503e7`。产品来源 `43b21ea9`、root 来源归属修复 `5ce82f64`、恢复产品 `4832fcd1`，候选顶端另有证据文档。此提交只记录接受，不改变产品行为。

沿原工作区/参数表单直接受理，复用原异步准备、Worker、官方 SDK 和目录回读。ROAS 冻结 owner、CBO/ABO、比例/来源/影响成员；Decimal 等额比较；CBO 正常更新去重与逐成员新鲜观察，ABO 独立；UNKNOWN 原执行不变且仅只读核查，符合有效当前证据/权限时可另行明确确认反向恢复。CBO 恢复核对完整成员及各自发送后证据，不借首项代表全系列。无全局强制预览、新 Native executor、依赖或迁移。

## 证据及独立审查

- [JB64逐AC与root补验](2026-11-15-jb64-native-roas-readback.md)：实现者正常/浏览器/拒错证据；root亲跑12项，来源修复及正常链10项，近似错误实际RED→finally/cmp恢复→2项GREEN。结果重叠，不求和。
- [JB65逐AC](2026-11-15-jb65-native-roas-recovery.md)：最终43参数化检查（含2条真实浏览器）、另2项M5a/b代表回归，4个真实源码拒错探针，Ruff/tsc。root另亲跑反向UNKNOWN、CBO/ABO原值NO_CHANGE、各自发送时间4项 /11.35s。
- 真实本地HTTP/PG17/Redis、原管理/目录Worker与持久消息，唯一平台替身在官方SDK transport；浏览器不mock业务HTTP。root复用匹配源码的已有证据，不重复全量验证。
- Whole-M5c fresh Standards R1与Spec R1均PASS：各P0=0、P1=0、P2=1（同一基线问题）、P3=0。两轴只读静态审查，动态证据为继承；不冒称reviewer亲跑。报告原件在外部m5c-implementation及本地private验收归档。

## 已知问题与措辞裁决

两轴同一基线CBO正常发送冲突成员裁剪问题统一登记 [JB-66](https://linear.app/erplus/issue/JB-66/cbo-roas-正常发送前避免裁剪冲突成员后继续系列写入)。固定基线已有，按审查规则P2；非本片新增阻塞。它的静态可达反例是冻结A/B后B原值/来源变化，B被裁剪而A仍发系列影响更新。恢复路径已做完整范围门禁，但正常发送该旧缺口仍保留；当前未执行该反例动态验证。

Spec未明确“缺证据先准备/补齐”是否强制自动刷新。root按用户保留原体验和批准范围明确：现有异步准备读取已发布目录；必需证据缺失的对象明确未验证且零写；投手沿原目录同步入口补齐后重提。本片不新增自动定向刷新门禁/重试循环。此为现有流程措辞收紧，无新增产品变化，已回写JB64并保留原审查依据。

真实Native资格、API实际传播/失败语义、真实授权、平台接受/生效及生产仍Unknown；未用文档/SDK double证明live支持。独立broker/Celery宿主投递未验收。旧SQL26>25与间歇stale UI根因Unknown，未通过孤立绿跑消除。ADS_SYNC_ENABLED等部署开关语义未改。新增可选DTO/恢复capability语义须随未来发布合同审定；本轮不发布。

## 本地集成与收尾

root接受本片本地实现，按项目规则将固定候选及此文档快进本地main，在main做一条实际工作区恢复纵切后关闭JB64/JB65，停止专用PG17/Redis并保留数据，回收本任务树/分支。实际main提交、post-merge结果、两票状态与清理读回保存在外部及根仓库`.local/validation/m5c-20261115/closeout.json`，不得把计划写成已执行。

EpicJB19、基线JB66及其它工作树/未跟踪研究与生成文件保持原状。无push、PR、deploy或真实广告/授权/R2写入。
