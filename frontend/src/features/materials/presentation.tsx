import { AxiosError } from "axios"
import { Copy } from "lucide-react"
import { toast } from "sonner"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
export const namingHint =
  "请在图片或视频文件名中包含素材标识或完整剧名。搭建时优先按填写的素材标识匹配，留空才按剧名匹配。"
export const stages: Record<string, string> = {
  registering: "正在登记导入文件",
  sealed: "文件登记完整",
  waiting_capacity: "等待暂存空间",
  reserved: "已预留暂存空间",
  validating: "正在校验原文件",
  verified: "原文件已校验",
  cleanup_pending: "等待清理原件",
  deleting: "正在清理原件",
  delete_unknown: "清理结果待核实",
  deleted: "临时原件已清理",
  missing: "尚无临时原件",
  receiving: "等待原文件接收",
  stored: "原文件已接收，等待平台上传",
  uploading: "正在上传 TikTok",
  verifying: "正在核实素材",
  available: "账户素材可用",
  blocked: "需要处理",
  result_unknown: "上传结果待核实",
  pending: "等待上传",
  failed: "上传失败",
  unavailable: "账户素材不可用",
}
export function Stage({ status }: { status: string }) {
  return (
    <Badge variant={status === "available" ? "secondary" : "outline"}>
      {stages[status] || "状态待核实"}
    </Badge>
  )
}
export function bytes(value: number) {
  return value < 1024
    ? `${value} B`
    : value < 1024 ** 2
      ? `${(value / 1024).toFixed(1)} KB`
      : value < 1024 ** 3
        ? `${(value / 1024 ** 2).toFixed(1)} MB`
        : `${(value / 1024 ** 3).toFixed(2)} GB`
}
export function CopyValue({ value, label }: { value: string; label?: string }) {
  return (
    <span className="flex min-w-0 items-center gap-1">
      <span className="min-w-0 wrap-anywhere whitespace-normal font-mono text-xs">
        {value}
      </span>
      <Button
        size="icon"
        variant="ghost"
        aria-label={`复制${label || ""} ${value}`}
        onClick={() =>
          void navigator.clipboard
            .writeText(value)
            .then(() => toast.success("已复制"))
            .catch(() => toast.error("复制失败，请选择完整文本复制"))
        }
      >
        <Copy />
      </Button>
    </span>
  )
}
const errors: Record<string, string> = {
  bc_default_connection_required:
    "请先到“账户与授权 → 授权管理”为当前 BC 设置使用授权。",
  connection_unavailable:
    "当前授权不可用，请到“账户与授权”检查授权状态；已有上传任务保留原授权。",
  connection_bc_mismatch:
    "此授权尚未完成当前 BC 的接入，请到“账户与授权 → 授权管理”检查同步状态。",
  material_channel_unverified:
    "当前执行连接的素材入库能力尚未完成核验，原文件已保留，无需重复上传。",
  capability_scope_unknown: "当前执行连接的上传权限尚未核实，原文件已保留。",
  needs_reselect: "请重新选择未传完的原文件，再继续传输。",
  wrong_file: "文件内容或元数据与原文件不一致，请重新选择。",
  ambiguous_file: "有多个相同元数据的文件，请单独重新选择以核验。",
  needs_new_generation:
    "缺少可信分片校验记录，需要安全重新上传。请核实后重试。",
  storage_backpressure: "暂存空间已满，文件排队等待释放容量。",
  upload_identity_changed: "上传状态已变化，请刷新核实当前版本。",
  registration_required: "文件登记尚未完整，请按原导入继续。",
  registration_conflict: "所选文件与原导入记录不一致，请选择原文件。",
  completion_unknown: "原文件接收结果待核实，不会直接重传。",
  part_signature_expired: "分片链接已过期，系统核实后会重新签发。",
  part_rejected: "文件当前无法继续传输，请查看服务器状态。",
  transfer_unavailable: "传输暂未完成，请检查网络后按原导入继续。",
  no_upload_account:
    "当前 BC 暂无可上传账户，原文件保留，请联系管理员处理授权。",
  no_available_upload_account:
    "当前 BC 暂无可上传账户，原文件保留，请联系管理员处理授权。",
  object_storage_unavailable: "原文件存储暂不可用，请稍后查看进度。",
  object_storage_unconfigured: "原文件存储尚未配置，请联系管理员。",
  no_upload_capable_account:
    "当前 BC 暂无可上传账户，原文件保留，请联系管理员处理授权。",
  storage_unavailable: "原文件存储暂不可用，请稍后查看进度。",
  upload_not_retryable: "该步骤当前不能重试，请刷新核实进度。",
  object_result_unknown: "原文件接收结果待核实，请刷新进度。",
  invalid_file: "文件不符合要求，请查看文件信息。",
  invalid_image: "图片内容损坏或无法解码，请更换静态 JPG、PNG 文件。",
  unsupported_image: "仅支持静态 JPG、PNG 图片，暂不支持动画或多帧图片。",
  image_pixel_limit: "图片像素超过服务运行上限，请选择尺寸较小的图片。",
  image_type_mismatch: "图片实际内容与声明类型不一致，请核对文件格式。",
  image_source_unavailable:
    "当前 BC 尚无已核实的图片来源，请先在该 BC 上传或恢复来源授权。",
  image_preview_unavailable: "平台暂未返回可用的图片预览地址，请稍后重试。",
  image_not_carousel_usable: "平台回读确认此图片暂不可用于轮播，请更换图片。",
  image_share_failed: "目标账户图片共享未成功，请查看该账户核实结果。",
  image_reconciliation_ambiguous:
    "目标账户存在多个候选图片，暂无法确定本次上传结果。",
  image_source_frozen: "已有图片任务的来源账户已冻结，不能直接更换。",
  image_material_required: "此操作只接受图片原始素材。",
  image_distribution_limit: "图片分发每次最多 100 个账户素材对。",
  image_pipeline_required: "图片必须使用图片分发任务，不能进入视频上传。",
  permission_denied: "上传权限已失效，请联系管理员。",
  blocked_auth: "平台账户授权失效，原文件保留。",
}
export function issue(error: unknown): string {
  if (error instanceof Error && errors[error.message])
    return errors[error.message]
  if (error instanceof Error && error.message === "resume_storage_unavailable")
    return "浏览器无法保存上传恢复信息，请释放当前站点存储空间后重试。尚未创建批次。"
  if (typeof error === "string")
    return errors[error] || "该步骤需要处理，请刷新进度或联系管理员。"
  if (error instanceof AxiosError) {
    if (error.response?.status === 403)
      return "当前角色没有上传权限，请联系管理员。"
    const code = error.response?.data?.code
    return errors[code] || "请求未完成，请检查网络并刷新状态。"
  }
  return "传输未完成，请检查网络后继续。"
}
export const isUnknown = (e: unknown) =>
  !(e instanceof AxiosError) ||
  !e.response ||
  e.response.status >= 500 ||
  e.response.status === 408
export const materialKey = (tenantId: string) =>
  ["tenant", tenantId, "materials"] as const
export const uploadKey = (tenantId: string, bcId: string) =>
  [...materialKey(tenantId), "uploads", bcId] as const
