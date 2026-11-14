import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { cp, mkdir, mkdtemp, readFile, rm, stat, symlink, writeFile } from 'node:fs/promises'
import { createServer, request as httpRequest } from 'node:http'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import { prepareRuntime } from './start.mjs'

const packageRoot = process.env.DSH_TEST_PACKAGE_ROOT

test('deployment selects the shipped official web profile and keeps port boundaries explicit', async () => {
  const packageJson = JSON.parse(await readFile(join(resolve(packageRoot || dirname(dirname(fileURLToPath(import.meta.url)))), 'package.json'), 'utf8'))
  assert.equal(packageJson.dependencies['@deepseek-ai/dsh'], '0.2.2-rc.1')
  const start = await readFile(join(dirname(fileURLToPath(import.meta.url)), 'start.mjs'), 'utf8')
  assert.match(start, /@deepseek-ai\/dsh-web-app/)
  assert.match(start, /port: 9082/)
  const proxy = await readFile(join(dirname(fileURLToPath(import.meta.url)), 'admin-proxy.mjs'), 'utf8')
  assert.match(proxy, /127\.0\.0\.1:9082/)
  assert.match(proxy, /listen\(9081/)
})

test('migrates old base preset inserts before loading the official web profile', async () => {
  const root = await mkdtemp(join(tmpdir(), 'tkada-dsh-profile-migration-'))
  const providerFile = join(root, 'provider.json')
  const tokenFile = join(root, 'token')
  const profile = join(root, 'home', 'profiles', 'tkada')
  try {
    process.env.DSH_MIGRATION_MODEL_KEY = 'test-only-model-key'
    await mkdir(profile, { recursive: true })
    const credentials = join(root, 'home', '.credentials.yaml')
    await writeFile(credentials, 'test-only-credential-content', { mode: 0o660 })
    await writeFile(providerFile, JSON.stringify({ provider: 'openai', model: 'gpt-4o-mini', config: { apiKeyEnv: 'DSH_MIGRATION_MODEL_KEY' } }))
    await writeFile(tokenFile, 'migration-token-' + 'x'.repeat(40))
    await writeFile(join(profile, 'package.json'), JSON.stringify({ name: 'dsh-profile-tkada', dependencies: { 'example-admin-plugin': '1.0.0' }, dsh: { profile: { bundles: ['example-admin-bundle'] } } }))
    await writeFile(join(profile, 'cordis.patch.yml'), `- insert:\n    - id: agent-preset-registry\n      name: '@deepseek-ai/dsh-agent-preset-registry'\n    - id: preset-standard\n      name: '@deepseek-ai/dsh-agent-preset'\n    - id: keep-admin-setting\n      config:\n        enabled: true\n`)
    await prepareRuntime({ home: join(root, 'home'), workspace: join(root, 'workspace'), providerFile, tokenFile, apiUrl: 'http://127.0.0.1:39999' })
    const manifest = JSON.parse(await readFile(join(profile, 'package.json'), 'utf8'))
    assert.equal(manifest.dependencies['example-admin-plugin'], '1.0.0')
    assert.deepEqual(manifest.dsh.profile.bundles, ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app', 'example-admin-bundle'])
    assert.equal((await stat(credentials)).mode & 0o777, 0o600)
    assert.equal(await readFile(credentials, 'utf8'), 'test-only-credential-content')
    const patch = await readFile(join(profile, 'cordis.patch.yml'), 'utf8')
    assert.doesNotMatch(patch, /agent-preset-registry|preset-standard/)
    assert.match(patch, /keep-admin-setting/)
    assert.match(patch, /id: webserver[\s\S]*port: 9082/)
    assert.match(patch, /id: web-runtime/)
  } finally {
    delete process.env.DSH_MIGRATION_MODEL_KEY
    await rm(root, { recursive: true, force: true })
  }
})

test('production entrypoint boots real DSH, polls TK-ADA and serves readiness without model calls', { skip: !packageRoot, timeout: 45000 }, async () => {
  const root = await mkdtemp(join(tmpdir(), 'tkada-dsh-production-'))
  const requests = []
  const api = createServer(async (request, response) => {
    let body = ''
    for await (const chunk of request) body += chunk
    requests.push({ path: request.url, authorization: request.headers.authorization, body: JSON.parse(body || '{}') })
    response.writeHead(200, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ task: null }))
  })
  let child, output = ''
  try {
    api.listen(0, '127.0.0.1')
    await once(api, 'listening')
    const portReservation = createServer()
    portReservation.listen(0, '127.0.0.1')
    await once(portReservation, 'listening')
    const healthPort = portReservation.address().port
    await new Promise(resolve => portReservation.close(resolve))
    await cp(dirname(dirname(fileURLToPath(import.meta.url))), join(root, 'plugin'), { recursive: true })
    await symlink(join(resolve(packageRoot), 'node_modules'), join(root, 'node_modules'), 'dir')
    const token = 'test-only-private-service-credential'
    await writeFile(join(root, 'token'), token, { mode: 0o600 })
    await writeFile(join(root, 'provider.json'), JSON.stringify({ provider: 'openai', model: 'gpt-4o-mini', config: { apiKeyEnv: 'DSH_SMOKE_MODEL_KEY', baseURL: `http://127.0.0.1:${api.address().port}/model` } }))
    const require = createRequire(join(resolve(packageRoot), 'package.json'))
    // 全新 HOME 和最小环境，避免读取开发者的个人模型密钥或 DSH 会话。
    child = spawn(process.execPath, [join(root, 'plugin/deployment/start.mjs')], { cwd: root, env: {
      PATH: process.env.PATH, HOME: root, DSH_HOME: join(root, 'home'), DSH_WORKSPACE: join(root, 'workspace'),
      DSH_PROVIDER_FILE: join(root, 'provider.json'), AI_DSH_TOKEN_FILE: join(root, 'token'),
      TKADA_API_URL: `http://127.0.0.1:${api.address().port}`, PORT: String(healthPort),
      DSH_CLI: require.resolve('@deepseek-ai/dsh/lib/bin.js'), DSH_SMOKE_MODEL_KEY: 'test-only-unused-model-key', DSH_TELEMETRY_DISABLED: '1',
    }, stdio: ['ignore', 'pipe', 'pipe'], detached: true })
    child.stdout.on('data', chunk => { output += chunk })
    child.stderr.on('data', chunk => { output += chunk })
    let ready = false
    for (let attempt = 0; attempt < 150; attempt++) {
      if (child.exitCode !== null) break
      try {
        const response = await fetch(`http://127.0.0.1:${healthPort}/ready`)
        ready = response.ok && (await response.json()).ready === true
      } catch {}
      if (ready && requests.length) break
      await delay(100)
    }
    assert.ok(ready, `DSH readiness failed:\n${output}`)
    assert.ok(requests.length, `DSH did not poll TK-ADA:\n${output}`)
    assert.ok(requests.every(request => request.path === '/api/integrations/ai-workbench/reviews/claim'))
    assert.equal(requests[0].authorization, `Bearer ${token}`)
    assert.equal(requests[0].body.main.key, 'ai-delivery-review')
    // 真实浏览器面：publicUrl 只负责生成上下文 URL，/dsh 前缀由管理代理剥离。
    const webOrigin = `http://127.0.0.1:${api.address().port}`
    let page
    for (let attempt = 0; attempt < 100 && child.exitCode === null; attempt++) {
      try {
        page = await fetch('http://127.0.0.1:9081/dsh/', { headers: { origin: webOrigin } })
        if (page.ok) break
      } catch {}
      await delay(100)
    }
    assert.ok(page?.ok, `WebUI did not become ready:\n${output}`)
    const html = await page.text()
    assert.equal(page.status, 200, `WebUI root response ${page.status}: ${html}`)
    assert.match(html, /<script/)
    const assetPath = html.match(/src="([^"?]*\/assets\/[^"?]+\.js)"/)?.[1]
    assert.ok(assetPath, `official WebUI bootstrap asset missing: ${html.slice(0, 500)}`)
    const assetUrl = new URL(assetPath, 'http://127.0.0.1:9081/dsh/')
    const asset = await fetch(assetUrl, { headers: { origin: webOrigin } })
    assert.equal(asset.status, 200)
    assert.match(asset.headers.get('content-type') || '', /javascript/)
    // DSH API_PATH=/api 是 HTTP RPC 前缀；实时 WebSocket 使用官方
    // REMOTE_STREAM_MUX_PATH=/api/remote.mux。
    const initialize = await fetch('http://127.0.0.1:9081/dsh/api/workspace/initializeDefault', {
      method: 'POST', headers: { origin: webOrigin, 'content-type': 'application/json' },
      body: JSON.stringify({ type: 'client-request', rpcId: crypto.randomUUID(), method: 'workspace/initializeDefault', payload: { args: {} } }),
    })
    assert.equal(initialize.status, 200)
    const initialized = await initialize.text()
    assert.ok(initialized.includes(join(root, 'workspace', 'deepseek-harness', 'default-workspace')), `default workspace did not initialize: ${initialized}`)
    // 页面和工作区可用不代表 Agent 能创建：真实装配 standard，覆盖其插件依赖。
    const createSession = await fetch('http://127.0.0.1:9081/dsh/api/session/create', {
      method: 'POST', headers: { origin: webOrigin, 'content-type': 'application/json' },
      body: JSON.stringify({ type: 'client-request', rpcId: crypto.randomUUID(), method: 'session/create', payload: { args: { request: { cwd: join(root, 'workspace'), agentPreset: 'standard' } } } }),
    })
    assert.equal(createSession.status, 200)
    const created = await createSession.text()
    assert.match(created, /"sessionId":"session-/i, `standard Agent did not create: ${created}`)
    assert.doesNotMatch(created, /agent-preset\/invalid|waiting for pluginManager/)
    const wsRequest = httpRequest('http://127.0.0.1:9081/dsh/api/remote.mux', {
      headers: { origin: webOrigin, upgrade: 'websocket', connection: 'Upgrade', 'sec-websocket-key': 'MTIzNDU2Nzg5MGFiY2RlZg==', 'sec-websocket-version': '13' },
    })
    const wsResult = await new Promise((resolve, reject) => {
      wsRequest.once('upgrade', response => resolve({ status: response.statusCode, socket: response.socket }))
      wsRequest.once('response', response => resolve({ status: response.statusCode }))
      wsRequest.once('error', reject)
      wsRequest.end()
    })
    assert.equal(wsResult.status, 101, `official WebUI WebSocket was not upgraded: ${wsResult.status}`)
    wsResult.socket.destroy()
    const patch = await readFile(join(root, 'home/profiles/tkada/cordis.patch.yml'), 'utf8')
    assert.ok(!patch.includes('test-only-unused-model-key'))
    assert.equal((await fetch(`http://127.0.0.1:${healthPort}/sessions`)).status, 404)
    child.kill('SIGTERM')
    const stopped = await Promise.race([once(child, 'exit').then(([code]) => code), delay(10000, undefined, { ref: false }).then(() => 'timeout')])
    assert.equal(stopped, 0, `DSH failed graceful shutdown:\n${output}`)
  } finally {
    if (child && child.exitCode === null && child.signalCode === null) { process.kill(-child.pid, 'SIGKILL'); await once(child, 'exit') }
    api.closeAllConnections()
    await new Promise(resolve => api.close(resolve))
    await rm(root, { recursive: true, force: true })
  }
})

// 必须经过真实 Agent 工厂与 standard 预设；服务级 scope 替身无法发现会话缺少 inject。
test('claimed review mounts task skills in a real Agent and completes through model transport', { skip: !packageRoot, timeout: 45000 }, async () => {
  const root = await mkdtemp(join(tmpdir(), 'tkada-dsh-claimed-'))
  const { loadMethods } = await import('../methods.mjs')
  const methods = await loadMethods()
  let claimed = false, modelCalls = 0, completion, child, output = ''
  const api = createServer(async (request, response) => {
    let raw = ''
    for await (const chunk of request) raw += chunk
    if (request.url.startsWith('/model/')) {
      modelCalls++
      // 只替换模型 HTTP 运输层，真实宿主解析流、执行 Skill 和结果工具。
      const call = modelCalls === 1
        ? { name: 'skill', arguments: JSON.stringify({ name: 'ai-delivery-review' }) }
        : (modelCalls === 3 || modelCalls === 4) ? { name: 'tkada_complete_review', arguments: JSON.stringify({ summary: 'fixture review complete', proposals: [] }) } : null
      response.writeHead(200, { 'content-type': 'text/event-stream' })
      // 第二轮故意只返回文本，覆盖同会话提醒后补交结构化结果。
      const delta = call ? { role: 'assistant', tool_calls: [{ index: 0, id: `call-${modelCalls}`, type: 'function', function: call }] } : { role: 'assistant', content: 'Complete.' }
      const chunk = (delta, finish_reason) => ({ id: `fixture-${modelCalls}`, object: 'chat.completion.chunk', created: 1, model: 'deepseek-chat', choices: [{ index: 0, delta, finish_reason }] })
      response.write(`data: ${JSON.stringify(chunk(delta, null))}\n\n`)
      response.write(`data: ${JSON.stringify(chunk({}, call ? 'tool_calls' : 'stop'))}\n\n`)
      response.end('data: [DONE]\n\n')
      return
    }
    response.writeHead(200, { 'content-type': 'application/json' })
    if (request.url.endsWith('/claim')) {
      if (claimed) return response.end('{"task":null}')
      claimed = true
      return response.end(JSON.stringify({ task_token: 'test-task-token', task: { id: 'task', tenant_id: 'tenant', optimizer_id: 'optimizer', fence: 'fence', expires_at: new Date(Date.now() + 30000).toISOString(), evidence: { methods } } }))
    }
    if (request.url.endsWith('/complete')) completion = JSON.parse(raw)
    response.end('{"status":"COMPLETE"}')
  })
  try {
    api.listen(0, '127.0.0.1')
    await once(api, 'listening')
    const health = createServer().listen(0, '127.0.0.1')
    await once(health, 'listening')
    const port = health.address().port
    await new Promise(resolve => health.close(resolve))
    await cp(dirname(dirname(fileURLToPath(import.meta.url))), join(root, 'plugin'), { recursive: true })
    await symlink(join(resolve(packageRoot), 'node_modules'), join(root, 'node_modules'), 'dir')
    await writeFile(join(root, 'token'), 'test-private-service-token-' + 'x'.repeat(40))
    await writeFile(join(root, 'provider.json'), JSON.stringify({ provider: 'fixture', model: 'fixture', config: { api: 'openai-completions', models: [{ id: 'fixture', reasoningEfforts: false }], apiKeyEnv: 'DSH_CLAIM_TEST_KEY', baseURL: `http://127.0.0.1:${api.address().port}/model` } }))
    const require = createRequire(join(resolve(packageRoot), 'package.json'))
    child = spawn(process.execPath, [join(root, 'plugin/deployment/start.mjs')], { cwd: root, detached: true, stdio: ['ignore', 'pipe', 'pipe'], env: {
      PATH: process.env.PATH, HOME: root, DSH_HOME: join(root, 'home'), DSH_WORKSPACE: join(root, 'workspace'),
      DSH_PROVIDER_FILE: join(root, 'provider.json'), AI_DSH_TOKEN_FILE: join(root, 'token'), TKADA_API_URL: `http://127.0.0.1:${api.address().port}`, PORT: String(port),
      DSH_CLI: require.resolve('@deepseek-ai/dsh/lib/bin.js'), DSH_CLAIM_TEST_KEY: 'fixture', DSH_TELEMETRY_DISABLED: '1',
    } })
    child.stdout.on('data', chunk => { output += chunk })
    child.stderr.on('data', chunk => { output += chunk })
    for (let attempt = 0; attempt < 250 && !completion && child.exitCode === null; attempt++) await delay(100)
    await delay(100)
    assert.ok(modelCalls >= 4, `Claimed task did not reach real model/tools (claimed=${claimed}, calls=${modelCalls}, exit=${child.exitCode}): ${output}`)
    assert.equal(completion?.summary, 'fixture review complete', output)
    assert.deepEqual(completion.method_usage, ['ai-delivery-review'])
    assert.notEqual(completion.failed, true)
  } finally {
    if (child?.exitCode === null) { try { process.kill(-child.pid, 'SIGTERM') } catch {} }
    await delay(500)
    if (child?.exitCode === null) { try { process.kill(-child.pid, 'SIGKILL') } catch {} }
    api.closeAllConnections()
    await new Promise(resolve => api.close(resolve))
    await rm(root, { recursive: true, force: true })
  }
})
