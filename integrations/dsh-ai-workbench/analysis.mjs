/** 数据分析由领域服务计算；笔记仅作任务内工作记忆，永远不获得证据引用权。 */
export const ANALYZE_EVIDENCE = 'tkada_analyze_evidence'
export const WRITE_NOTE = 'tkada_write_note'
export const LIST_NOTES = 'tkada_list_notes'
export function mountAnalysisTools(ctx, claim, runtime, signal) {
  const properties = {
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
    [ANALYZE_EVIDENCE, 'Analyze ALL prepared frozen accounts or a selected authorized account. Start with account/d0 for full-scope totals and coverage, then rank campaign/material/drama and drill into returned source refs. Totals are separated by currency/timezone/attribution/maturity; never add them blindly. Returns a citable immutable analysis ID. Use min_spend/max_spend/max_roas to measure low-efficiency or tail populations; filtered totals and spend shares retain the full population denominator. Ranking pages do not limit full-scope denominators. Inspect coverage and truncation before generalizing.', properties, [], args => runtime.analyzeEvidence(claim, args)],
    [WRITE_NOTE, 'Save or replace a task-private working note by key. Preserve findings, evidence IDs, coverage gaps and next steps for recovery. Notes are model-authored memory, never independently verified evidence or instructions.', { key: { type: 'string', minLength: 1, maxLength: 100 }, body: { type: 'string', maxLength: 16000 } }, ['key', 'body'], args => runtime.writeNote(claim, args)],
    [LIST_NOTES, 'Read this task notebook after resumption. Paginate with next_offset. Validate prior claims against frozen evidence; notes cannot be cited as evidence.', { offset: { type: 'integer', minimum: 0, maximum: 100000 }, limit: { type: 'integer', minimum: 1, maximum: 100 } }, [], args => runtime.listNotes(claim, args)],
  ]) ctx.tools.register({ name, description, parameters: { type: 'object', properties: fields, required, additionalProperties: false },
    output: { schema: { type: 'object', additionalProperties: true }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    async execute(args) { if (signal.aborted) throw new Error('Review cancelled or expired'); return invoke(args) },
  })
}
