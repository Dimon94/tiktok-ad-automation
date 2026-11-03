import { AxiosError } from "axios"
import { Copy } from "lucide-react"
import { useState } from "react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { isForbidden } from "@/features/tenants/shared"
export const kinds: Record<string, string> = {
  wangyan: "网眼",
  jiashu: "嘉书",
  duiba: "兑吧",
  gangganhao: "刚刚好",
  rongliang: "容量",
  minglue: "明略",
}
export const connectionStates: Record<string, string> = {
  pending: "待验证",
  active: "可用",
  verifying: "正在验证",
  reauth_required: "等待自动恢复",
  error: "验证异常",
  disabled: "已停用",
}
export const resultStates: Record<string, string> = {
  pending: "处理中",
  needs_resolution: "待选候选",
  blocked_auth: "连接或权限需处理",
  config_conflict: "配置冲突",
  retryable_error: "暂时失败",
  result_unknown: "结果待核实",
  failed: "取链失败",
  ready: "已就绪",
}
const errors: Record<string, string> = {
  provider_auth_expired: "版权方认证已过期",
  provider_session_expired: "版权方会话已过期，系统将自动恢复后继续。",
  provider_session_refreshing: "正在自动恢复版权方连接，完成后继续当前任务。",
  provider_session_refresh_failed:
    "版权方连接暂未恢复，请稍后检查网络或联系管理员。",
  provider_application_forbidden:
    "该账号无权访问此应用，请联系管理员检查应用权限。",
  connection_unavailable: "连接尚不可用，请联系管理员检查验证状态。",
  lookup_incomplete:
    "版权方历史链接尚不能完整核实，本次取链已停止，未创建新链接。",
  provider_unavailable: "版权方服务暂时不可用，请查看任务实际状态。",
  provider_captcha_failed: "验证码自动识别未成功，请重新验证版权方连接。",
  provider_auth_failed: "版权方认证失败，请检查账号与密码。",
  provider_credentials_invalid: "认证信息不符合该版权方要求。",
  provider_forbidden: "该账号无权访问此应用。",
  provider_app_forbidden: "该账号无权访问此应用。",
  provider_rejected: "版权方未接受本次请求。",
  provider_schema_unsupported: "版权方响应暂不受支持，请联系管理员。",
  provider_result_unknown: "版权方结果尚未核实，请查看后续核实进度。",
  provider_timeout: "版权方响应超时，请查看连接或任务的实际状态。",
  config_conflict: "已有渠道配置与本次请求不一致。",
  config_unverifiable: "已有渠道配置尚未核实。",
  provider_request_invalid: "版权方输入无效，请检查必填字段。",
}
export function ProviderError({ error }: { error: unknown }) {
  // Never render arbitrary remote text or retain Axios request objects containing credentials.
  const code =
    error instanceof AxiosError
      ? error.response?.data?.code
      : typeof error === "string"
        ? error
        : undefined
  return (
    <Alert variant="destructive">
      <AlertTitle>
        {isForbidden(error) || code === "action_forbidden"
          ? "权限不足"
          : "操作未完成"}
      </AlertTitle>
      <AlertDescription>
        {isForbidden(error) || code === "action_forbidden"
          ? "当前角色无权执行此操作，请联系租户管理员。"
          : errors[code] || "请求未完成，请检查连接的实际状态后再操作。"}
      </AlertDescription>
    </Alert>
  )
}
export function safeError(error: unknown): string {
  if (isForbidden(error)) return "action_forbidden"
  return error instanceof AxiosError &&
    typeof error.response?.data?.code === "string"
    ? error.response.data.code
    : "request_failed"
}
export function CopyField({
  value,
  label,
  expanded = false,
}: {
  value: string | null | undefined
  label: string
  expanded?: boolean
}) {
  const [copied, setCopied] = useState(false)
  if (value == null)
    return <span className="text-muted-foreground">尚无{label}</span>
  return (
    <div className="flex min-w-0 items-center gap-2">
      <span
        className={
          expanded
            ? "min-w-0 flex-1 break-all whitespace-pre-wrap text-sm"
            : "max-w-56 truncate text-xs"
        }
      >
        {value || "无独立归因基础名"}
      </span>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label={`复制${label}`}
        onClick={() => {
          void navigator.clipboard.writeText(value).then(() => setCopied(true))
        }}
      >
        <Copy />
      </Button>
      {copied && (
        <span role="status" className="sr-only">
          已复制{label}
        </span>
      )}
    </div>
  )
}
const configLabels: Record<string, string> = {
  episode: "起播集数",
  charge_level: "付费档位",
  channel_prefix: "渠道前缀",
  chapter_index: "起播章节",
}
export function ConfigFacts({
  config,
  incomplete = false,
}: {
  config: Record<string, string | number | boolean | null> | null | undefined
  incomplete?: boolean
}) {
  if (config == null) return <p className="text-muted-foreground">尚未核实</p>
  return (
    <div className="flex flex-col gap-2">
      {Object.entries(config).length ? (
        <dl className="grid grid-cols-[auto_1fr] gap-2">
          {Object.entries(config).map(([key, value]) => (
            <div key={key} className="contents">
              <dt>{configLabels[key] || key}</dt>
              <dd className="break-all">
                {value == null ? "尚未核实" : String(value)}
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <p>暂无已公开的配置项</p>
      )}
      {incomplete && (
        <p className="text-muted-foreground">
          仅展示已核实的公开配置，原配置未完整展示。
        </p>
      )}
    </div>
  )
}
