/** 与后端 ReviewResult 配套：报告、普通建议、可执行配置分别提交。 */
const text = maxLength => ({ type: 'string', minLength: 1, maxLength })
const evidenceIds = { type: 'array', maxItems: 30, items: { type: 'string', format: 'uuid' } }
const object = (properties, required) => ({ type: 'object', properties, required, additionalProperties: false })
export const REVIEW_RESULT_PARAMETERS = object({
  summary: text(1000),
  report: object({
    headline: text(160),
    verdict: text(1000),
    sections: { type: 'array', minItems: 1, maxItems: 12, items: object({
      title: text(100), body: text(4000), evidence_ids: evidenceIds,
      table: object({
        columns: { type: 'array', minItems: 1, maxItems: 12, items: text(1000) },
        rows: { type: 'array', maxItems: 50, items: { type: 'array', minItems: 1, maxItems: 12, items: { type: 'string', maxLength: 1000 } } },
      }, ['columns', 'rows']),
    }, ['title', 'body', 'evidence_ids']) },
    rule_decision: object({
      status: { enum: ['PROPOSED', 'NO_CHANGE', 'INSUFFICIENT_EVIDENCE'] }, reason: text(2000),
    }, ['status', 'reason']),
  }, ['headline', 'verdict', 'sections', 'rule_decision']),
  proposals: { type: 'array', maxItems: 20, items: object({
    kind: { enum: ['strategy', 'rule'] }, operation: { enum: ['create', 'update'] },
    target_id: { type: 'string', format: 'uuid' }, after: { type: 'object' },
    rationale: text(4000), problem: text(2000), expected_effect: text(2000), validation: text(2000),
    evidence_ids: { ...evidenceIds, minItems: 1 },
  }, ['kind', 'operation', 'after', 'rationale', 'problem', 'expected_effect', 'validation', 'evidence_ids']) },
  recommendations: { type: 'array', maxItems: 20, items: object({
    problem: text(2000), suggestion: text(4000), evidence_ids: evidenceIds,
    validation: { type: 'string', maxLength: 2000 }, capability_request: { type: 'string', maxLength: 100 },
  }, ['problem', 'suggestion']) },
  experience_candidates: { type: 'array', maxItems: 10, items: object({
    title: text(200), body: text(4000), applicability: text(2000),
    evidence_ids: { ...evidenceIds, minItems: 1 }, validation: text(2000),
  }, ['title', 'body', 'applicability', 'evidence_ids', 'validation']) },
}, ['summary', 'report', 'proposals'])
