import { test } from 'node:test'
import assert from 'node:assert/strict'
import { verificationRuntime } from './verification.mjs'

test('remembers successful paired diagnostics and partitions without retaining raw bodies', async () => {
  const value = { id: 'analysis-1', all_totals: [{ spend: '20', currency: 'USD', engagement_diagnostics: { events: '40', events_per_spend: '2', paired_spend: '20', paired_revenue: '1', body: 'SECRET' } }], items: [{ token: 'SECRET' }], source_refs: ['SECRET'] }
  const v = verificationRuntime({ analyzeEvidence: async () => value, readKnowledge: async () => ({ id: 'k', title: 'SOP', version: 2, body: 'SECRET', source: { authorization: 'SECRET' } }), dailyBacktestRule: async () => ({ id: 'bt', variants: [{ name: 'baseline', status: 'PARTIAL_COVERAGE', metrics: { partitions: [{ currency: 'USD', kept_retrospective_loss: '15', prior_long_tail_share: '0.4' }] } }] }) })
  assert.equal(await v.runtime.analyzeEvidence({}, {}), value)
  await v.runtime.readKnowledge({}, {})
  await v.runtime.dailyBacktestRule({}, {})
  const d = v.diagnostics()
  assert.equal(d.records[0].evidence.all_totals[0].engagement_diagnostics.events_per_spend, '2')
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
