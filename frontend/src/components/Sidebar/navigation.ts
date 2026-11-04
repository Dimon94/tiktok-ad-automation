import {
  BarChart3,
  Bell,
  Clapperboard,
  FileVideo,
  History,
  Link2,
  ListTodo,
  Monitor,
  SlidersHorizontal,
  Users,
  Workflow,
} from "lucide-react"
import type { NavGroup } from "./Main"

// 左侧导航按“投放准备 → 广告投放 → 设置”的日常流程分组：
// 先备素材和策略，再搭建、看任务与报表、做投后操作，配置类入口统一放在最后。
// prefix 为空时生成未接入租户时的全局入口；带租户时所有路径都挂在租户路由下。
export function workspaceNavigation({
  prefix = "",
  manage,
  materialProduction,
}: {
  prefix?: string
  manage: boolean
  materialProduction?: {
    open: () => void
    pending: boolean
    pendingLabel: string
  }
}): NavGroup[] {
  return [
    {
      label: "投放准备",
      items: [
        ...(prefix && materialProduction
          ? [
              {
                icon: Clapperboard,
                title: "素材制作",
                onClick: materialProduction.open,
                disabled: materialProduction.pending,
                pendingLabel: materialProduction.pending
                  ? materialProduction.pendingLabel
                  : undefined,
              },
            ]
          : []),
        { icon: FileVideo, title: "素材库", path: `${prefix}/materials` },
        {
          icon: SlidersHorizontal,
          title: "投放策略",
          path: `${prefix}/strategies`,
        },
      ],
    },
    {
      label: "广告投放",
      items: [
        {
          icon: Clapperboard,
          title: "广告搭建",
          path: prefix ? `${prefix}/builds/new` : "/",
        },
        { icon: ListTodo, title: "搭建任务", path: `${prefix}/build-tasks` },
        // 广告报表、操作记录依赖租户与 BC 上下文，未接入租户时不展示。
        ...(prefix
          ? [
              { icon: BarChart3, title: "广告报表", path: `${prefix}/ads` },
              { icon: BarChart3, title: "经营看板", path: `${prefix}/business` },
              {
                icon: History,
                title: "操作记录",
                path: `${prefix}/ad-management-tasks`,
              },
            ]
          : []),
        {
          icon: Workflow,
          title: "自动规则",
          path: `${prefix}/automation-rules`,
        },
      ],
    },
    {
      label: "设置",
      items: [
        { icon: Monitor, title: "账户与授权", path: `${prefix}/accounts` },
        { icon: Link2, title: "版权方连接", path: `${prefix}/providers` },
        // 通知设置与成员管理只对管理员开放；通知设置仅存在于租户路由下。
        ...(manage && prefix
          ? [{ icon: Bell, title: "通知设置", path: `${prefix}/notifications` }]
          : []),
        ...(manage
          ? [{ icon: Users, title: "成员管理", path: `${prefix}/members` }]
          : []),
      ],
    },
  ]
}
