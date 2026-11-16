import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mountEvidenceTools, QUERY_EVIDENCE, READ_EVIDENCE } from './evidence.mjs'

test('evidence tools pass scoped identity and page arguments; cancelled reads never reach transport', async () => {
  const tools = new Map(), calls = [], claim = { task: { id: 'task-a' }, task_token: 'private' }
  const controller = new AbortController()
  mountEvidenceTools({ tools: { register: tool => tools.set(tool.name, tool) } }, claim, {
    queryEvidence: async (...args) => { calls.push(args); return { items: [], next_offset: 10 } },
    readEvidence: async (...args) => { calls.push(args); return { id: 'read-page', rows: [{ spend: '10' }] } },
  }, controller.signal)
  const query = { category: 'reporting', offset: 10 }
  assert.equal((await tools.get(QUERY_EVIDENCE).execute(query)).next_offset, 10)
  const read = { evidence_id: 'frozen-report', offset: 20, limit: 1 }
  const page = await tools.get(READ_EVIDENCE).execute(read)
  assert.equal(page.id, 'read-page')
  assert.deepEqual(calls, [[claim, query], [claim, read]])
  assert.equal(tools.get(READ_EVIDENCE).parameters.additionalProperties, false)
  assert.equal(tools.get(READ_EVIDENCE).parameters.properties.limit.maximum, 100)
  assert.ok(!tools.get(READ_EVIDENCE).output.render(read, page)[0].text.includes('private'))
  controller.abort()
  await assert.rejects(tools.get(QUERY_EVIDENCE).execute(query), /cancelled/)
  await assert.rejects(tools.get(READ_EVIDENCE).execute(read), /cancelled/)
  assert.equal(calls.length, 2)
})


test('provenance selectors and inner cursor reach the scoped reader and preserve delivered raw proof', async () => {
  const tools = new Map(), calls = [], claim = { task: { id: 'task-a' }, task_token: 'private' }
  const record = { remote_id: 'c1', fact: { spend: '0.100000000000000001' }, publication: { publication_version: 7, observed_at: '2026-11-13T01:00:00Z' } }
  const delivery = { id: 'delivered-page', records: [record], next_cursor: 'inner-next', total_records: 200 }
  mountEvidenceTools({ tools: { register: tool => tools.set(tool.name, tool) } }, claim, {
    readEvidence: async (...args) => { calls.push(args); return delivery },
  }, new AbortController().signal)
  const tool = tools.get(READ_EVIDENCE)
  const args = { evidence_id: 'frozen-source', view: 'provenance', report_id: 'archive-report', row_key: 'account-1', bucket: { level: 'campaign', source_kind: 'PLATFORM_DIRECT', contract_revision: 'native-growth-day-v1', currency: 'USD' }, component: 'component-digest', evidence_cursor: 'inner-current', limit: 25 }
  assert.equal(tool.parameters.additionalProperties, false)
  assert.ok(Object.keys(args).every(key => key in tool.parameters.properties))
  assert.deepEqual(tool.parameters.properties.view.enum, ['reading', 'provenance'])
  assert.deepEqual(tool.parameters.properties.bucket.required.sort(), Object.keys(args.bucket).sort())
  assert.equal(tool.parameters.properties.bucket.additionalProperties, false)
  const result = await tool.execute(args)
  assert.deepEqual(calls, [[claim, args]])
  assert.deepEqual(JSON.parse(tool.output.render(args, result)[0].text), delivery)
  assert.equal(result.records[0].fact.spend, '0.100000000000000001')
})
