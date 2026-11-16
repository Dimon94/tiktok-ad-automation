/** 插件拥有方法资产；宿主拥有注册服务；TK-ADA 只归档每任务实际使用的副本。 */
import { readFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'

export const METHOD_VERSION = '1.8.0'
export const PROVIDER = 'tkada-review'
export const SKILL_TOOL = 'skill'
export const MAIN_KEY = 'ai-delivery-review'
const references = {
  builds: '核对排期、测新队列、搭建执行与对象映射。',
  rules: '核对规则版本、命中、动作、回读和控损效果。',
  materials: '分析素材覆盖、制作策略与投放表现。',
  business: '区分平台 ROAS、经营收益及结算资格。',
  scale: '分析放量扩量机会、供给与风险；不执行复制新增。',
}
export const skillName = key => key === MAIN_KEY ? MAIN_KEY : `ai-delivery-review-${key}`
export const hashBody = body => createHash('sha256').update(body).digest('hex')

export async function loadMethods() {
  async function resource(key, path, description) {
    const body = await readFile(new URL(`./skills/ai-delivery-review/${path}`, import.meta.url), 'utf8')
    return { key, description, version: METHOD_VERSION, sha256: hashBody(body), body }
  }
  return { main: await resource(MAIN_KEY, 'SKILL.md', '统一检查目标、执行兑现、证据资格、历史建议与下一轮验证。'),
    references: await Promise.all(Object.entries(references).map(([key, description]) => resource(key, `references/${key}.md`, description))) }
}

export function mountTaskSkills(agentCtx, methods, { renderSkillContent, scopeOf }) {
  const resources = [methods.main, ...methods.references].map(r => ({ ...r }))
  const permitted = new Map(resources.map(r => [skillName(r.key), r]))
  const used = new Set()
  for (const resource of resources) {
    if (resource.sha256 !== hashBody(resource.body)) throw new Error('Frozen method digest mismatch')
    agentCtx.skills.register({ name: skillName(resource.key), description: resource.description,
      content: resource.body, source: 'bundled', provider: PROVIDER,
      invocation: { modelInvocable: true, userInvocable: true },
      resourceBase: { kind: 'opaque', description: 'Load reference methods by their exact catalog skill name; filesystem access is not available.' } })
  }
  // 影射宿主通用 skill 工具，防止读取宿主个人 Skill；仍通过原生 registry 加载本会话资产。
  agentCtx.tools.register({ name: SKILL_TOOL, description: 'Load one frozen TK-ADA review skill from this task catalog.',
    parameters: { type: 'object', properties: { name: { type: 'string', enum: [...permitted.keys()] } }, required: ['name'], additionalProperties: false },
    output: { schema: { type: 'object', additionalProperties: true }, render: (_args, skill) => [{ type: 'text', text: renderSkillContent(skill) }] },
    async execute({ name }) {
      const resource = permitted.get(name)
      if (!resource) throw new Error('Skill is outside this task catalog')
      const skill = await agentCtx.skills.get(name, { scope: scopeOf(agentCtx) })
      if (!skill || skill.provider !== PROVIDER || hashBody(skill.content) !== resource.sha256) throw new Error('Task skill registration mismatch')
      used.add(resource.key)
      return skill
    },
  })
  return used
}
