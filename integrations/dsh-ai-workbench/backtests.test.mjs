import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mountBacktestTool, BACKTEST_RULE } from './backtests.mjs'

test('backtest tool uses task runtime and blocks cancelled calls', async () => {
  const tools = new Map(), controller = new AbortController(), claim = { task_token: 'task-secret' }
  const args = { evidence_id: 'frozen-id', candidate_definition: { conditions: [] } }
  let calls = 0
  mountBacktestTool({ tools: { register: t => tools.set(t.name, t) } }, claim, { backtestRule: async (identity, input) => {
    assert.equal(identity, claim); assert.deepEqual(input, args); calls++; return { id: 'archived-backtest', limitations: [] }
  } }, controller.signal)
  const tool = tools.get(BACKTEST_RULE)
  assert.equal((await tool.execute(args)).id, 'archived-backtest')
  controller.abort()
  await assert.rejects(tool.execute(args), /cancelled/)
  assert.equal(calls, 1)
})

test('daily replay permits a new policy without an existing rule id', async () => {
  const { mountDailyBacktestTool, DAILY_BACKTEST_RULE } = await import('./backtests.mjs')
  const tools = new Map(), controller = new AbortController(), claim = { task_token: 'scoped' }
  const args = { candidate_definitions: [{ accounts: { mode: 'ACCOUNTS', advertiser_ids: ['a'] } }] }
  mountDailyBacktestTool({ tools: { register: t => tools.set(t.name, t) } }, claim, {
    dailyBacktestRule: async (identity, input) => {
      assert.equal(identity, claim)
      assert.deepEqual(input, args)
      return { rule_id: null, comparison: 'new_policy_vs_observed_no_additional_pause' }
    }
  }, controller.signal)
  const tool = tools.get(DAILY_BACKTEST_RULE)
  assert.ok(!tool.parameters.required?.includes('rule_id'))
  assert.equal((await tool.execute(args)).rule_id, null)
})
