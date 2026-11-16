import assert from 'node:assert/strict'
import { test } from 'node:test'

test('plugin owns versioned methods and native registry exposes frozen task skills only', async () => {
  const { loadMethods, mountTaskSkills, skillName } = await import('./methods.mjs')
  const methods = await loadMethods()
  assert.equal(methods.main.key, 'ai-delivery-review')
  assert.match(methods.main.body, /经营报告：先解释钱和回收/)
  assert.match(methods.main.body, /report.executive/)
  assert.equal(methods.references.length, 5)
  const registered = new Map(), tools = new Map()
  const ctx = { skills: { register: definition => registered.set(definition.name, definition), get: async name => registered.get(name) }, tools: { register: tool => tools.set(tool.name, tool) } }
  const usage = mountTaskSkills(ctx, methods, { renderSkillContent: s => s.content, scopeOf: () => 'task-scope' })
  assert.equal(registered.size, 6)
  assert.equal(usage.size, 0)
  const result = await tools.get('skill').execute({ name: skillName('scale') })
  assert.match(result.content, /放量/)
  assert.ok(usage.has('scale'))
  await assert.rejects(tools.get('skill').execute({ name: 'personal-secret' }))
  const frozenBody = result.content
  methods.references.find(r => r.key === 'scale').body = 'upgraded content'
  assert.equal((await tools.get('skill').execute({ name: skillName('scale') })).content, frozenBody)
})
