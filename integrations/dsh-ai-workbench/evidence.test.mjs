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
