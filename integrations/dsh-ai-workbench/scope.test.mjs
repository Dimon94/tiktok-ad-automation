import { test } from 'node:test'
import assert from 'node:assert/strict'
import { promptEvidence, expandDefinition, expandProposals } from './scope.mjs'

test('scope reference compresses only exact frozen account sets and expands without mutation or widening', () => {
  const ids = Array.from({ length: 1344 }, (_, i) => `account-${i}`)
  const evidence = { optimizer: { advertiser_ids: ids, bc_id: 'bc' }, capabilities: { scope: { bc_id: 'bc', advertiser_ids: ids }, publication: 'MANUAL' }, methods: { secret: 'not in prompt' }, rules: [{ definition: { accounts: { mode: 'ACCOUNTS', advertiser_ids: [...ids].reverse() } } }, { definition: { accounts: { mode: 'ACCOUNTS', advertiser_ids: ['other'] } } }] }
  const claim = { task: { evidence } }
  const prompt = promptEvidence(evidence)
  assert.equal(prompt.methods, undefined)
  assert.equal(prompt.optimizer.advertiser_count, 1344)
  assert.equal(prompt.capabilities.scope.advertiser_count, 1344)
  assert.equal(prompt.capabilities.publication, 'MANUAL')
  assert.deepEqual(prompt.rules[0].definition.accounts, { mode: 'REVIEW_ACCOUNTS' })
  assert.deepEqual(prompt.rules[1].definition.accounts.advertiser_ids, ['other'])
  assert.ok(JSON.stringify(prompt).length < JSON.stringify(evidence).length / 5)
  const definition = { accounts: { mode: 'REVIEW_ACCOUNTS' }, action: { type: 'PAUSE' } }
  assert.deepEqual(expandDefinition(definition, claim).accounts.advertiser_ids, ids)
  assert.equal(definition.accounts.mode, 'REVIEW_ACCOUNTS')
  assert.equal(evidence.optimizer.advertiser_ids, ids)
  assert.throws(() => expandDefinition({ accounts: { mode: 'REVIEW_ACCOUNTS', advertiser_ids: ['outside'] } }, claim), /only mode/)
  assert.throws(() => expandDefinition(definition, { task: { evidence: {} } }), /unavailable/)
  const subset = { accounts: { mode: 'ACCOUNTS', advertiser_ids: ['account-1'] } }
  assert.equal(expandDefinition(subset, claim), subset)
  const result = { proposals: [{ kind: 'rule', after: { enabled: false, definition } }, { kind: 'strategy', after: { config: {} } }] }
  const expanded = expandProposals(result, claim)
  assert.deepEqual(expanded.proposals[0].after.definition.accounts.advertiser_ids, ids)
  assert.equal(expanded.proposals[0].after.enabled, false)
  assert.equal(result.proposals[0].after.definition.accounts.mode, 'REVIEW_ACCOUNTS')
  assert.equal(expanded.proposals[1], result.proposals[1])
})
