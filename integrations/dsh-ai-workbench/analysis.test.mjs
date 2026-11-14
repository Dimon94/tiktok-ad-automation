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
