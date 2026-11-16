import { createServer } from 'node:http'
import { inject as runtimeInject, apply as applyRuntime } from '../index.mjs'
export const inject = runtimeInject
export function apply(ctx, config) {
  let connected = false
  applyRuntime(ctx, { ...config, onConnection: value => { connected = value } })
  // 健康端口只有集群内部探针使用；没有公网会话或管理入口。
  ctx.effect(() => {
    const server = createServer((request, response) => {
      if (!['/health', '/ready'].includes(request.url)) { response.writeHead(404); response.end(); return }
      const ready = request.url === '/health' || connected
      response.writeHead(ready ? 200 : 503, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ ready, connected, plugin: '1.8.2' }))
    }).listen(config.healthPort || 9080, '0.0.0.0')
    return () => new Promise(resolve => server.close(resolve))
  })
}
