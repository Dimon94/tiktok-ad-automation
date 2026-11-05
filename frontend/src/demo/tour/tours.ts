import { SAMPLE_ACCOUNTS, SAMPLE_DRAMAS } from "../shell/samples"
import { fillField } from "./actions"
import {
  button,
  byText,
  card,
  dialogClose,
  dialogTarget,
  fieldInput,
  inDialog,
  inRow,
  mainHas,
  navItem,
  tableRow,
} from "./find"
import type { Tour, TourChapter, TourContext, TourStep } from "./types"

// 体验版引导内容：按日常工作顺序分成 7 章，每一步都对应页面上一个具体按钮或区域，
// 需要操作的地方请体验者亲手点击，点中后自动进入下一步。可以从目录单独演示任意一章。
// 定位都用页面上可见的文字，生产页面改文案时需要同步这里（端到端测试会发现）。
const at =
  (path = "") =>
  (c: TourContext) =>
    `/tenants/${c.tenantId}${path}?bc_id=${c.bcId}`
const tab = (label: string) => byText('[role="tab"]', label, { exact: true })
const sideButton = (label: string) =>
  byText("[data-sidebar] a, [data-sidebar] button", label, { exact: true })

/** 页面上可见的表格里出现了某段文字（不看筛选项、隐藏页签等内容）。 */
const tableHas = (text: string | RegExp) => () => {
  const content = Array.from(document.querySelectorAll("main tbody"))
    .filter((el) => (el as HTMLElement).offsetParent !== null)
    .map((el) => el.textContent ?? "")
    .join(" ")
  return typeof text === "string" ? content.includes(text) : text.test(content)
}
const hasValue = (find: () => Element | null) => () =>
  !!(find() as HTMLInputElement | null)?.value?.trim()

const click = (
  title: string,
  cta: string,
  body: string,
  target: TourStep["target"],
  extra: Partial<TourStep> = {},
): TourStep => ({ title, cta, body, target, advance: "click", ...extra })

const look = (
  title: string,
  body: string,
  target?: TourStep["target"],
  extra: Partial<TourStep> = {},
): TourStep => ({ title, body, target, ...extra })

const accounts: TourChapter = {
  id: "accounts",
  title: "账户与授权",
  summary: "TikTok 授权、BC 与广告账户同步",
  steps: [
    look(
      "当前租户与 BC",
      "所有操作都在当前租户和 BC（商务中心）范围内进行。顶部可以随时切换租户和 BC，各 BC 的账户、素材、广告互相隔离。",
      byText(
        "header button, [data-slot=sidebar-inset] button",
        "星河短剧 BC-A",
      ),
      { path: at("/accounts") },
    ),
    look(
      "已同步的广告账户",
      "TikTok 授权后，BC 下的广告账户自动同步到这里。每个账户都标明能否用于搭建广告、能否上传素材，以及最近一次核验时间。",
      tableRow(0),
      { path: at("/accounts") },
    ),
    click(
      "授权管理",
      "授权管理",
      "接下来看看授权连接：官方 API 和官方 MCP 两种通道，一次授权可以接入多个 BC。",
      tab("授权管理"),
    ),
    look(
      "授权连接列表",
      "每条连接显示授权状态、关联的 BC 和最近一次发现账户的时间。当前 BC 默认使用哪条连接，在上方“使用授权”里设置。",
      tableRow(0),
    ),
    click(
      "重新授权",
      "重新授权",
      "授权过期或要扩大权限时点“重新授权”，演示一下完整的授权流程。",
      inRow("button", 0, "重新授权"),
      { settleMs: 800 },
    ),
    click(
      "前往 TikTok 授权",
      "前往 TikTok 授权",
      "正式环境会跳到 TikTok 官方页面登录确认；体验版会模拟授权完成并直接返回。",
      inDialog("button", "前往 TikTok 授权"),
      { settleMs: 2000 },
    ),
    look(
      "授权完成",
      "返回后系统自动发现账户，发现成功后新授权才正式生效，取消或失败不会影响原来可用的连接。",
      tableRow(0),
    ),
  ],
}

const providers: TourChapter = {
  id: "providers",
  title: "版权方与推广链接",
  summary: "接入版权方后台，自动取推广链接",
  steps: [
    click(
      "打开版权方连接",
      "版权方连接",
      "版权方（网眼、嘉书、刚刚好、兑吧等）的分销后台接入后，搭建时系统会自动去取每部剧的推广链接，不用手工复制。",
      navItem("版权方连接"),
    ),
    look(
      "已接入的版权方",
      "每个连接对应一个版权方后台账号，系统定期验证；会话过期时取链会自动恢复。",
      tableRow(0),
      { path: at("/providers") },
    ),
    click(
      "查看推广应用",
      "查看已发现应用",
      "看看这个版权方下有哪些推广应用（小程序）。",
      inRow("button", 0, "查看已发现应用"),
      { settleMs: 800 },
    ),
    look(
      "推广应用与取链默认值",
      "每个应用可以设置取链默认值，比如起播集、卡点集；搭建时按这里的默认值取链，也可以单次调整。",
      dialogTarget,
    ),
    click("关闭", "关闭", "看完点“关闭”。", inDialog("button", "关闭")),
    click(
      "链接记录",
      "链接记录",
      "所有已经取到的推广链接都记录在这里。",
      tab("链接记录"),
    ),
    look(
      "每部剧的推广链接",
      "同一部剧再次搭建时优先复用已有链接；链接过期或参数变化时自动重新取链。",
      tableRow(0),
    ),
  ],
}

const materials: TourChapter = {
  id: "materials",
  title: "素材上传",
  summary: "批量上传视频，自动安排账户入库",
  steps: [
    click(
      "打开素材库",
      "素材库",
      "素材统一上传、统一管理，搭建时按剧名自动匹配。",
      navItem("素材库"),
    ),
    look(
      "素材列表",
      "文件名里写上完整剧名，搭建时就能自动匹配到这部剧。每条素材显示上传来源、文件信息、入库状态和可用账户数。",
      tableRow(0),
      { path: at("/materials") },
    ),
    look(
      "素材制作",
      "需要剪辑新素材时点左侧“素材制作”，会在新标签页打开君博的素材平台并自动登录（需要有权限的生产账号）。",
      sideButton("素材制作"),
      { placement: "right" },
    ),
    click(
      "批量上传",
      "批量上传",
      "演示一次上传。正式使用时可以一次选择上万个视频。",
      button("批量上传", true),
      { settleMs: 800 },
    ),
    click(
      "选择素材文件",
      "选择本地素材",
      "正式使用时这里会打开电脑的文件选择框，一次最多选 2 万个视频。体验版不需要准备视频，点一下会直接放入 3 个示例视频。",
      inDialog("button", "选择本地素材"),
      { settleMs: 800 },
    ),
    click(
      "开始上传",
      "开始上传",
      "已选好 3 个示例视频，文件名里带着完整剧名。点“开始上传”。",
      inDialog("button", /开始上传/),
      { settleMs: 1500 },
    ),
    look(
      "上传进度",
      "原文件先临时中转，系统自动安排合法的素材账户上传到 TikTok，最后核实入库。状态会依次变化，直到“账户素材可用”。",
      tableRow(0),
      {
        readyWhen: tableHas("账户素材可用"),
        waitingText: "正在上传和核实，约 10 秒…",
      },
    ),
    click(
      "回到素材库",
      "素材库",
      "回到素材库看看刚上传的素材。",
      tab("素材库"),
    ),
    look(
      "新素材已入库",
      "刚上传的示例素材已经入库，可以用于搭建。",
      tableRow(0, "Love After the Storm"),
    ),
  ],
}

const strategies: TourChapter = {
  id: "strategies",
  title: "投放策略",
  summary: "预算、出价、定向、分组规则保存成策略",
  steps: [
    click(
      "打开投放策略",
      "投放策略",
      "把预算、出价、定向、广告组和广告的生成规则保存成策略，搭建时一键套用，团队口径统一。",
      navItem("投放策略"),
    ),
    look(
      "策略列表",
      "每套策略显示当前版本、预算策略、竞价策略和生成规则。每次修改都保存为新版本，历史可追溯。",
      tableRow(0),
      { path: at("/strategies") },
    ),
    click(
      "复制为新策略",
      "复制为新策略",
      "基于现有策略改一改最快。点第一行的“复制为新策略”。",
      inRow("button, a", 0, "复制为新策略"),
      { settleMs: 1200 },
    ),
    look(
      "策略名称",
      "给新策略起个名字。点下面的按钮填入示例名称，也可以自己输入。",
      fieldInput("策略名称"),
      {
        action: {
          label: "帮我填写",
          run: () =>
            void fillField(fieldInput("策略名称")(), "体验策略 · 美区加量测试"),
        },
        readyWhen: hasValue(fieldInput("策略名称")),
        waitingText: "先填写策略名称。",
      },
    ),
    look(
      "实时结构预览",
      "右侧按当前设置实时演示会生成多少系列、广告组和广告，以及预算合计，改完马上看到效果。",
      card("结构与预算示例"),
    ),
    click(
      "创建策略",
      "创建策略",
      "保存为新策略的第 1 个版本。",
      button("创建策略", true),
      { settleMs: 1500 },
    ),
    look(
      "策略已创建",
      "新策略已经可以在搭建时选用。之后每次编辑都会生成新版本，已提交的搭建任务不受影响。",
      "main h1",
    ),
  ],
}

const builds: TourChapter = {
  id: "builds",
  title: "批量搭建广告",
  summary: "多部剧 × 多个账户一次搭建并启用",
  steps: [
    click(
      "打开广告搭建",
      "广告搭建",
      "核心功能：一次把多部剧投到多个账户。",
      navItem("广告搭建"),
    ),
    look(
      "三步完成搭建",
      "批量输入 → 准备与调整 → 搭建预览。前两步都不会创建广告，预览确认后才提交。",
      byText("main div", /^1·批量输入/),
      { path: at("/builds/new") },
    ),
    click(
      "选择版权方连接",
      "版权方连接",
      "先选这批剧从哪个版权方取推广链接。",
      '[role="combobox"][aria-label="版权方连接"]',
      { settleMs: 600 },
    ),
    click(
      "选择网眼",
      "网眼 · 主账号",
      "选“网眼 · 主账号”。",
      inDialog('[role="option"]', "网眼"),
    ),
    click(
      "选择推广应用",
      "推广应用",
      "再选用哪个推广应用（小程序）。",
      '[role="combobox"][aria-label="推广应用"]',
      { settleMs: 600 },
    ),
    click(
      "选一个应用",
      "第一个应用",
      "选第一个可用的应用。",
      inDialog('[role="option"]'),
    ),
    click(
      "选择投放策略",
      "投放策略",
      "最后选套用哪套投放策略。",
      '[role="combobox"][aria-label="投放策略"]',
      { settleMs: 600 },
    ),
    click(
      "选一套策略",
      "美区 · 目标 ROAS 1.1",
      "选“美区 · 目标 ROAS 1.1 · 按素材分组”。",
      inDialog('[role="option"]', "美区·目标ROAS1.1"),
    ),
    look(
      "填写剧目",
      "每行一部完整剧名。点下面的按钮填入 2 部示例剧。",
      "#build-drama",
      {
        action: {
          label: "填入示例剧目",
          run: () => void fillField("#build-drama", SAMPLE_DRAMAS.join("\n")),
        },
        readyWhen: hasValue(() => document.getElementById("build-drama")),
        waitingText: "先填入剧目。",
      },
    ),
    look(
      "填写广告账户",
      "每行一个账户 ID 或名称，也可以点“选择账户”从列表勾选。所有剧都会投到所有账户。点下面的按钮填入 2 个示例账户。",
      "#build-account",
      {
        action: {
          label: "填入示例账户",
          run: () =>
            void fillField("#build-account", SAMPLE_ACCOUNTS.join("\n")),
        },
        readyWhen: hasValue(() => document.getElementById("build-account")),
        waitingText: "先填入账户。",
      },
    ),
    click(
      "下一步：准备链接与素材",
      "下一步：准备链接与素材",
      "系统核实剧名和账户、自动取推广链接、按剧名匹配素材。这一步还不会创建广告。",
      button("下一步：准备链接与素材", true),
      { settleMs: 2500 },
    ),
    look(
      "准备结果",
      "每部剧的推广链接、匹配到的素材数量都在这里，可以逐个修改链接、调整素材。",
      tableRow(0),
      {
        readyWhen: () => !!button("选择小程序", true)(),
        waitingText: "正在取链和匹配素材…",
      },
    ),
    click(
      "选择推广小程序",
      "选择小程序",
      "选这批剧实际播放的小程序。",
      button("选择小程序", true),
      { settleMs: 800 },
    ),
    click(
      "选一个小程序",
      "选择并继续",
      "系统会检查每个目标账户是否都能用这个小程序。",
      inDialog("button", "选择并继续"),
      { settleMs: 2000 },
    ),
    click(
      "选择投放身份",
      "选择投放身份",
      "选广告展示时用的 TikTok 账号。",
      button("选择投放身份", true),
      { settleMs: 800 },
    ),
    click(
      "选一个身份",
      "Velvet Reels",
      "选“Velvet Reels”，系统会逐个账户验证这个身份。",
      inDialog("button, [role=option], li, div[tabindex]", "Velvet Reels"),
      { settleMs: 2000 },
    ),
    look(
      "定向与排期",
      "国家、语言、年龄等定向默认来自策略，可以只针对这一批修改；排期可以立即开始或指定时间。",
      byText("label, button, [role=radio]", "立即开始"),
    ),
    click(
      "生成搭建预览",
      "生成搭建预览",
      "生成最终要创建的内容。",
      button("生成搭建预览", true),
      { settleMs: 2500 },
    ),
    look(
      "搭建预览",
      "精确列出每部剧在每个账户会创建多少系列、广告组、广告，以及排除了哪些组合、为什么排除。",
      tableRow(0),
      {
        readyWhen: () => {
          const submit = document.querySelector<HTMLButtonElement>(
            'button[aria-label^="创建并立即启用"]',
          )
          return !!submit && !submit.disabled
        },
        waitingText: "正在生成预览…",
      },
    ),
    look(
      "数量与预算合计",
      "底部汇总：总共多少系列、广告组、广告，以及系列预算合计。",
      byText("p", /Campaign·\d+AdGroup/),
    ),
    click(
      "提交搭建",
      "创建并立即启用",
      "提交后系统直接在 TikTok 创建并启用，不需要再手动开启。",
      'button[aria-label^="创建并立即启用"]',
      { settleMs: 2000 },
    ),
    look(
      "执行进度",
      "后台逐个账户执行，已创建数量实时更新。等几秒看看进度变化。",
      'dl[aria-label="创建数量摘要"]',
      {
        readyWhen: () => !mainHas("任务正在处理")() && !mainHas("排队中")(),
        waitingText: "正在逐个账户创建…",
      },
    ),
    click(
      "查看组合明细",
      "查看明细",
      "每个“剧 × 账户”组合都能看到具体创建了哪些广告组和广告。",
      inRow("button", 0, "查看明细"),
      { settleMs: 1000 },
    ),
    look(
      "组合明细",
      "广告组、广告、素材逐项列出，带平台返回的 ID 和审核状态。失败的步骤可以一键重试。",
      dialogTarget,
    ),
    click("关闭明细", "关闭", "点右上角的 × 关闭。", dialogClose),
  ],
}

const ads: TourChapter = {
  id: "ads",
  title: "报表与批量调整",
  summary: "六维报表、批量改预算、操作记录",
  steps: [
    click(
      "打开广告报表",
      "广告报表",
      "所有账户的投放数据汇总在一张报表里。",
      navItem("广告报表"),
    ),
    look(
      "核心指标",
      "消耗、ROAS、展示、点击率、CPM 等指标按所选日期汇总，日期和筛选条件可以保存起来常用。",
      card("消耗"),
      { path: at("/ads") },
    ),
    click(
      "按系列查看",
      "系列",
      "可以按账户、系列、广告组、广告、素材、剧六个维度分析。点“系列”。",
      tab("系列"),
      { settleMs: 1200 },
    ),
    look(
      "系列明细",
      "每一行是一个系列，显示状态、预算、消耗、ROAS 和多日回收。点行首的箭头可以展开详情和趋势。",
      tableRow(0),
    ),
    click(
      "勾选系列",
      "勾选框",
      "勾选第一行。勾选后可以批量启用、暂停、改预算、改目标 ROAS、改名。",
      inRow('[role="checkbox"]', 0),
    ),
    click(
      "批量修改预算",
      "修改预算",
      "演示批量改预算。",
      button("修改预算", true),
      { settleMs: 1000 },
    ),
    look(
      "填写新预算",
      "预算会改在实际持有预算的那一层。点下面的按钮填入 250，提交后由后台逐项校验并执行。",
      '[aria-label="管理新值"]',
      {
        action: {
          label: "填入 250",
          run: () => void fillField('[aria-label="管理新值"]', "250"),
        },
        readyWhen: () =>
          !!inDialog("button", /^提交/)() && !mainHas("没有需要修改的对象")(),
        waitingText: "先填入新的日预算。",
      },
    ),
    click(
      "确认提交",
      "提交",
      "确认预算参数后直接提交管理任务，准备进度与逐项结果可在操作记录查看。",
      inDialog("button", /^提交管理任务$/),
      { settleMs: 1500 },
    ),
    click(
      "查看操作记录",
      "查看操作记录",
      "修改已经提交，几秒后执行完成，报表里的预算会同步更新。点“查看操作记录”。",
      byText("a, button", "查看操作记录"),
      { settleMs: 1200 },
    ),
    look(
      "操作记录",
      "每次批量修改都有记录：谁、什么时候、改了哪些对象、平台是否接收。人工操作和自动规则触发的都在这里。",
      tableRow(0),
      { path: at("/ad-management-tasks") },
    ),
    click(
      "查看明细",
      "查看明细",
      "点第一条的“查看明细”。",
      inRow("a, button", 0, "查看明细"),
      { settleMs: 1200 },
    ),
    look(
      "逐项回执",
      "每个对象的原值、新值、平台回执和数据核对结果；需要时可以一键恢复成原值。",
      card("逐项回执"),
    ),
  ],
}

const rules: TourChapter = {
  id: "rules",
  title: "自动规则",
  summary: "按条件自动盯盘、提预算、止损",
  steps: [
    click(
      "打开自动规则",
      "自动规则",
      "让系统按规则盯盘：ROAS 过低自动暂停、达标自动提预算、夜间降预算……",
      navItem("自动规则"),
    ),
    look(
      "执行概览",
      "最近的执行动作、待确认、失败和冲突数量一目了然。",
      card("执行动作"),
      { path: at("/automation-rules") },
    ),
    look(
      "规则列表",
      "每条规则可以选择“仅通知”“确认后执行”或“自动执行”，按固定频率运行，开关随时启停。",
      tableRow(0),
    ),
    click(
      "从模板新建",
      "从模板新建",
      "用模板快速建一条规则。",
      button("从模板新建", true),
      { settleMs: 800 },
    ),
    click(
      "选模板",
      "剧级止损",
      "选“剧级止损”：每部剧今天消耗高且 ROAS 偏低时暂停。",
      inDialog("button, [role=option], div[tabindex], li", "剧级止损"),
      { settleMs: 1200 },
    ),
    look(
      "规则内容",
      "范围、条件、动作、频率和通知都已按模板填好，可以按需调整。",
      dialogTarget,
    ),
    click(
      "试运行",
      "试运行",
      "保存前先试运行，看看按现在的数据会命中哪些对象。试运行不会改动广告。",
      inDialog("button", "试运行"),
      { settleMs: 2000 },
    ),
    click("保存规则", "保存", "确认后保存。", inDialog("button", "保存")),
    click(
      "待确认",
      "待确认",
      "“确认后执行”的规则命中后会在这里排队等人确认。",
      tab("待确认"),
    ),
    click(
      "处理一批",
      "处理",
      "点第一批的“处理”。",
      inRow("button", 0, "处理"),
      { settleMs: 1000 },
    ),
    look(
      "判断时与现在",
      "同时显示规则判断时和现在的数据；条件已不满足的对象会单独标出，不会被执行。",
      dialogTarget,
    ),
    click(
      "确认执行",
      "确认执行所选",
      "确认执行勾选的对象。",
      inDialog("button", "确认执行所选"),
      { settleMs: 1500 },
    ),
    click(
      "通知设置",
      "通知设置",
      "规则的预警和执行结果可以推送到飞书群。",
      navItem("通知设置"),
    ),
    click(
      "发送测试消息",
      "发送测试消息",
      "配置好飞书机器人后，先发一条测试消息确认能收到。",
      button("发送测试消息", true),
      { path: at("/notifications"), settleMs: 1200 },
    ),
    look(
      "体验完成",
      "主流程就到这里。左下角“引导”可以按章节重新演示；“本页讲解”讲解当前页面；需要输入时点“填入示例…”一键填好；“重置”回到初始数据。",
    ),
  ],
}

export const CHAPTERS: TourChapter[] = [
  accounts,
  providers,
  materials,
  strategies,
  builds,
  ads,
  rules,
]

function tourOf(id: string, title: string, chapters: TourChapter[]): Tour {
  return {
    id,
    title,
    steps: chapters.flatMap((chapter, index) =>
      chapter.steps.map((step) => ({
        ...step,
        chapter: `第 ${CHAPTERS.indexOf(chapter) + 1} 章 ${chapter.title}`,
        // 单独演示某一章时，第一步要先回到这一章开始的页面。
        path:
          step === chapter.steps[0] && index === 0 && !step.path
            ? chapterStart[chapter.id]
            : step.path,
      })),
    ),
  }
}

// 每章第一步是点侧栏进入；单独演示一章时先停在一个能看到侧栏的页面上。
const chapterStart: Record<string, TourStep["path"]> = {
  accounts: at("/accounts"),
  providers: at("/accounts"),
  materials: at("/providers"),
  strategies: at("/materials"),
  builds: at("/strategies"),
  ads: at("/build-tasks"),
  rules: at("/ads"),
}

export const MAIN_TOUR = tourOf("main", "完整引导", CHAPTERS)
export const chapterTour = (chapter: TourChapter) =>
  tourOf(chapter.id, chapter.title, [chapter])

// “本页怎么用”：当前页面对应的章节，从这一页开始讲（跳过“点侧栏进入”这一步）。
const PAGE_CHAPTERS: { match: RegExp; chapter: TourChapter }[] = [
  { match: /\/accounts$/, chapter: accounts },
  { match: /\/providers$/, chapter: providers },
  { match: /\/materials$/, chapter: materials },
  { match: /\/strategies$/, chapter: strategies },
  { match: /\/builds\/new$/, chapter: builds },
  { match: /\/ads$/, chapter: ads },
  { match: /\/automation-rules$/, chapter: rules },
]

const SIMPLE_PAGE_TOURS: { match: RegExp; tour: Tour }[] = [
  {
    match: /\/build-tasks$/,
    tour: {
      id: "build-tasks",
      title: "搭建任务",
      steps: [
        look(
          "搭建任务",
          "每次提交都是一个任务，可以按进行中、需处理、已完成筛选。部分失败的任务点进去可以一键重试，结果待核实的可以一键核查。",
          tableRow(0),
        ),
        click(
          "打开任务",
          "任务行",
          "点第一条任务进入详情。",
          inRow("a, button", 0),
        ),
      ],
    },
  },
  {
    match: /\/ad-management-tasks$/,
    tour: {
      id: "ad-management-tasks",
      title: "操作记录",
      steps: [
        look(
          "操作记录",
          "所有批量启停、改预算、改出价、改名的执行记录，包括人工和自动规则触发的。",
          tableRow(0),
        ),
        click(
          "查看明细",
          "查看明细",
          "点“查看明细”看每个对象的平台回执。",
          inRow("a, button", 0, "查看明细"),
        ),
      ],
    },
  },
  {
    match: /\/notifications$/,
    tour: {
      id: "notifications",
      title: "通知设置",
      steps: [
        click(
          "飞书通知",
          "发送测试消息",
          "配置飞书机器人地址后，自动规则的预警和执行结果会推送到群里。先发一条测试消息验证。",
          button("发送测试消息", true),
        ),
      ],
    },
  },
  {
    match: /\/members$/,
    tour: {
      id: "members",
      title: "成员管理",
      steps: [
        look(
          "成员与角色",
          "租户管理员、投手、只读成员三种角色，权限按租户隔离。",
          tableRow(0),
        ),
        click(
          "添加成员",
          "添加成员",
          "可以添加已有用户，或者直接新建用户加入租户。",
          button("添加成员", true),
        ),
      ],
    },
  },
  {
    match: /^\/platform\/tenants$/,
    tour: {
      id: "platform",
      title: "平台管理",
      steps: [
        look(
          "多租户",
          "平台管理员在这里开通租户、设置默认 BC、停用租户。",
          tableRow(0),
        ),
        click(
          "进入租户",
          "进入租户",
          "点“进入租户”代管这个租户的投放。",
          inRow("a, button", 0, "进入租户"),
        ),
      ],
    },
  },
]

export function pageTourFor(pathname: string): Tour | null {
  const page = PAGE_CHAPTERS.find(({ match }) => match.test(pathname))
  if (page) {
    const tour = chapterTour(page.chapter)
    // 已经在这一页了：章节第一步若是“点侧栏进入”，就从第二步讲起。
    const steps =
      page.chapter.steps[0].advance === "click"
        ? tour.steps.slice(1)
        : tour.steps
    return { ...tour, id: `page-${tour.id}`, steps }
  }
  return (
    SIMPLE_PAGE_TOURS.find(({ match }) => match.test(pathname))?.tour ?? null
  )
}
