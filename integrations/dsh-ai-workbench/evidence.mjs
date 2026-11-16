/** 只能读取本任务的冻结源；目录 ID 不等于可引用的已读页 ID。 */
export const QUERY_EVIDENCE = 'tkada_query_evidence'
export const READ_EVIDENCE = 'tkada_read_evidence'
export function mountEvidenceTools(ctx, claim, runtime, signal) {
  for (const [name, description, properties, required, invoke] of [
    [QUERY_EVIDENCE, 'Query frozen evidence metadata at task cutoff, including reporting dimensions and D0/D3/D7 windows. Paginate with next_offset. Index entries are not read evidence and cannot be cited.',
      { category: { enum: ['reporting', 'plans', 'build_occurrences', 'rule_evidence', 'attribution', 'material_orders', 'history', 'business', 'operation_logs', 'data_pack_business', 'data_pack_operation_events', 'knowledge_refs', 'preparation'] }, offset: { type: 'integer', minimum: 0 }, advertiser_id: { type: 'string' }, dimension: { enum: ['account', 'campaign', 'drama', 'material'] }, maturity_horizon: { enum: ['d0', 'd3', 'd7'] } }, ['category'], args => runtime.queryEvidence(claim, args)],
    [READ_EVIDENCE, 'Read a frozen evidence page by index ID. Start with limit 10 (max 100); paginate with next_offset. Cite returned page id, never unread index IDs. Reporting metadata carries maturity, coverage and sample truncation; missing is not zero. Read rule history before backtesting. For exact archived facts use view=provenance and the evidence_ref report owner_id, row_key, bucket and component; continue with evidence_cursor from next_cursor. These pages can split a single large row. Only the returned page id is citable.',
      { evidence_id: { type: 'string', format: 'uuid' }, view: { enum: ['reading', 'provenance'] }, report_id: { type: 'string', maxLength: 128 }, row_key: { type: 'string', maxLength: 512 }, component: { type: 'string', maxLength: 128 }, evidence_cursor: { type: 'string', maxLength: 8192 }, bucket: { type: 'object', additionalProperties: false, required: ['level', 'source_kind', 'contract_revision', 'currency'], properties: { level: { type: ['string', 'null'] }, source_kind: { type: ['string', 'null'] }, contract_revision: { type: ['string', 'null'] }, currency: { type: 'string' } } }, offset: { type: 'integer', minimum: 0, maximum: 1000000000 }, limit: { type: 'integer', minimum: 1, maximum: 100 } }, ['evidence_id'], args => runtime.readEvidence(claim, args)],
  ]) ctx.tools.register({ name, description, parameters: { type: 'object', properties, required, additionalProperties: false },
    output: { schema: { type: 'object', additionalProperties: true }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    async execute(args) { if (signal.aborted) throw new Error('Review cancelled or expired'); return invoke(args) },
  })
}
