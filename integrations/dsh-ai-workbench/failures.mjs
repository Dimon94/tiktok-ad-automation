/** 只发布固定错误分类，模型/代理原文可能含认证头或业务正文，不能直接进页面与日志。 */
export function modelFailure(reason) {
  if (reason?.kind !== 'error') return null
  const error = reason.error || reason.failure || {}
  const code = error.code || ''
  const message = typeof error.message === 'string' ? error.message : ''
  let category = 'MODEL_ERROR', description = '模型请求失败，无法确认具体原因', recoverable = false
  if (code === 'AUTH') { category = 'MODEL_AUTH'; description = '模型服务认证失败，请管理员检查授权' }
  else if (code === 'QUOTA_EXCEEDED') { category = 'MODEL_QUOTA'; description = '模型服务配额不足，请管理员检查额度' }
  else if (code === 'RATE_LIMIT') { category = 'MODEL_RATE_LIMIT'; description = '模型服务限流，请稍后重试' }
  else if (code === 'CONTEXT_WINDOW_EXCEEDED') { category = 'MODEL_CONTEXT_LIMIT'; description = '模型上下文达到服务限制'; recoverable = true }
  else if (code === 'PI_AI_ERROR' && /selected model cannot continue this conversation/i.test(message)) {
    category = 'MODEL_SESSION_REJECTED'; description = '模型服务拒绝继续当前会话，具体上游原因未提供'; recoverable = true
  } else if (['TRANSPORT', 'STREAM_CLOSED', 'TIMEOUT', 'SERVER', 'EMPTY_RESPONSE'].includes(code)) {
    category = 'MODEL_' + code; description = '模型服务连接中断、超时或未返回有效结果'; recoverable = true
  } else if (code === 'INVALID_REQUEST') { category = 'MODEL_INVALID_REQUEST'; description = '模型服务拒绝请求参数，请管理员检查适配配置' }
  return { category, description, recoverable }
}

export function failureError(failure, recoveries) {
  const error = new Error(`Native Agent model turn failed: ${failure.category}`)
  error.reviewFailure = { ...failure, recoveries }
  return error
}

export function publicFailure(error) {
  const failure = error?.reviewFailure
  return failure
    ? `[${failure.category}] ${failure.description}。已尝试 ${failure.recoveries} 次新会话接续；冻结证据和分析笔记已保留。`
    : '[REVIEW_EXECUTION_ERROR] 复盘会话执行失败，请管理员检查任务装配或工具运行；冻结证据和分析笔记已保留。'
}
