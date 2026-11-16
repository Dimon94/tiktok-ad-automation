/** 模型只传任务内范围引用；领域API始终收到明确账户集合并继续独立校验。 */
export function expandDefinition(definition, claim) {
  if (definition?.accounts?.mode !== 'REVIEW_ACCOUNTS') return definition
  if (Object.keys(definition.accounts).some(k => k !== 'mode')) throw new Error('REVIEW_ACCOUNTS accepts only mode; use ACCOUNTS for an explicit subset')
  const ids = claim.task.evidence.optimizer?.advertiser_ids
  if (!Array.isArray(ids) || !ids.length) throw new Error('Frozen review account scope is unavailable')
  return { ...definition, accounts: { mode: 'ACCOUNTS', advertiser_ids: [...ids] } }
}
export function expandProposals(result, claim) {
  return { ...result, proposals: (result.proposals || []).map(p => p.kind === 'rule' && p.after?.definition ? { ...p, after: { ...p.after, definition: expandDefinition(p.after.definition, claim) } } : p) }
}
export function promptEvidence(evidence) {
  const { methods: _methods, ...data } = evidence
  const { advertiser_ids: ids, ...optimizer } = data.optimizer || {}
  if (!Array.isArray(ids) || !ids.length) return data
  const scope = new Set(ids)
  const capabilityScope = data.capabilities?.scope
  const capabilityIds = capabilityScope?.advertiser_ids
  const capabilities = Array.isArray(capabilityIds) && capabilityIds.length === scope.size && capabilityIds.every(id => scope.has(id))
    ? { ...data.capabilities, scope: { bc_id: capabilityScope.bc_id, advertiser_count: ids.length, accounts: { mode: 'REVIEW_ACCOUNTS' } } }
    : data.capabilities
  return { ...data, ...(capabilities ? { capabilities } : {}), optimizer: { ...optimizer, advertiser_count: ids.length, accounts: { mode: 'REVIEW_ACCOUNTS' } },
    rules: (data.rules || []).map(r => {
      const accounts = r.definition?.accounts
      const sameScope = accounts?.mode === 'ACCOUNTS' && accounts.advertiser_ids?.length === scope.size && accounts.advertiser_ids.every(id => scope.has(id))
      return sameScope ? { ...r, definition: { ...r.definition, accounts: { mode: 'REVIEW_ACCOUNTS' } } } : r
    }),
    account_scope_contract: 'REVIEW_ACCOUNTS is a plugin-only exact reference to this frozen task account set, never ALL/BC expansion. In backtest candidate_definitions or rule proposals after.definition use accounts:{mode:"REVIEW_ACCOUNTS"}; the plugin expands exact IDs before domain validation. Use normal ACCOUNTS with explicit IDs for a subset. Account IDs needed for drilldown are returned by ranked analyses/evidence queries. Original frozen configuration is unchanged.' }
}
