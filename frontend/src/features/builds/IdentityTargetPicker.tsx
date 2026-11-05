import { useQueryClient } from "@tanstack/react-query"
import { RotateCw } from "lucide-react"
import { useState } from "react"
import { BuildsService, type DraftSummary, type IdentityOption } from "@/client"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { buildKey, mutationKey, readPendingMutation } from "./api"
import { BuildError, reportError, unknownOutcome } from "./presentation"
import { useDraftIdentities } from "./useDraftIdentities"

function identityName(item: IdentityOption) {
  return item.display_name || item.username || "未命名投放身份"
}

function identityType(item: IdentityOption) {
  return item.identity_type === "BC_AUTH_TT" ? "BC 授权身份" : "TikTok 账号"
}

export function IdentityTargetPicker({
  tenantId,
  bcId,
  summary,
  write,
  onPrepare,
  onRefresh,
}: {
  tenantId: string
  bcId: string
  summary: DraftSummary
  write: boolean
  onPrepare: () => Promise<void>
  onRefresh: () => void
}) {
  const [open, setOpen] = useState(false)
  const [input, setInput] = useState("")
  const [search, setSearch] = useState("")
  const [busy, setBusy] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<unknown>()
  const client = useQueryClient()
  const query = useDraftIdentities(tenantId, bcId, summary, search)
  const data = query.data
  if (!summary.account_count || !summary.drama_count) return null
  const pendingKey = mutationKey(tenantId, bcId, summary.draft_id)
  const selectable =
    write && summary.status === "READY" && !readPendingMutation(pendingKey)

  async function choose(item: IdentityOption) {
    if (!selectable || busy || refreshing || !data?.catalog_job_id) return
    const requestId = crypto.randomUUID()
    setBusy(true)
    setError(undefined)
    sessionStorage.setItem(
      pendingKey,
      JSON.stringify({ requestId, kind: "identity", prepare: false }),
    )
    try {
      await BuildsService.selectIdentity({
        path: { tenant_id: tenantId, draft_id: summary.draft_id },
        body: {
          request_id: requestId,
          expected_revision: summary.revision,
          catalog_job_id: data.catalog_job_id,
          identity_id: item.identity_id,
          identity_type: item.identity_type,
          identity_authorized_bc_id: item.identity_authorized_bc_id,
        },
      })
      sessionStorage.removeItem(pendingKey)
      setOpen(false)
      await client.invalidateQueries({
        queryKey: [...buildKey(tenantId, bcId), summary.draft_id],
      })
      onRefresh()
    } catch (e) {
      if (!unknownOutcome(e)) sessionStorage.removeItem(pendingKey)
      setError(e)
      reportError(e)
      onRefresh()
    } finally {
      setBusy(false)
    }
  }

  async function refresh() {
    if (!selectable || busy || refreshing) return
    setRefreshing(true)
    setError(undefined)
    try {
      // 显式刷新先过期服务端目录；普通准备继续复用有效的账户资料。
      await BuildsService.refreshIdentitiesCatalog({
        path: { tenant_id: tenantId, draft_id: summary.draft_id },
      })
      await client.invalidateQueries({
        queryKey: [...buildKey(tenantId, bcId), summary.draft_id],
      })
      await onPrepare()
      onRefresh()
    } catch (e) {
      setError(e)
      reportError(e)
    } finally {
      setRefreshing(false)
    }
  }

  return (
    <Card>
      <CardContent className="flex min-w-0 flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="font-medium">投放身份</p>
            <p className="text-sm text-muted-foreground">
              {data?.selected
                ? `${identityName(data.selected)}${data.selected.username ? ` · @${data.selected.username}` : ""}`
                : query.isError
                  ? "可用投放身份读取失败，请更新后重试"
                  : query.isPending || data?.state === "pending"
                    ? "正在读取可用投放身份…"
                    : data?.state === "unavailable"
                      ? "参考账户暂无可用投放身份"
                      : data?.state === "stale"
                        ? "原投放身份已不可用，请重新选择"
                        : "请选择本批次统一使用的投放身份"}
            </p>
          </div>
          {write && (
            <div className="flex gap-2">
              <Button
                variant="ghost"
                size="icon"
                disabled={!selectable || busy || refreshing}
                onClick={() => void refresh()}
                title="刷新可用投放身份列表"
                aria-label="刷新可用投放身份列表"
              >
                <RotateCw className={refreshing ? "animate-spin" : ""} />
              </Button>
              <Button
                variant="outline"
                disabled={busy || refreshing || !data?.catalog_job_id}
                onClick={() => {
                  setInput("")
                  setSearch("")
                  setOpen(true)
                }}
              >
                {data?.selected ? "更换投放身份" : "选择投放身份"}
              </Button>
            </div>
          )}
        </div>
        {query.error ? <BuildError error={query.error} /> : null}
        {!open && error ? <BuildError error={error} /> : null}
        {(query.isError || data?.state === "pending") &&
          summary.status === "READY" &&
          write && (
            <Button
              className="self-start"
              variant="outline"
              disabled={!selectable || busy || refreshing}
              onClick={() => void refresh()}
            >
              更新可用投放身份
            </Button>
          )}
        <p className="text-xs text-muted-foreground">
          目录来自排序后的第一个广告账户；选择后会逐个广告账户验证同一身份，不会自动换成其他身份。
        </p>
        <Dialog open={open} onOpenChange={(value) => !busy && setOpen(value)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>选择投放身份</DialogTitle>
              <DialogDescription>
                同名的不同授权身份会分别列出，请结合类型和 ID 选择。
              </DialogDescription>
            </DialogHeader>
            {error ? <BuildError error={error} /> : null}
            <form
              className="flex items-end gap-2"
              onSubmit={(event) => {
                event.preventDefault()
                event.stopPropagation()
                setSearch(input.trim())
              }}
            >
              <Field>
                <FieldLabel htmlFor="identity-selector-search">
                  搜索投放身份
                </FieldLabel>
                <Input
                  id="identity-selector-search"
                  maxLength={255}
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                  placeholder="输入名称、用户名或完整 ID"
                />
              </Field>
              <Button type="submit" disabled={busy}>
                搜索
              </Button>
            </form>
            <div className="max-h-96 space-y-2 overflow-y-auto">
              {data?.items?.map((item) => (
                <Button
                  key={`${item.identity_type}:${item.identity_id}:${item.identity_authorized_bc_id || ""}`}
                  className="h-auto w-full justify-start gap-3 whitespace-normal p-3 text-left"
                  variant="outline"
                  disabled={busy || !selectable || query.isFetching}
                  onClick={() => void choose(item)}
                >
                  <Avatar>
                    <AvatarImage src={item.profile_image || undefined} alt="" />
                    <AvatarFallback>
                      {identityName(item).slice(0, 1)}
                    </AvatarFallback>
                  </Avatar>
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-2">
                      <strong>{identityName(item)}</strong>
                      <Badge variant="secondary">{identityType(item)}</Badge>
                    </span>
                    {item.username ? (
                      <span className="block text-xs font-normal text-muted-foreground">
                        @{item.username}
                      </span>
                    ) : null}
                    <span className="block break-all text-xs font-normal text-muted-foreground">
                      {item.identity_id}
                    </span>
                  </span>
                </Button>
              ))}
              {!query.isFetching && !data?.items?.length ? (
                <p className="text-sm text-muted-foreground">
                  {search
                    ? "没有符合条件的投放身份，请调整关键词。"
                    : "当前没有可选的投放身份。"}
                </p>
              ) : null}
            </div>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  )
}
