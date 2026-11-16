import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import { loadMethods, mountTaskSkills, skillName, hashBody } from './methods.mjs'

// 指向已有 DSH 宿主的 package.json 所在目录；不安装第二套生产运行时。
const root = process.env.DSH_TEST_PACKAGE_ROOT
const report = { executive: { assessment: '当前收入观察不足，不能判断低回收原因', drivers: [{ finding: '缺少可用收入', impact: '部分账户没有成熟收入，不能补零', action: '补齐事实后复查', evidence_ids: ['11111111-1111-4111-8111-111111111111'] }], next_review: '补齐后复查消耗和回收' }, headline: '继续观察', verdict: '本轮不调整规则', sections: [{ title: '证据覆盖', body: '成熟窗口不足', evidence_ids: [], table: { columns: ['窗口', '结论'], rows: [['D7', '证据不足']] } }], rule_decision: { status: 'INSUFFICIENT_EVIDENCE', reason: '缺成熟证据与回测' } }

test('real DSH registry scopes task methods and renders on-demand skill content', { skip: !root }, async () => {
  const require = createRequire(`${root}/package.json`)
  const { Context } = await import(require.resolve('@deepseek-ai/cordis'))
  const { default: SkillRegistry, renderSkillContent } = await import(require.resolve('@deepseek-ai/dsh-skill'))
  const { createScope, scopeOf } = await import(require.resolve('@deepseek-ai/dsh-scope'))
  const host = new Context()
  const registry = await host.plugin(SkillRegistry)
  const consumer = await host.inject(['skills'], async ctx => {
    ctx.skills.register({ name: 'private-host-skill', description: 'unrelated host content', content: 'not task data', source: 'runtime' })
    const first = createScope(ctx, {}), second = createScope(ctx, {})
    try {
      const methods = await loadMethods(), tools = new Map()
      const taskContext = first.ctx.extend({ tools: { register: t => tools.set(t.name, t) } })
      mountTaskSkills(taskContext, methods, { renderSkillContent, scopeOf })
      const newer = await loadMethods()
      newer.main.body += '\nsecond task version'
      newer.main.sha256 = hashBody(newer.main.body)
      mountTaskSkills(second.ctx.extend({ tools: { register() {} } }), newer, { renderSkillContent, scopeOf })
      const main = await tools.get('skill').execute({ name: skillName(methods.main.key) })
      assert.equal(main.content, methods.main.body)
      assert.match(renderSkillContent(main), /skill_content/)
      assert.notEqual((await second.ctx.skills.get(skillName(methods.main.key), { scope: scopeOf(second.ctx) })).content, main.content)
      await assert.rejects(tools.get('skill').execute({ name: 'private-host-skill' }))
      assert.equal(await ctx.skills.get(skillName(methods.main.key)), undefined)
    } finally { await first.dispose(); await second.dispose() }
  })
  await consumer.dispose(); await registry.dispose()
})

// 必须使用真实 ToolsService 走 runClaim setup，不能再用 Map 替身掩盖 restrict 合同。
test('real DSH tool registry accepts task session setup and hides inherited tools', { skip: !root }, async () => {
  const require = createRequire(`${root}/package.json`)
  const { Context } = await import(require.resolve('@deepseek-ai/cordis'))
  const { default: SystemPrompt } = await import(require.resolve('@deepseek-ai/dsh-system-prompt'))
  const { default: Tools } = await import(require.resolve('@deepseek-ai/dsh-tools'))
  const { default: Skills, renderSkillContent } = await import(require.resolve('@deepseek-ai/dsh-skill'))
  const { createScope, scopeOf } = await import(require.resolve('@deepseek-ai/dsh-scope'))
  const { runClaim } = await import('./runtime.mjs')
  const { mkdtemp, rm } = await import('node:fs/promises')
  const { tmpdir } = await import('node:os')
  const host = new Context()
  const prompt = await host.plugin(SystemPrompt), tools = await host.plugin(Tools), registry = await host.plugin(Skills)
  const directory = await mkdtemp(`${tmpdir()}/aiwb-native-session-`)
  try {
    const consumer = await host.inject(['tools', 'skills'], async ctx => {
      ctx.tools.register({ name: 'exec', parameters: { type: 'object' }, output: { schema: { type: 'object' }, render: () => [] }, execute: async () => ({}) })
      const scope = createScope(ctx, {})
      let completed = 0
      const runtime = { queryEvidence: async (_claim, args) => ({ items: [{ id: 'frozen', category: args.category }] }), readEvidence: async (_claim, args) => ({ id: 'page', source_id: args.evidence_id, rows: [] }), complete: async () => { completed++; return { status: 'COMPLETE' } } }
      const native = { agentPresets: { mount: async () => {} }, agents: { create: async options => {
        await options.setup(scope.ctx, {})
        const key = scopeOf(scope.ctx)
        assert.equal(scope.ctx.tools.get('exec', key), undefined)
        return { agent: { followup() {}, whenIdle: async () => {
          await scope.ctx.tools.get('skill', key).execute({ name: 'ai-delivery-review' })
          const index = await scope.ctx.tools.get('tkada_query_evidence', key).execute({ category: 'reporting' })
          assert.equal(index.items[0].id, 'frozen')
          const page = await scope.ctx.tools.get('tkada_read_evidence', key).execute({ evidence_id: index.items[0].id })
          assert.equal(page.id, 'page')
          await scope.ctx.tools.get('tkada_complete_review', key).execute({ report, summary: 'native setup works', proposals: [] })
        } }, dispose: () => scope.dispose() }
      } } }
      try {
        await runClaim(native, { stateRoot: directory, modelSelection: { provider: 'fixture', model: 'fixture' } },
          { task: { id: 'task', tenant_id: 'tenant', optimizer_id: 'optimizer', fence: 'fence', expires_at: new Date(Date.now()+60000).toISOString(), evidence: { methods: await loadMethods() } } },
          runtime, { SessionId: x=>x, createUserMessage: x=>x, scopeOf, renderSkillContent })
        assert.equal(completed, 1)
      } finally { await scope.dispose() }
    })
    await consumer.dispose()
  } finally { await registry.dispose(); await tools.dispose(); await prompt.dispose(); await rm(directory, { recursive: true, force: true }) }
})
