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
