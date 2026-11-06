import { useBlocker } from "@tanstack/react-router"
import { AxiosError } from "axios"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { useWorkspacePageGuard } from "@/features/workspace/WorkspaceTabs"
import { handleApiError } from "@/lib/api-feedback"
import { cn } from "@/lib/utils"
export function BuildGuard({
  dirty,
  title = "有未保存的修改",
  description = "离开会丢弃本页未保存输入，服务器中已保存的草稿和任务会保留。",
  leaveLabel = "丢弃未保存修改",
}: {
  dirty: boolean
  title?: string
  description?: string
  leaveLabel?: string
}) {
  const retainsPage = useWorkspacePageGuard(dirty)
  const blocker = useBlocker({
    // A forced 401 login transition must retain the request ledger without
    // being trapped by the unsaved-input prompt. Authorization remains server-owned.
    shouldBlockFn: ({ next }) =>
      dirty && !!localStorage.getItem("access_token") && !retainsPage(next),
    enableBeforeUnload: () => dirty && !!localStorage.getItem("access_token"),
    withResolver: true,
  })
  return (
    <Dialog
      open={blocker.status === "blocked"}
      onOpenChange={(open) => {
        if (!open) blocker.reset?.()
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={() => blocker.reset?.()}>
            留在当前页
          </Button>
          <Button onClick={() => blocker.proceed?.()}>{leaveLabel}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
export const states: Record<string, string> = {
  DRAFT: "草稿",
  PREPARING: "准备中",
  READY: "已就绪",
  BLOCKED: "无法搭建",
  BUILDING: "正在生成",
  FROZEN: "已冻结",
  OBSOLETE: "已过期",
  FAILED: "失败",
  ready: "已就绪",
  pending: "处理中",
  needs_resolution: "待选择候选",
  result_unknown: "结果待核实",
  config_conflict: "配置冲突",
  blocked_auth: "连接需要重新认证",
  duplicate: "重复输入",
  resolved: "已解析",
  unresolved: "未解析",
  failed: "失败",
  matched: "已匹配",
  empty: "空输入",
  manual: "已手动调整",
  ambiguous: "名称存在歧义",
  not_found: "未找到匹配",
  invalid: "输入无效",
}
export function BuildStatus({ value }: { value: string }) {
  return <span>{states[value] || value}</span>
}
export function BuildError({ error }: { error: unknown }) {
  const status =
    error instanceof AxiosError ? error.response?.status : undefined
  const code =
    error instanceof AxiosError ? error.response?.data?.code : undefined
  return (
    <Alert variant="destructive">
      <AlertTitle>
        {status === 403
          ? "权限不足"
          : [
                "tiktok_call_deadline_exceeded",
                "tiktok_local_resources_unavailable",
                "scene_refresh_failed",
              ].includes(code)
            ? "目录读取未完成"
            : status === 409
              ? "草稿已更新"
              : "操作未完成"}
      </AlertTitle>
      <AlertDescription>
        {typeof code === "string" && reasonLabels[code]
          ? reasonLabels[code]
          : status === 403
            ? "当前角色无权执行此操作，请联系管理员。"
            : status === 409
              ? "本地输入已保留。请先查看最新版本，核对差异后重新应用修改。"
              : code === "request_not_found"
                ? "尚未查到原请求，不能据此重新创建。请继续核实。"
                : "请求未完成，输入已保留。请核实当前状态。"}
        {typeof code === "string" && (
          <span className="block break-all text-xs">{code}</span>
        )}
      </AlertDescription>
    </Alert>
  )
}
export function reportError(error: unknown) {
  if (error instanceof Error) handleApiError(error)
}
export const unknownOutcome = (error: unknown) =>
  !(error instanceof AxiosError) ||
  !error.response ||
  error.response.status >= 500
export function BuildSteps({ step }: { step: 1 | 2 | 3 }) {
  // 步骤只提示当前位置，不再用整张卡片叠加默认上下留白。
  return (
    <ol
      aria-label="搭建步骤"
      className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2 text-xs"
    >
      {["批量输入", "准备与调整", "搭建预览"].map((label, i) => (
        <li
          key={label}
          aria-current={i + 1 === step ? "step" : undefined}
          className={cn(
            "flex items-center gap-2 after:ml-1 after:h-px after:w-4 after:bg-border last:after:hidden sm:after:w-10",
            i + 1 === step
              ? "font-medium text-foreground"
              : "text-muted-foreground",
          )}
        >
          <span
            className={cn(
              "flex size-5 shrink-0 items-center justify-center rounded-full border text-[11px]",
              i + 1 === step &&
                "border-primary bg-primary text-primary-foreground",
            )}
          >
            {i + 1}
          </span>
          {label}
        </li>
      ))}
    </ol>
  )
}

export const reasonLabels: Record<string, string> = {
  continuation_pending:
    "原任务仍在执行或结果待核实，请先核实原任务后刷新续建预览",
  continuation_changed: "来源任务已变化，请刷新续建预览后重新核对",
  continuation_unchanged: "当前配置对应的对象已完成，请查看原任务",
  continuation_route_changed: "执行连接或授权已变化，请先核实原任务",
  execution_superseded: "该范围已由新版本继续搭建",
  image_refresh_required:
    "图片：目标账户图片未全部就绪，请等待原分发任务核验。",
  image_source_unavailable:
    "图片：没有可分发的来源，请补充当前 BC 的图片来源。",
  image_target_unavailable: "图片：目标图片准备失败，请查看原素材任务。",
  image_content_changed: "图片：原始内容已变化，请重新生成预览。",
  carousel_music_missing: "音乐：请为图片创意选择音乐。",
  carousel_music_unavailable:
    "音乐：目标账户无法使用已选曲目，请调整草稿后重新预览。",
  carousel_music_unverified:
    "音乐：可提交；将在提交后、发送前核验目标账户的原选曲目。",
  carousel_music_read_failed: "音乐：读取暂时失败，重试将继续核验原曲目。",
  carousel_ads_unavailable: "图片：此账户的所有图片广告均已排除。",
  identity_unavailable: "身份：请检查当前账户可用身份并重新准备。",
  image_rules_not_applied: "图片：请应用当前组合规则。",
  image_composition_pending: "图片：组合仍在准备中。",
  music_preparation_pending: "音乐：配乐任务仍在准备中。",
  music_not_ready: "音乐：请完成所有图片创意的配乐。",
  bc_default_connection_required:
    "请先到“账户与授权 → 授权管理”为当前 BC 设置使用授权。",
  connection_unavailable:
    "当前授权不可用，请到“账户与授权”检查授权状态。已准备的任务仍保留原授权。",
  connection_bc_mismatch:
    "此授权尚未完成当前 BC 的接入，请到“账户与授权 → 授权管理”检查同步状态。",
  manual_link_mapping_required: "剧名已变化，请更新对应的手动链接。",
  manual_link_required: "请补充推广链接。",
  provider_rejected:
    "版权方未确认取链结果，请重新解析并准备，系统会优先读取已有链接。",
  manual_link_invalid: "请填写有效的 TikTok Minis 推广链接。",
  manual_attribution_required: "请补充网眼提供的归因名称。",
  manual_link_conflict: "同一剧目 ID 对应不同输入，请合并为一行。",
  tiktok_call_deadline_exceeded: "目录读取暂时超时，请点击更新后重试。",
  tiktok_local_resources_unavailable: "目录读取暂时繁忙，请点击更新后重试。",
  scene_refresh_failed: "目录读取失败，请点击更新后重试。",
  minis_selection_required: "请在推广小程序区域按名称选择后继续",
  minis_catalog_stale: "小程序目录已过期，请更新可用小程序后重新选择",
  minis_link_conflict: "所选小程序与推广链接指向不一致，请分开搭建",
  minis_unavailable: "所选小程序不在当前账户的可用目录中",
  minis_links_unavailable: "请先完成剧目推广链接准备",

  recovery_no_candidates: "当前没有需要重试或核查的步骤。",
  currency_mismatch: "账户币种与策略不一致",
  materials_missing: "未匹配到可用素材",
  material_unavailable: "素材暂不可用于此账户",
  material_remote_source_unavailable: "来源账户没有当前可用的视频副本",
  original_unavailable: "原文件及可用来源副本均不可用",
  material_digest_missing: "素材缺少可信内容摘要",
  material_result_pending: "素材上传结果尚未核实",
  material_reconciliation_budget_exhausted:
    "本轮素材核查已停止自动重试，结果仍未知；请核查原任务，勿重复上传。",
  material_share_unconfirmed: "素材共享结果尚未核实",
  material_share_unverified: "素材共享路径尚未核实",
  material_preview_unverified: "素材来源预览尚未通过核实",
  url_upload_capacity_exceeded: "素材超过 URL 转存容量限制",
  sdk_upload_capacity_exceeded: "素材超过文件上传容量限制",
  material_refresh_required: "正在重新核实目标账户的视频与封面",
  cover_pending: "正在准备目标账户的视频封面",
  cover_result_unknown: "封面上传结果待核实，请核查原任务，勿重复上传",
  cover_permission_unverified: "当前连接的封面读写权限尚未核实",
  cover_video_changed: "目标视频或连接已变化，请重新准备当前素材",
  cover_evidence_stale: "封面核实已过期，需要重新读取平台结果",
  cta_options_unavailable: "CTA 尚未通过当前场景核实",
  field_limits_unverified: "平台字段限制尚未核实",
  budget_limits_unverified: "预算限制尚未核实",
  adgroup_budget_unverified:
    "当前账户尚未核实组预算能力，请改用系列预算或先完成账户能力核查。",
  invalid_material_allocation:
    "素材安排与数量规则不匹配，请调整固定数量或素材上限后重新预览。",
  bid_strategy_invalid:
    "竞价策略与账户能力不匹配，请选择可用竞价方式并重新生成预览。",
  budget_out_of_range: "预算超出平台允许范围",
  roas_out_of_range: "目标 ROAS 超出允许范围",
  roas_limits_unverified: "目标 ROAS 限制尚未核实",
  scene_unsupported: "当前账户不支持所需投放场景",
  account_access_denied: "当前账户缺少搭建权限",
  account_not_in_bc: "账户不属于当前 BC",
  account_ownership_conflict: "账户归属存在冲突",
  account_metadata_incomplete: "账户信息尚未完善",
  scene_link_unavailable: "链接或应用资产尚不可用",
  targeting_regions_unavailable: "所选国家不在本批共同可投范围，请调整定向。",
  targeting_regions_unverified: "共同可投国家尚未核实，请重新准备。",
  preview_targeting_outdated: "定向规则已更新，请重新生成预览。",
  preview_naming_outdated: "命名规则已更新，请重试生成预览。",
  preview_batch_number_exhausted: "暂未分配到唯一随机号，请重试。",
  naming_context_missing: "缺少版权方或剧目标识，请核对后重试生成预览。",
  name_too_long: "生成名称超过平台长度限制",
  name_invalid: "生成名称包含不支持字符",
  duplicate_campaign_name: "Campaign 名称重复",
  copy_too_long: "冻结正文超过场景长度限制",
  creative_count_exceeded: "SP 创意条数超过场景限制",
  drama_not_found: "未找到完整剧名",
  config_conflict: "已有链接配置与本次请求冲突",
  config_unverifiable: "已有配置尚未核实",
  provider_auth_expired: "版权方认证已过期",
  result_unknown: "远端结果尚待核实",
}
export function BuildReason({ code }: { code: string | null }) {
  if (!code) return null
  return (
    <span className="block break-all text-xs text-muted-foreground">
      {reasonLabels[code] && (
        <span className="block">{reasonLabels[code]}</span>
      )}
      {code !== "recovery_no_candidates" && <span>{code}</span>}
    </span>
  )
}
