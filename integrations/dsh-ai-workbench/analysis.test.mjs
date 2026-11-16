import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mountAnalysisTools } from './analysis.mjs'

test('analysis and notebook tools use only scoped RPCs and reject cancelled work', async () => {
  const tools = new Map(), calls = [], claim = { task: { id: 'review-a' } }
  const signal = new AbortController()
  const runtime = Object.fromEntries(['analyzeEvidence', 'writeNote', 'listNotes'].map(method => [method, async (...args) => { calls.push([method, ...args]); return { id: method } }]))
  mountAnalysisTools({ tools: { register: tool => tools.set(tool.name, tool) } }, claim, runtime, signal.signal)
  const query = { dimension: 'account', maturity_horizon: 'd0', sort_by: 'spend', max_roas: 1, min_spend: 10, limit: 20 }
  await tools.get('tkada_analyze_evidence').execute(query)
  await tools.get('tkada_write_note').execute({ key: 'coverage', body: 'Read full-scope totals first' })
  await tools.get('tkada_list_notes').execute({})
  assert.deepEqual(calls, [['analyzeEvidence', claim, query], ['writeNote', claim, { key: 'coverage', body: 'Read full-scope totals first' }], ['listNotes', claim, {}]])
  for (const tool of tools.values()) {
    assert.equal(tool.parameters.additionalProperties, false)
    signal.abort()
    await assert.rejects(tool.execute({}), /cancelled/)
  }
  assert.equal(calls.length, 3)
})

test('timeline tool passes bounded scope arguments and honours cancellation', async () => {
  const { mountTimelineTool, ANALYZE_TIMELINE } = await import('./analysis.mjs')
  let tool, calls = 0
  const controller = new AbortController()
  const claim = { task: { id: 'task' }, task_token: 'secret' }
  const args = { dimension: 'material', days: 7, advertiser_id: 'a', offset: 2, limit: 1 }
  mountTimelineTool({ tools: { register: value => { tool = value } } }, claim, { analyzeTimeline: async (identity, request) => { calls++; assert.equal(identity, claim); assert.equal(request, args); return { id: 'result' } } }, controller.signal)
  assert.equal(tool.name, ANALYZE_TIMELINE)
  assert.equal((await tool.execute(args)).id, 'result')
  controller.abort()
  await assert.rejects(tool.execute(args), /cancelled/)
  assert.equal(calls, 1)
})
