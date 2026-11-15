# M5b 原生预算：本地接受与集成依据

## 范围与实际交付

用户批准 JB-62→JB-63 两票及其依赖，随后要求 fresh Codex gpt-6.1-sol/high 执行 implement-spec。两票直接归 Epic JB-19；输入为批准的完整票据和 approved-scope，不补造缺失的正式Spec/T/R。沿用已批准的 root独立集成树、逐票fresh上下文、串行唯一写者。原工作区直接受理、异步准备、通用管理owner、官方SDK和当前官方金额/权限校验保持。

基准 `0e7cf885018922bc111c28de49a99f4b163c9f3e`。JB62源码 `84db9974b69af5eb97ca82a66f30590803db350b`，阶段接受 `34b1e0cf5b4fa7670995390d99504a0ca1684c48`；JB63源码 `208ef0302872ed9e365656cc961b67337fbf6abe`。整片固定审查HEAD `d033bd243341aa2b89404e157d17cdc4344448c3`，五提交、十七路径。没有迁移、依赖升级、原生专属执行器、全局预览要求、排期或ROAS编辑。

- 预算实际归属和CBO去重复用原实现；准备时冻结原值/目标值、owner账户币种、选中来源及归并/展开原因。跨块只合并来源，不重算金额或覆盖已冻结币种。affected_refs表示实际预算修改对象，不承诺穷尽未选后代；来源单独保留。
- 原任务详情及恢复预览共用预算事实展示；广告列表补齐预算入口，仍使用原表单/直接受理路径。历史没有冻结事实的任务保持缺项，不从今日账户元数据补造历史含义。
- SDK接受后预算进入待观察；只有发送后、同连接、有效当前授权的真实目录事实匹配才记观察成功。不匹配或缺观察来源需检查；刷新不重发预算，SDK payload不改排期或定向。
- UNKNOWN只读核查匹配/不同/缺失/过期；执行和原request receipt不被观察冒充。显式恢复是单独确认的新反向意图，使用冻结原金额，当前金额、来源连接、发送后证据及当前权限在预览/发送前复核。Decimal等额不同文本表示不会误判；管理许可复核包含UNKNOWN来源项。

## 独立阶段验收与继承证据

完整逐AC及命令见 [JB62](2026-11-15-jb62-native-budget-readback.md)、[JB63](2026-11-15-jb63-native-budget-recovery.md)。

Root亲跑：JB62精确候选的chain、multi_account、readback_revocation共7通过/25.58s；JB63精确候选的current_gate、mixed_owners_exact_once共17通过/59.63s。均为真实HTTP、PG17/Redis、原Worker、SDK传输替身；Ruff与差异检查通过。两次是不同阶段集合，不借它们宣称全部历史/并发交错已证明。

源码匹配继承：JB62最终75检查、60个有限金额/owner组合、原工作区浏览器、五个实际源码RED→finally/cmp恢复→GREEN探针；普通回归146通过后，唯一旧刷新排队即ACCEPTED断言按真实新鲜观察合同改为REFRESH_PENDING，定向1通过。JB63最终32预算专项、10动作检查、两个预算恢复工作区、七个实际源码拒错探针；正式/demo构建和tsc均有证据。Root未重复全套，也不把重叠集合累加成独立覆盖数。

## Standards

**PASS，P0=0/P1=0/P2=0/P3=0。** [完整报告](native-m5b-review/review-r1-standards.md)。fresh Codex gpt-6.1-sol/high仅执行该轴，核对固定refs/merge-base/五提交/十七路径；追踪共享的非Native、状态、ROAS、改名、自动任务及预览调用方。亲跑Python3.14只读AST和exact diff-check，动态证据明确继承。

## Spec

**PASS，P0=0/P1=0/P2=0/P3=0。** [完整报告](native-m5b-review/review-r1-spec.md)。独立fresh同模型，仅按两张批准票和scope核对归属/冻结/展示/SDK/读取/UNKNOWN/恢复/权限，未采用旧未获批预览方案。亲跑只读AST/diff-check，未冒称亲跑动态纵链。两轴互不读取对方报告，无产品修改。

汇总：Standards PASS（0/0/0/0）；Spec PASS（0/0/0/0），均无阻断或未处理finding。Root据已通过阶段和独立审查接受M5b本地交付；本地集成后仍做一个原工作区恢复纵链最小检查，实际main SHA、Issue完成与环境/工作树读回记录于外部 `m5b-implementation/closeout.json`，不提前冒称已执行。

## 失败与Unknown

JB63较大回归为122通过、1条旧状态stale浏览器失败；单例隔离重跑通过，失败时存在另一共享库runner，调度干扰只是可能解释，根因仍Unknown。最后32项预算检查为串行最终绿跑，不用单次重跑证明所有间歇已修复。旧direct10748 SQL count26>25保留，不改阈值、不声称历史全套全绿。夹具导入、非法合成状态/外键、SDK替身字段和页面异步等待修正均明确不计产品RED。

本地测试使用独立UTF8 PG17集群/Redis和原Worker函数/持久payload，非独立常驻broker/Celery交付验收；认证/时间/SDK输入为合成。有限枚举与实际拒错不是无界形式证明。真实TikTok接受、官方政策实际资格、生产、真实授权和真实广告/预算写均未验证。没有推送、PR、部署或生产迁移。EpicJB19仍有后续阶段，不因M5b完成关闭。
