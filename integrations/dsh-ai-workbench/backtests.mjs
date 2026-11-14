/** 仅重放 TK-ADA 已冻结规则事实，不读取最新平台数据、不执行动作。 */
export const BACKTEST_RULE = 'tkada_backtest_rule'
export const DAILY_BACKTEST_RULE = 'tkada_daily_backtest_rule'
export function mountDailyBacktestTool(agentCtx, claim, runtime, signal) {
  agentCtx.tools.register({ name: DAILY_BACKTEST_RULE, description: 'Replay each frozen reporting day without execution history. Tests up to five complete candidate PAUSE definitions, including new rules without rule_id; optionally compares a bound rule baseline; returns partitioned spend, revenue, ROAS, blocked-loss proxy and false-stop risk. Cite the returned artifact and limitations.',
    parameters: { type: 'object', properties: { rule_id: { type: 'string', format: 'uuid' }, candidate_definitions: { type: 'array', maxItems: 5, items: { type: 'object', additionalProperties: true } } }, additionalProperties: false },
    output: { schema: { type: 'object', additionalProperties: true }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    async execute(args) { if (signal.aborted) throw new Error('Review cancelled or expired'); return runtime.dailyBacktestRule(claim, args) }
  })
}
export function mountBacktestTool(agentCtx, claim, runtime, signal) {
  agentCtx.tools.register({ name: BACKTEST_RULE, description: 'Test a candidate rule against one frozen rule_evidence ID. Only captured accounts/ranges/metrics are supported. Archives deterministic hit differences; never proves causal savings or false pauses. Cite returned ID and report limitations.',
    parameters: { type: 'object', properties: { evidence_id: { type: 'string', format: 'uuid' }, candidate_definition: { type: 'object', additionalProperties: true } }, required: ['evidence_id', 'candidate_definition'], additionalProperties: false },
    output: { schema: { type: 'object', additionalProperties: true }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    async execute(args) {
      if (signal.aborted) throw new Error('Review cancelled or expired')
      return runtime.backtestRule(claim, args)
    }
  })
}
