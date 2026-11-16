import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { loadMethods, skillName } from './methods.mjs'
import { createRuntime, runClaim, COMPLETE_TOOL } from './runtime.mjs'

const report = { headline: '继续观察', verdict: '本轮不调整规则', sections: [{ title: '证据覆盖', body: '成熟窗口不足', evidence_ids: [], table: { columns: ['窗口', '结论'], rows: [['D7', '证据不足']] } }], rule_decision: { status: 'INSUFFICIENT_EVIDENCE', reason: '缺成熟证据与回测' } }

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
      const agent = { followup: message => { prompt = message }, whenIdle: async () => { await assert.rejects(registered.execute({ report, summary: 'premature', proposals: [] }), /Load the main/); await tools.get('skill').execute({ name: skillName('ai-delivery-review') }); await tools.get('skill').execute({ name: skillName('scale') }); assert.equal((await tools.get('tkada_search_knowledge').execute({ query: '补贴' })).items[0].id, 'knowledge-1'); assert.equal((await tools.get('tkada_read_knowledge').execute({ knowledge_id: 'knowledge-1' })).body, 'policy text'); assert.equal((await registered.execute({ report, summary: 'uncorrected draft', proposals: [] })).status, 'verification_required'); assert.equal(completions.length, 0);
        // 即使首稿已通过，补充真实业务范围后第二次提交仍必须重新核验，不能直写结果。
        claim.task.evidence.optimizer.advertiser_ids = ['a']
        const blocked = await registered.execute({ report, summary: 'skip available work', proposals: [] })
        assert.equal(blocked.status, 'work_required')
        assert.equal(blocked.persisted, false)
        assert.equal(completions.length, 0)
        for (const maturity_horizon of ['d0', 'd3', 'd7']) await tools.get('tkada_analyze_evidence').execute({ dimension: 'account', maturity_horizon })
        await tools.get('skill').execute({ name: skillName('business') })
        await tools.get('skill').execute({ name: skillName('builds') })
        await registered.execute({ report, summary: 'insufficient evidence', proposals: [] }) } }
      await input.setup({ inject: async (names, callback) => { assert.deepEqual(names, ['tools', 'skills']); return callback({ skills: { register: s => skills.set(s.name, s), get: async name => skills.get(name) }, tools: { restrict: value => { allowed = value.allow }, guard: value => { guard = value }, register: value => { tools.set(value.name, value); if (value.name === COMPLETE_TOOL) registered = value } } }) } }, agent)
      return { agent, dispose: async () => { disposed = true } }
    } } }
    const completions = []
    await runClaim(ctx, { stateRoot: directory }, claim, { analyzeEvidence: async (_claim, request) => ({ id: 'analysis', request }), searchKnowledge: async () => ({ items: [{ id: 'knowledge-1' }] }), readKnowledge: async () => ({ id: 'knowledge-1', body: 'policy text' }), complete: async (identity, result) => { completions.push({ identity, result }); return { status: 'COMPLETE' } } }, { SessionId: value => value, createUserMessage: value => value, renderSkillContent: s => s.content, scopeOf: () => "task-scope" })
    assert.deepEqual(allowed, [])
    assert.deepEqual([...tools.keys()].sort(), ["tkada_analyze_timeline", "tkada_analyze_evidence", "tkada_write_note", "tkada_list_notes", "tkada_query_evidence", "tkada_read_evidence", "skill", "tkada_search_knowledge", "tkada_read_knowledge", "tkada_backtest_rule", "tkada_daily_backtest_rule", COMPLETE_TOOL].sort())
    assert.equal(guard({ name: 'exec' }), 'Only task-scoped evidence, analysis, notes, skills and review completion are permitted')
    assert.ok(options.meta.cwd.includes('tenant-a/optimizer-a/task-1/fence-1'))
    assert.ok(!JSON.stringify(prompt).includes('short-token'))
    assert.equal(completions.length, 1)
    assert.ok(registered.parameters.required.includes('report'))
    assert.deepEqual(completions[0].result.report, report)
    assert.deepEqual(completions[0].result.method_usage, ['ai-delivery-review', 'builds', 'business', 'scale'])
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
  let heartbeat, prompt, renewals = 0
  try {
    globalThis.setInterval = callback => { heartbeat = callback; return { fixture: true } }
    const claim = { task: { id: 'task', tenant_id: 'tenant', optimizer_id: 'optimizer', fence: 'fence', expires_at: new Date(Date.now()+30000).toISOString(), max_expires_at: new Date(Date.now()+7200000).toISOString(), evidence: { methods: await loadMethods() } }, task_token: 'old-private-token' }
    let completion
    const tools = new Map(), skills = new Map()
    const ctx = { agentPresets: { mount: async () => {} }, agents: { create: async input => {
      await input.setup({ inject: async (_names, callback) => callback({ skills: { register: s => skills.set(s.name, s), get: async name => skills.get(name) }, tools: { register: t => tools.set(t.name, t), restrict() {}, guard() {} } }) })
      return { agent: { followup: message => { prompt = message }, whenIdle: async () => {
        await heartbeat() // 短暂504不应撤销仍然有效的任务。
        await heartbeat()
        await tools.get('skill').execute({ name: skillName('ai-delivery-review') })
        await tools.get(COMPLETE_TOOL).execute({ report, summary: 'draft', proposals: [] }); await tools.get(COMPLETE_TOOL).execute({ report, summary: 'done', proposals: [] })
      } }, dispose: async () => {} }
    } } }
    await runClaim(ctx, { stateRoot: directory, modelSelection: { provider: 'fixture', model: 'fixture' } }, claim,
      { heartbeat: async () => { if (!renewals++) throw new Error('TK-ADA runtime HTTP 504; check task'); return { task_token: 'renewed-private-token', expires_at: new Date(Date.now()+300000).toISOString() } }, complete: async (identity) => { completion = identity.task_token; return { status: 'COMPLETE' } } },
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


test('native model turn failure is terminal and is not mistaken for normal idle or blindly continued', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'tkada-model-error-'))
  let wakes = 0, disposed = false
  const results = []
  const agent = { session: { snapshotEvents: () => [{ type: 'turn/end', data: { reason: { kind: 'error', error: { code: 'PI_AI_ERROR' } } } }] }, followup: () => { wakes++ }, whenIdle: async () => {} }
  const claim = { task: { id: 't', tenant_id: 'tenant', optimizer_id: 'o', fence: 'f', expires_at: new Date(Date.now()+60000).toISOString(), evidence: { methods: await loadMethods() } } }
  try {
    await assert.rejects(runClaim({ agents: { create: async () => ({ agent, dispose: async () => { disposed = true } }) } }, { stateRoot: directory, modelSelection: { provider: 'test', model: 'test' } }, claim, { complete: async (_c, result) => results.push(result) }, { SessionId: x => x, createUserMessage: x => x }), /Native Agent model turn failed/)
    assert.equal(wakes, 1)
    assert.equal(results.length, 1)
    assert.equal(results[0].failed, true)
    assert.equal(disposed, true)
  } finally { await rm(directory, { recursive: true, force: true }) }
})

test('heavy analysis calls queue without blocking light reads and failures release the queue', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'tkada-heavy-queue-'))
  try {
    const tokenFile = join(directory, 'token'); await writeFile(tokenFile, 'secret')
    const started = [], gates = []
    const runtime = createRuntime({ url: 'https://tkada.example', tokenFile, stateRoot: directory }, async (url) => {
      started.push(url)
      if (url.endsWith('/evidence/timeline') || url.endsWith('/rules/daily-backtest')) await new Promise(resolve => gates.push(resolve))
      return { ok: !url.endsWith('/evidence/timeline'), status: 500, json: async () => ({}) }
    })
    const claim = { task: { id: 't' }, task_token: 'scoped' }
    const first = runtime.analyzeTimeline(claim, {})
    const rejected = assert.rejects(first, /500/)
    const second = runtime.dailyBacktestRule(claim, {})
    await runtime.listNotes(claim, {})
    assert.equal(started.filter(s => s.endsWith('/rules/daily-backtest')).length, 0)
    gates.shift()(); await rejected
    await new Promise(resolve => setTimeout(resolve, 0))
    assert.equal(started.filter(s => s.endsWith('/rules/daily-backtest')).length, 1)
    gates.shift()(); await second
  } finally { await rm(directory, { recursive: true, force: true }) }
})

test('gateway timeout retrieves only the original cached computation, without replaying the work', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'tkada-result-poll-'))
  try {
    const tokenFile = join(directory, 'token'); await writeFile(tokenFile, 'private')
    const requests = []
    const runtime = createRuntime({ url: 'https://tkada.example', tokenFile, stateRoot: directory }, async (url, options) => {
      requests.push({ url, body: JSON.parse(options.body), token: options.headers.authorization })
      return requests.length === 1 ? { ok: false, status: 504, json: async () => ({}) } : { ok: true, json: async () => ({ id: 'original-result', variants: [] }) }
    })
    const result = await runtime.dailyBacktestRule({ task: { id: 't' }, task_token: 'scoped' }, { rule_id: 'rule' })
    assert.equal(result.id, 'original-result')
    assert.deepEqual(requests.map(r => r.body), [{ rule_id: 'rule' }, { rule_id: 'rule', cache_only: true }])
    assert.ok(requests.every(r => r.token === 'Bearer scoped'))
  } finally { await rm(directory, { recursive: true, force: true }) }
})
