import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { loadMethods, skillName } from './methods.mjs'
import { createRuntime, runClaim, COMPLETE_TOOL } from './runtime.mjs'

test('service credential is used only for claim, task completion uses scoped token', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'tkada-review-'))
  try {
    const tokenFile = join(directory, 'service-token')
    await writeFile(tokenFile, 'private-service-key', { mode: 0o600 })
    const requests = []
    const runtime = createRuntime({ url: 'https://tkada.example', tokenFile, stateRoot: directory }, async (url, options) => {
      requests.push({ url, options })
      return { ok: true, json: async () => ({ status: 'COMPLETE' }) }
    })
    await runtime.claim()
    await runtime.complete({ task: { id: 'task-1' }, task_token: 'short-task-key' }, { summary: 'no change', proposals: [] })
    await runtime.searchKnowledge({ task: { id: 'task-1' }, task_token: 'short-task-key' }, { query: '赔付' })
    await runtime.readKnowledge({ task: { id: 'task-1' }, task_token: 'short-task-key' }, { knowledge_id: 'knowledge-1' })
    await runtime.queryEvidence({ task: { id: "task-1" }, task_token: "short-task-key" }, { category: "reporting", offset: 10 })
    await runtime.readEvidence({ task: { id: "task-1" }, task_token: "short-task-key" }, { evidence_id: "source-1", offset: 20 })
    assert.ok(requests[4].url.endsWith("/task-1/evidence/query"))
    assert.ok(requests[5].url.endsWith("/task-1/evidence/read"))
    assert.equal(requests[4].options.headers.authorization, "Bearer short-task-key")
    assert.equal(requests[5].options.headers.authorization, "Bearer short-task-key")
    assert.equal(requests[2].options.headers.authorization, 'Bearer short-task-key')
    assert.equal(requests[3].options.headers.authorization, 'Bearer short-task-key')
    assert.ok(requests[2].url.endsWith('/task-1/knowledge/search'))
    assert.ok(requests[3].url.endsWith('/task-1/knowledge/read'))
    assert.equal(requests[0].options.headers.authorization, 'Bearer private-service-key')
    assert.equal(requests[1].options.headers.authorization, 'Bearer short-task-key')
    assert.equal(requests[1].options.redirect, 'error')
    assert.throws(() => createRuntime({ url: 'http://outside.example', tokenFile, stateRoot: directory }), /HTTPS/)
  } finally { await rm(directory, { recursive: true, force: true }) }
})

test('native DSH session has only structured result tool and a tenant/task directory', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'tkada-dsh-test-'))
  try {
    const claim = { task: { id: 'task-1', tenant_id: 'tenant-a', optimizer_id: 'optimizer-a', fence: 'fence-1', expires_at: new Date(Date.now() + 60000).toISOString(), evidence: { optimizer: { id: 'optimizer-a' }, methods: await loadMethods() } }, task_token: 'short-token' }
    let registered, allowed, guard, prompt, options, disposed = false
    const tools = new Map(), skills = new Map()
    const ctx = { get: () => ({ currentSelection: () => ({ provider: 'test', model: 'test' }) }), agentPresets: { mount: async () => {} }, agents: { create: async input => {
      options = input
      const agent = { followup: message => { prompt = message }, whenIdle: async () => { await assert.rejects(registered.execute({ summary: 'premature', proposals: [] }), /Load the main/); await tools.get('skill').execute({ name: skillName('ai-delivery-review') }); await tools.get('skill').execute({ name: skillName('scale') }); assert.equal((await tools.get('tkada_search_knowledge').execute({ query: '补贴' })).items[0].id, 'knowledge-1'); assert.equal((await tools.get('tkada_read_knowledge').execute({ knowledge_id: 'knowledge-1' })).body, 'policy text'); await registered.execute({ summary: 'insufficient evidence', proposals: [] }) } }
      await input.setup({ inject: async (names, callback) => { assert.deepEqual(names, ['tools', 'skills']); return callback({ skills: { register: s => skills.set(s.name, s), get: async name => skills.get(name) }, tools: { restrict: value => { allowed = value.allow }, guard: value => { guard = value }, register: value => { tools.set(value.name, value); if (value.name === COMPLETE_TOOL) registered = value } } }) } }, agent)
      return { agent, dispose: async () => { disposed = true } }
    } } }
    const completions = []
    await runClaim(ctx, { stateRoot: directory }, claim, { searchKnowledge: async () => ({ items: [{ id: 'knowledge-1' }] }), readKnowledge: async () => ({ id: 'knowledge-1', body: 'policy text' }), complete: async (identity, result) => { completions.push({ identity, result }); return { status: 'COMPLETE' } } }, { SessionId: value => value, createUserMessage: value => value, renderSkillContent: s => s.content, scopeOf: () => "task-scope" })
    assert.deepEqual(allowed, [])
    assert.deepEqual([...tools.keys()].sort(), ["tkada_analyze_evidence", "tkada_write_note", "tkada_list_notes", "tkada_query_evidence", "tkada_read_evidence", "skill", "tkada_search_knowledge", "tkada_read_knowledge", "tkada_backtest_rule", COMPLETE_TOOL].sort())
    assert.equal(guard({ name: 'exec' }), 'Only task-scoped evidence, analysis, notes, skills and review completion are permitted')
    assert.ok(options.meta.cwd.includes('tenant-a/optimizer-a/task-1/fence-1'))
    assert.ok(!JSON.stringify(prompt).includes('short-token'))
    assert.equal(completions.length, 1)
    assert.deepEqual(completions[0].result.method_usage, ['ai-delivery-review', 'scale'])
    assert.ok(!JSON.stringify(prompt).includes('必需：完整当地日'))
    assert.equal(skills.size, 6)
    assert.equal(disposed, true)
  } finally { await rm(directory, { recursive: true, force: true }) }
})

test('session setup failure reports a terminal failed review instead of waiting for repeated expiry', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'tkada-dsh-failure-'))
  const results = []
  try {
    const claim = { task: { id: 'task', tenant_id: 'tenant', optimizer_id: 'optimizer', fence: 'fence', expires_at: new Date(Date.now() + 60000).toISOString(), evidence: { methods: await loadMethods() } } }
    const ctx = { agents: { create: async () => { throw new Error('fixture setup failure') } } }
    await assert.rejects(runClaim(ctx, { stateRoot: directory, modelSelection: { provider: 'fixture', model: 'fixture' } }, claim,
      { complete: async (_claim, result) => { results.push(result) } }, { SessionId: x => x }), /fixture setup failure/)
    assert.equal(results.length, 1)
    assert.equal(results[0].failed, true)
    assert.deepEqual(results[0].proposals, [])
    assert.ok(!results[0].summary.includes('fixture setup failure'))
  } finally { await rm(directory, { recursive: true, force: true }) }
})

test('analysis RPCs never use service credentials and safe errors let an Agent correct arguments', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'tkada-analysis-rpc-'))
  try {
    const calls = []
    const runtime = createRuntime({ url: 'https://tkada.example', tokenFile: join(directory, 'never-read'), stateRoot: directory }, async (url, options) => {
      calls.push({ url, options })
      return { ok: !url.endsWith('/evidence/analyze'), status: 422, json: async () => ({ code: 'ai_evidence_not_found', message: 'sensitive upstream detail', api_key: 'private' }) }
    })
    const claim = { task: { id: 'task-1' }, task_token: 'task-only' }
    await assert.rejects(runtime.analyzeEvidence(claim, {}), error => {
      assert.match(error.message, /422 ai_evidence_not_found/)
      assert.doesNotMatch(error.message, /sensitive|api_key|private|task-only/)
      return true
    })
    await runtime.writeNote(claim, { key: 'findings', body: 'checkpoint' })
    await runtime.listNotes(claim, {})
    assert.ok(calls[1].url.endsWith('/task-1/notes'))
    assert.ok(calls[2].url.endsWith('/task-1/notes/list'))
    assert.ok(calls.every(call => call.options.headers.authorization === 'Bearer task-only'))
  } finally { await rm(directory, { recursive: true, force: true }) }
})

test('heartbeat updates the in-memory token without leaking it to the Agent', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'tkada-renew-test-'))
  const originalInterval = globalThis.setInterval
  let heartbeat, prompt
  try {
    globalThis.setInterval = callback => { heartbeat = callback; return { fixture: true } }
    const claim = { task: { id: 'task', tenant_id: 'tenant', optimizer_id: 'optimizer', fence: 'fence', expires_at: new Date(Date.now()+30000).toISOString(), max_expires_at: new Date(Date.now()+7200000).toISOString(), evidence: { methods: await loadMethods() } }, task_token: 'old-private-token' }
    let completion
    const tools = new Map(), skills = new Map()
    const ctx = { agentPresets: { mount: async () => {} }, agents: { create: async input => {
      await input.setup({ inject: async (_names, callback) => callback({ skills: { register: s => skills.set(s.name, s), get: async name => skills.get(name) }, tools: { register: t => tools.set(t.name, t), restrict() {}, guard() {} } }) })
      return { agent: { followup: message => { prompt = message }, whenIdle: async () => {
        await heartbeat()
        await tools.get('skill').execute({ name: skillName('ai-delivery-review') })
        await tools.get(COMPLETE_TOOL).execute({ summary: 'done', proposals: [] })
      } }, dispose: async () => {} }
    } } }
    await runClaim(ctx, { stateRoot: directory, modelSelection: { provider: 'fixture', model: 'fixture' } }, claim,
      { heartbeat: async () => ({ task_token: 'renewed-private-token', expires_at: new Date(Date.now()+300000).toISOString() }), complete: async (identity) => { completion = identity.task_token; return { status: 'COMPLETE' } } },
      { SessionId: x => x, createUserMessage: x => x, renderSkillContent: s => s.content, scopeOf: () => 'task' })
    assert.equal(completion, 'renewed-private-token')
    assert.doesNotMatch(JSON.stringify(prompt), /private-token/)
  } finally { globalThis.setInterval = originalInterval; await rm(directory, { recursive: true, force: true }) }
})

test('native validation errors expose only bounded locations, types and numeric constraints', async () => {
  const runtime = createRuntime({ url: 'https://tkada.example', tokenFile: '/never-read', stateRoot: '/private' }, async () => ({ ok: false, status: 422, json: async () => ({ detail: [
    { loc: ['body', 'offset'], type: 'less_than_equal', msg: 'sensitive validation message', input: 'private-value', ctx: { le: 100000, dangerous: 'private' } },
    { loc: ['body', 'secret-private-field'], type: 'extra_forbidden', input: 'secret-private-body' },
  ] }) }))
  await assert.rejects(runtime.analyzeEvidence({ task: { id: 'task' }, task_token: 'private-task-token' }, { offset: 100001 }), error => {
    assert.match(error.message, /offset/)
    assert.match(error.message, /less_than_equal/)
    assert.match(error.message, /100000/)
    assert.doesNotMatch(error.message, /private|sensitive|dangerous|secret/)
    return true
  })
})
