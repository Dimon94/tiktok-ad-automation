import { test } from 'node:test'
import assert from 'node:assert/strict'
import { modelFailure, publicFailure, failureError } from './failures.mjs'

test('model errors distinguish rejection, capacity, quota, authentication and unknown failures without leaking text', () => {
  const cases = [
    ['PI_AI_ERROR', 'The selected model cannot continue this conversation.', 'MODEL_SESSION_REJECTED', true],
    ['PI_AI_ERROR', 'The model refused to complete the request', 'MODEL_ERROR', false],
    ['CONTEXT_WINDOW_EXCEEDED', 'secret', 'MODEL_CONTEXT_LIMIT', true],
    ['AUTH', 'secret', 'MODEL_AUTH', false],
    ['QUOTA_EXCEEDED', 'secret', 'MODEL_QUOTA', false],
    ['RATE_LIMIT', 'secret', 'MODEL_RATE_LIMIT', false],
    ['INVALID_REQUEST', 'secret', 'MODEL_INVALID_REQUEST', false],
    ['TRANSPORT', 'secret', 'MODEL_TRANSPORT', true],
    ['TIMEOUT', 'secret', 'MODEL_TIMEOUT', true],
    ['SERVER', 'secret', 'MODEL_SERVER', true],
    ['EMPTY_RESPONSE', 'secret', 'MODEL_EMPTY_RESPONSE', true],
    ['STREAM_CLOSED', 'secret', 'MODEL_STREAM_CLOSED', true],
  ]
  for (const [code, message, category, recoverable] of cases) {
    const failure = modelFailure({ kind: 'error', error: { code, message } })
    assert.equal(failure.category, category)
    assert.equal(failure.recoverable, recoverable)
    assert.doesNotMatch(publicFailure(failureError(failure, 2)), /secret/)
  }
  assert.equal(modelFailure({ kind: 'aborted' }), null)
  assert.equal(modelFailure({ kind: 'stop' }), null)
  assert.doesNotMatch(publicFailure(new Error('secret')), /secret/)
})
