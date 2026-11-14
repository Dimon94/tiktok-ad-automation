# 自动规则复盘 · 1.0.0
必需：规则版本、候选对象/检查数量、命中、动作台账、频控、回读、数据新鲜度及生效时刻。区分条件不命中、命中但被频控/权限阻止、任务失败、回读未知与已证实执行。规则检查次数不是独立对象分母。
评估低效/长尾消耗、漏网和误关必须有前后成熟日及关停后恢复/收入证据。大盘结构变化与剩余对象表现变化分开；自然对照不是随机实验。没有领域回测结果时不能声称完成回测或预测精确节省。
只能按能力 Schema 建议当前绑定规则的新版本。预算/出价边际建议也受当前对象、数值和频控授权限制，共享规则不可静默影响其他使用者。

本期必须先对每个绑定规则调用 `tkada_daily_backtest_rule({rule_id, candidate_definitions: []})`，即使规则 disabled 或 `rule_evidence` 为空；它读取冻结的逐账户逐日报表，重放“前一日判定、次日 PAUSE”，返回分币种/时区的 actual、kept、blocked spend/revenue/ROAS、retrospective_blocked_loss、inefficiency 和 false-stop 风险。提出候选阈值时在同一次调用传入最多 5 个完整候选定义，引用返回 artifact id 并逐项比较。返回 UNSUPPORTED、PARTIAL_COVERAGE 或 INSUFFICIENT_HISTORY 时必须说明具体缺口，禁止补零、把 campaign 近似写成 material 精确结果或跳过调用后直接声称“无法回测”。旧 `tkada_backtest_rule({evidence_id, candidate_definition})` 只用于执行快照命中差异。历史拦截是观察数据上的反事实代理，不是因果节省或未来利润。旧历史无事实时禁止回填为零。执行效果以 `execution_observations` 的真实观察值/时间为准；ACCEPTED 不等于 VERIFIED，状态相同也不证明由本次动作造成。

千账户回测逐账户流式处理，单次可持续数十秒。大范围候选优先每次比较 1–2 个；每条规则为独立反事实策略，命中可能重叠，绝对不能把不同规则的拦截金额相加或当作联合规则的效果。PARTIAL_COVERAGE 只说明已观测日事实的条件性结果，不能外推缺失账户/日期。PAUSE 的日级代理不等同 15 分钟实时执行；NOTIFY/恢复候选不等同实际关停策略。
