import { test } from 'node:test'
import assert from 'node:assert/strict'
import { verificationRuntime } from './verification.mjs'

test('remembers successful paired diagnostics and partitions without retaining raw bodies', async () => {
  const value = { id: 'analysis-1', totals: [{ spend: '20', currency: 'USD', engagement_diagnostics: { events: '40', events_per_spend: '2', paired_spend: '20', paired_revenue: '1', body: 'SECRET' } }], items: [{ token: 'SECRET' }], source_refs: ['SECRET'] }
  const v = verificationRuntime({ analyzeEvidence: async () => value, readKnowledge: async () => ({ id: 'k', title: 'SOP', version: 2, body: 'SECRET', source: { authorization: 'SECRET' } }), dailyBacktestRule: async () => ({ id: 'bt', variants: [{ name: 'baseline', status: 'PARTIAL_COVERAGE', metrics: { partitions: [{ currency: 'USD', kept_retrospective_loss: '15', prior_long_tail_share: '0.4' }] } }] }) })
  assert.equal(await v.runtime.analyzeEvidence({}, {}), value)
  await v.runtime.readKnowledge({}, {})
  await v.runtime.dailyBacktestRule({}, {})
  const d = v.diagnostics()
  assert.equal(d.records[0].evidence.totals[0].engagement_diagnostics.events_per_spend, '2')
  assert.equal(d.records[2].evidence.variants[0].metrics.partitions[0].kept_retrospective_loss, '15')
  assert.equal(JSON.stringify(d).includes('SECRET'), false)
})

test('failed calls are not remembered and retained diagnostics have bounded size', async () => {
  let count = 0
  const v = verificationRuntime({ analyzeEvidence: async () => { if (!count++) throw new Error('failed'); return { id: String(count), all_totals: Array.from({ length: 1000 }, () => ({ spend: '9'.repeat(10000) })) } } })
  await assert.rejects(v.runtime.analyzeEvidence())
  assert.equal(v.diagnostics().records.length, 0)
  for (let i = 0; i < 100; i++) await v.runtime.analyzeEvidence()
  assert.ok(v.diagnostics().records.length <= 16)
  assert.ok(JSON.stringify(v.diagnostics()).length < 65000)
})

test('recovery pointers distinguish source pages from analysis artifacts and remain bounded', async () => {
  let n = 0
  const v = verificationRuntime({
    readEvidence: async () => ({ id: 'page', source_id: 'source', category: 'business', body: 'PRIVATE-BODY' }),
    analyzeEvidence: async (_c, request) => ({ id: 'artifact-' + n++, request, body: 'PRIVATE-BODY' }),
  })
  await v.runtime.readEvidence({}, { evidence_id: 'source', offset: 10, limit: 1 })
  const request = { dimension: 'account', maturity_horizon: 'd0' }
  await v.runtime.analyzeEvidence({}, request)
  const state = v.resumeContext()
  assert.equal(state.references[0].source_id, 'source')
  assert.equal(state.references[0].id, 'page')
  assert.deepEqual(state.references[0].request, { evidence_id: 'source', offset: 10, limit: 1 })
  assert.deepEqual(state.references[1].request, request)
  assert.deepEqual(state.verified.horizons, ['d0'])
  assert.doesNotMatch(JSON.stringify(state), /PRIVATE-BODY/)
  for (let i = 0; i < 150; i++) await v.runtime.analyzeEvidence({}, { dimension: 'material', advertiser_id: 'a'.repeat(1000), offset: i })
  assert.ok(JSON.stringify(v.resumeContext().references).length <= 12000)
  assert.deepEqual(v.resumeContext().verified.horizons, ['d0'])
})


test('timeline reminders keep endpoint cohort values but discard bodies and secrets', async () => {
  const row = { date: '2026-11-13', spend: '100', loss_proxy: '60', cohorts: [{ cohort: 'prior_low_spend', spend: '40', body: 'SECRET' }], token: 'SECRET' }
  const v = verificationRuntime({ analyzeTimeline: async () => ({ id: 'timeline-1', coverage: { requested_accounts: 100 }, partitions: [{ currency: 'USD', timezone: 'UTC', daily: [row], balanced_panel: { accounts: 90, daily: [row] } }], limitations: { authorization: 'SECRET' } }) })
  await v.runtime.analyzeTimeline()
  const d = v.diagnostics()
  assert.equal(d.records[0].evidence.timeline.partitions[0].balanced_panel.daily_endpoints[0].cohorts[0].spend, '40')
  assert.equal(JSON.stringify(d).includes('SECRET'), false)
})


test('bounded reminders preserve scope diagnostics while lower-priority ranking pages rotate', async () => {
  let n = 0
  const v = verificationRuntime({ analyzeEvidence: async (_claim, request) => ({ id: String(++n), request, totals: [{ spend: String(n) }] }) })
  await v.runtime.analyzeEvidence({}, { dimension: 'account', maturity_horizon: 'd0' })
  for (let i = 0; i < 30; i++) await v.runtime.analyzeEvidence({}, { dimension: 'material', offset: i })
  assert.equal(v.diagnostics().records[0].evidence.request.dimension, 'account')
  assert.ok(v.diagnostics().records.length <= 16)
})

test('completion requires actual full-scope work and every rule, independent of reminder eviction', async () => {
  const evidence = { optimizer: { advertiser_ids: ['a', 'b'] }, rules: [{ id: 'r1' }, { id: 'r2' }], source_catalog: ['rule_backtest_inputs', 'history', 'business', 'data_pack_business', 'rule_evidence'].map(category => ({ category, source_count: 2 })) }
  const methods = new Set(['business', 'materials', 'rules', 'builds', 'scale'])
  let id = 0
  const v = verificationRuntime({
    analyzeEvidence: async (_claim, request) => ({ id: String(++id), request, totals: [] }),
    analyzeTimeline: async (_claim, request) => ({ id: String(++id), request, partitions: [{ daily: [{ loss_proxy: '10' }] }] }),
    dailyBacktestRule: async (_claim, request) => ({ id: String(++id), rule_id: request.rule_id || null, variants: request.rule_id ? [{ name: 'baseline', status: 'UNSUPPORTED' }] : [{ name: 'candidate_1', status: request.invalid ? 'INVALID_DEFINITION' : 'COMPLETE', ...(request.invalid ? {} : { metrics: {} }) }] }),
    readEvidence: async (_claim, request) => ({ id: String(++id), category: request.category, rows: [] }),
    searchKnowledge: async () => ({ items: [{ id: 'knowledge' }] }),
    readKnowledge: async (_claim, request) => ({ id: request.knowledge_id, body: 'SECRET' }),
  })
  assert.ok(v.completionGaps(evidence, methods).length > 5)
  await v.runtime.analyzeEvidence({}, { dimension: 'account', maturity_horizon: 'd0', advertiser_id: 'a' })
  await v.runtime.analyzeEvidence({}, { dimension: 'account', maturity_horizon: 'd0', min_spend: '1' })
  assert.ok(v.completionGaps(evidence, methods).some(g => g.includes('maturity_horizon=d0')))
  for (const maturity_horizon of ['d0', 'd3', 'd7']) await v.runtime.analyzeEvidence({}, { dimension: 'account', maturity_horizon })
  for (const dimension of ['campaign', 'material']) await v.runtime.analyzeTimeline({}, { dimension })
  await v.runtime.dailyBacktestRule({}, { rule_id: 'r1' })
  assert.ok(v.completionGaps(evidence, methods).some(g => g.includes('rule_id=r2')))
  await v.runtime.dailyBacktestRule({}, { rule_id: 'r2' })
  for (const category of ['history', 'business', 'data_pack_business', 'rule_evidence']) await v.runtime.readEvidence({}, { category })
  await v.runtime.searchKnowledge()
  await v.runtime.readKnowledge({}, { knowledge_id: 'unrelated' })
  assert.ok(v.completionGaps(evidence, methods).some(g => g.includes('knowledge body')))
  await v.runtime.readKnowledge({}, { knowledge_id: 'knowledge' })
  await v.runtime.dailyBacktestRule({}, { candidate_definitions: [{}], invalid: true })
  assert.ok(v.completionGaps(evidence, methods).some(g => g.includes('NEW policy')))
  await v.runtime.dailyBacktestRule({}, { candidate_definitions: [{}] })
  for (let i = 0; i < 40; i++) await v.runtime.analyzeEvidence({}, { dimension: 'material' })
  assert.deepEqual(v.completionGaps(evidence, methods), [])
  assert.equal(JSON.stringify(v.diagnostics()).includes('SECRET'), false)
  assert.ok(v.completionGaps(evidence, new Set()).some(g => g.includes('ai-delivery-review-materials')))
})

test('empty knowledge search is a legitimate gap and failed tools never satisfy work', async () => {
  const evidence = { optimizer: { advertiser_ids: ['a'] }, source_catalog: [] }
  const methods = new Set(['business', 'builds', 'scale'])
  const v = verificationRuntime({ searchKnowledge: async () => ({ items: [] }), analyzeEvidence: async (_claim, request) => { if (request.maturity_horizon === 'd7') throw Error('transport failure'); return { id: 'a', request } } })
  await v.runtime.searchKnowledge()
  for (const maturity_horizon of ['d0', 'd3']) await v.runtime.analyzeEvidence({}, { dimension: 'account', maturity_horizon })
  await assert.rejects(v.runtime.analyzeEvidence({}, { dimension: 'account', maturity_horizon: 'd7' }))
  assert.equal(v.completionGaps(evidence, methods).length, 1)
  assert.ok(v.completionGaps(evidence, methods)[0].includes('d7'))
})


test('equal financial totals retain distinct campaign/material cohort and balanced-panel checks', async () => {
  const v = verificationRuntime({ analyzeTimeline: async (_claim, request) => ({ id: request.dimension, request, partitions: [{ currency: 'USD', timezone: 'UTC', daily: [{ date: '2026-11-13', spend: '100', roas: '0.4', cohorts: [{ cohort: 'first_observed_spend', spend: request.dimension === 'campaign' ? '36' : '1', spend_share: request.dimension === 'campaign' ? '0.36' : '0.01' }] }], balanced_panel: { accounts: request.dimension === 'campaign' ? 381 : 274, daily: [{ roas: request.dimension === 'campaign' ? '0.50' : '0.74' }] } }] }) })
  for (const dimension of ['campaign', 'material']) await v.runtime.analyzeTimeline({}, { dimension })
  const checks = v.diagnostics().records.map(r => r.evidence.timeline.partitions[0].dimension_check)
  assert.deepEqual(checks.map(c => [c.dimension, c.balanced_accounts, c.balanced_roas_end, c.last_day_cohorts[0].spend_share]), [['campaign', 381, '0.50', '0.36'], ['material', 274, '0.74', '0.01']])
})
