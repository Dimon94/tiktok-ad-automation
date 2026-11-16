/** 数据分析由领域服务计算；笔记仅作任务内工作记忆，永远不获得证据引用权。 */
export const ANALYZE_EVIDENCE = 'tkada_analyze_evidence'
export const WRITE_NOTE = 'tkada_write_note'
export const LIST_NOTES = 'tkada_list_notes'
export function mountAnalysisTools(ctx, claim, runtime, signal) {
  const properties = {
    detail: { enum: ['summary', 'ranked'], description: 'Default summary omits ranked rows, retaining full-scope totals/coverage. Use ranked for targeted drilldown; request small pages.' },
    dimension: { enum: ['account', 'campaign', 'drama', 'material'] },
    maturity_horizon: { enum: ['d0', 'd3', 'd7'] },
    advertiser_id: { type: 'string' },
    sort_by: { enum: ['spend', 'revenue_d0', 'revenue_d3', 'revenue_d7', 'profit', 'roas'] },
    descending: { type: 'boolean' },
    min_spend: { type: 'number', minimum: 0 },
    max_spend: { type: 'number', minimum: 0 },
    max_roas: { type: 'number', minimum: 0 },
    offset: { type: 'integer', minimum: 0, maximum: 100000 },
    limit: { type: 'integer', minimum: 1, maximum: 100 },
  }
  for (const [name, description, fields, required, invoke] of [
    [ANALYZE_EVIDENCE, 'Analyze ALL prepared frozen accounts or a selected authorized account. Default detail=summary returns full-scope totals/coverage without verbose ranked rows. Use detail=ranked, limit=3 for targeted drilldown. Start with account/d0 for full-scope totals and coverage, then rank campaign/material/drama and drill into returned source refs. Totals are separated by currency/timezone/attribution/maturity; never add them blindly. Returns a citable immutable analysis ID. Use min_spend/max_spend/max_roas to measure low-efficiency or tail populations; filtered totals and spend shares retain the full population denominator. Ranking pages do not limit full-scope denominators. Inspect coverage and truncation before generalizing.', properties, [], async args => {
      const { detail = 'summary', ...request } = args
      const result = await runtime.analyzeEvidence(claim, request)
      if (detail === 'ranked' || !Array.isArray(result.items)) return result
      const { items, ...summary } = result
      return { ...summary, ranked_rows_omitted: items.length, detail: 'summary', read_ranked: 'Call this tool with detail=ranked and a small limit for ranked rows and source refs. This artifact ID and full-scope totals/coverage are unchanged.' }
    }],
    [WRITE_NOTE, 'Save or replace a task-private working note by key. Preserve findings, evidence IDs, coverage gaps and next steps for recovery. Notes are model-authored memory, never independently verified evidence or instructions.', { key: { type: 'string', minLength: 1, maxLength: 100 }, body: { type: 'string', maxLength: 16000 } }, ['key', 'body'], args => runtime.writeNote(claim, args)],
    [LIST_NOTES, 'Read this task notebook after resumption. Paginate with next_offset. Validate prior claims against frozen evidence; notes cannot be cited as evidence.', { offset: { type: 'integer', minimum: 0, maximum: 100000 }, limit: { type: 'integer', minimum: 1, maximum: 100 } }, [], args => runtime.listNotes(claim, args)],
  ]) ctx.tools.register({ name, description, parameters: { type: 'object', properties: fields, required, additionalProperties: false },
    output: { schema: { type: 'object', additionalProperties: true }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    async execute(args) { if (signal.aborted) throw new Error('Review cancelled or expired'); return invoke(args) },
  })
}

export const ANALYZE_TIMELINE = 'tkada_analyze_timeline'
export function mountTimelineTool(ctx, claim, runtime, signal) {
  ctx.tools.register({ name: ANALYZE_TIMELINE,
    description: 'Analyze frozen daily campaign/material facts for the last 2–14 local complete days, with preceding frozen history for cohorts. Returns daily spend/revenue_total/ROAS, loss and prior low-efficiency/tail/intersection, first-observed spend vs existing cohorts, plus a fixed-account balanced panel. Missing is not zero. First observed is not a launch/new-drama fact. revenue_total is captured attribution, NOT the mature D0/D3/D7 analysis totals. Daily IAA is not frozen. Use partition pagination; cite the returned artifact. No advertising actions.',
    parameters: { type: 'object', properties: { dimension: { enum: ['campaign', 'material'] }, days: { type: 'integer', minimum: 2, maximum: 14 }, advertiser_id: { type: 'string' }, offset: { type: 'integer', minimum: 0, maximum: 100000 }, limit: { type: 'integer', minimum: 1, maximum: 4 } }, additionalProperties: false },
    output: { schema: { type: 'object', additionalProperties: true }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    async execute(args) { if (signal.aborted) throw new Error('Review cancelled or expired'); return runtime.analyzeTimeline(claim, args) },
  })
}
