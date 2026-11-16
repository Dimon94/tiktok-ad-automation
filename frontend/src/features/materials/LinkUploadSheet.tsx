import { useMutation } from "@tanstack/react-query"
import { AxiosError } from "axios"
import { useEffect, useRef, useState } from "react"
import { type LinkBatchCreate, MaterialLinksService } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Textarea } from "@/components/ui/textarea"
import { AccountPicker } from "@/features/accounts/AccountPicker"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { RequestError } from "@/features/tenants/shared"
import { parseMaterialLinks } from "./link-input"

export function LinkUploadSheet({
  tenantId,
  bcId,
  onClose,
  onStarted,
}: {
  tenantId: string
  bcId: string
  onClose: () => void
  onStarted: (id: string) => void
}) {
  const [text, setText] = useState("")
  const [accounts, setAccounts] = useState("")
  const [submitted, setSubmitted] = useState<LinkBatchCreate | null>(null)
  const controller = useRef(new AbortController())
  useEffect(() => {
    const current = new AbortController()
    controller.current = current
    return () => current.abort()
  }, [])
  const parsed = parseMaterialLinks(text)
  const advertisers = [
    ...new Set(
      accounts
        .split("\n")
        .map((v) => v.trim())
        .filter(Boolean),
    ),
  ]
  const total = parsed.unique.length * advertisers.length
  const mutation = useMutation({
    mutationFn: async (body: LinkBatchCreate) =>
      (
        await MaterialLinksService.createLinkUpload({
          path: { tenant_id: tenantId },
          body,
          signal: controller.current.signal,
        })
      ).data,
    onError: (error) => {
      if (
        error instanceof AxiosError &&
        [400, 403, 404, 409, 422].includes(error.response?.status ?? 0)
      )
        setSubmitted(null)
    },
    onSuccess: (batch) => {
      if (!controller.current.signal.aborted) onStarted(batch.id)
    },
  })
  const locked = mutation.isPending || submitted !== null
  function submit() {
    const body = submitted ?? {
      request_id: crypto.randomUUID(),
      bc_id: bcId,
      text,
      advertiser_ids: advertisers,
    }
    // 传输异常后保留原请求，点击核实只重复同一幂等请求，不另建批次。
    setSubmitted(body)
    mutation.mutate(body)
  }
  return (
    <ManagementSheet
      title="素材链接上传"
      description={`目标 BC：${bcId}。每条链接将上传到所有所选广告账户，提交后在后台继续处理。`}
      dirty={Boolean(text || accounts) && !submitted}
      onClose={onClose}
    >
      <FieldGroup>
        <Field data-invalid={parsed.errors.length > 0}>
          <FieldLabel htmlFor="material-links">素材预览链接</FieldLabel>
          <Textarea
            id="material-links"
            aria-invalid={parsed.errors.length > 0}
            rows={10}
            placeholder="每行一个 TikTok 素材预览链接，支持换行批量粘贴"
            value={text}
            disabled={locked}
            maxLength={250000}
            onChange={(event) => setText(event.target.value)}
          />
          <FieldDescription>
            最多 500 行；相同 VID 仅保留首次出现。按上海日期 + 原 VID +
            原行号自动命名。
          </FieldDescription>
          {parsed.errors.length > 0 && (
            <FieldError>{parsed.errors.slice(0, 5).join("；")}</FieldError>
          )}
        </Field>
        <Field>
          <FieldLabel htmlFor="link-upload-accounts">目标广告账户</FieldLabel>
          <AccountPicker
            tenantId={tenantId}
            bcId={bcId}
            value={accounts}
            disabled={locked}
            capability="upload"
            onChange={setAccounts}
          />
          <Textarea
            id="link-upload-accounts"
            rows={3}
            placeholder="可选择账户，也可每行粘贴一个广告账户 ID"
            value={accounts}
            disabled={locked}
            onChange={(event) => setAccounts(event.target.value)}
          />
          <FieldDescription>
            仅支持当前 BC 下有素材上传权限的账户，最多 20 个。
          </FieldDescription>
        </Field>
        <Alert>
          <AlertDescription>
            {parsed.unique.length} 个有效链接 ·{" "}
            {parsed.lines.length - parsed.unique.length} 个重复链接 ·{" "}
            {advertisers.length} 个账户 · {total} 条上传任务
          </AlertDescription>
        </Alert>
        {(advertisers.length > 20 || total > 2000) && (
          <FieldError>
            每批最多 20 个账户、2000 条上传任务，请分批提交。
          </FieldError>
        )}
        {mutation.error && (
          <>
            <RequestError error={mutation.error} retry={submit} />
            <p className="text-sm text-muted-foreground">
              提交结果待核实。再次提交会复用原请求；也可关闭后在链接上传列表查看批次。
            </p>
          </>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose}>
            关闭
          </Button>
          <Button
            disabled={
              mutation.isPending ||
              (!submitted &&
                (!total ||
                  parsed.errors.length > 0 ||
                  advertisers.length > 20 ||
                  total > 2000 ||
                  advertisers.some((v) => !/^\d{1,30}$/.test(v))))
            }
            onClick={submit}
          >
            {mutation.isPending
              ? "正在提交…"
              : submitted
                ? "核实原提交"
                : `开始上传 ${total} 条任务`}
          </Button>
        </div>
      </FieldGroup>
    </ManagementSheet>
  )
}
