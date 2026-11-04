import { useQuery, useQueryClient } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { useCallback, useMemo, useState } from "react"
import {
  type ProviderApplicationPublic,
  type ProviderConnectionCreate,
  type ProviderConnectionPublic,
  ProvidersService,
} from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  displayTime,
  FilterSelect,
  Identifier,
} from "@/features/accounts/presentation"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import {
  canManage,
  isForbidden,
  Pager,
  ServerTable,
  useCursorPage,
  useRetainedData,
} from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { ApplicationLinkDefaults } from "./ApplicationLinkDefaults"
import { RateSettings } from "@/features/business/RateSettings"
import {
  connectionStates,
  kinds,
  ProviderError,
  safeError,
} from "./presentation"
import { applicationsQuery, connectionsQuery, providerKey } from "./queries"

export function ConnectionPanel({
  onLinks,
}: {
  onLinks: (id: string) => void
}) {
  const { tenantId, scope } = useTenantScope(),
    client = useQueryClient(),
    paging = useCursorPage()
  const [input, setInput] = useState(""),
    [search, setSearch] = useState(""),
    [kindFilter, setKindFilter] = useState("all"),
    [statusFilter, setStatusFilter] = useState("all")
  const [businessRate, setBusinessRate] = useState<ProviderConnectionPublic | null>(null)
  const query = useQuery({
    ...connectionsQuery(tenantId!, paging.cursor, paging.limit, {
      query: search,
      kind:
        kindFilter === "all"
          ? undefined
          : (kindFilter as ProviderConnectionCreate["kind"]),
      status: statusFilter === "all" ? undefined : (statusFilter as "active"),
    }),
    refetchInterval: (q) =>
      q.state.data?.items.some((row) =>
        ["verifying", "reauth_required"].includes(row.status),
      )
        ? 3000
        : false,
  })
  const data = useRetainedData(query.data, query.error)
  const [editing, setEditing] = useState<
      ProviderConnectionPublic | "new" | null
    >(null),
    [detail, setDetail] = useState<ProviderConnectionPublic | null>(null),
    [error, setError] = useState<string>(),
    [busy, setBusy] = useState<string>()
  const manage =
    canManage(scope?.role) &&
    !isForbidden(query.error) &&
    error !== "action_forbidden"
  const verify = useCallback(
    async (row: ProviderConnectionPublic) => {
      if (busy) return
      setBusy(row.id)
      setError(undefined)
      try {
        await ProvidersService.postVerify({
          path: { tenant_id: tenantId!, connection_id: row.id },
        })
      } catch (e) {
        setError(safeError(e))
      } finally {
        await client.invalidateQueries({ queryKey: providerKey(tenantId!) })
        setBusy(undefined)
      }
    },
    [busy, tenantId, client],
  )
  const columns = useMemo<ColumnDef<ProviderConnectionPublic>[]>(
    () => [
      {
        header: "连接",
        cell: ({ row: { original: r } }) => (
          <div className="flex flex-col gap-1">
            <strong>{r.display_name}</strong>
            <Badge variant="outline">{kinds[r.kind] || r.kind}</Badge>
          </div>
        ),
      },
      {
        header: "验证状态",
        cell: ({ row: { original: r } }) => (
          <div>
            <Badge variant={r.status === "active" ? "secondary" : "outline"}>
              {connectionStates[r.status] || r.status}
            </Badge>
            {r.error_code && (
              <p className="text-xs text-muted-foreground">{r.error_code}</p>
            )}
          </div>
        ),
      },
      {
        header: "应用",
        cell: ({ row }) => (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setDetail(row.original)}
          >
            查看已发现应用
          </Button>
        ),
      },
      {
        header: "最近验证",
        cell: ({ row }) =>
          row.original.verified_at
            ? displayTime(row.original.verified_at)
            : "尚未验证",
      },
      {
        header: "操作",
        cell: ({ row: { original: r } }) => (
          <div className="flex gap-1">
            <Button variant="ghost" size="sm" onClick={() => setDetail(r)}>
              查看详情
            </Button>
            {manage && r.status !== "disabled" && (
              <>
                <Button variant="ghost" size="sm" onClick={() => setEditing(r)}>
                  编辑连接
                </Button>
                <Button variant="ghost" size="sm" onClick={() => setBusinessRate(r)}>
                  抽成设置
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!!busy || r.status === "verifying"}
                  onClick={() => void verify(r)}
                >
                  重新验证
                </Button>
              </>
            )}
          </div>
        ),
      },
    ],
    [manage, busy, verify],
  )
  return (
    <div className="flex flex-col gap-4">
      {error && <ProviderError error={error} />}
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          {manage
            ? "首次保存后验证连接；取链时会自动恢复过期会话。账号或密码变更后请更新凭据。"
            : "连接凭据由租户管理员维护。"}
        </p>
        {manage && <Button onClick={() => setEditing("new")}>新增连接</Button>}
      </div>
      <Card className="min-w-0">
        <CardHeader>
          <form
            className="flex flex-wrap items-end gap-3"
            onSubmit={(e) => {
              e.preventDefault()
              setSearch(input.trim())
              paging.reset()
            }}
          >
            <Field className="w-full sm:w-80">
              <FieldLabel htmlFor="provider-connection-search">
                连接名称
              </FieldLabel>
              <Input
                id="provider-connection-search"
                maxLength={255}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder="搜索连接名称"
              />
            </Field>
            <FilterSelect
              label="版权方"
              choices={kinds}
              value={kindFilter}
              onChange={(v) => {
                setKindFilter(v)
                paging.reset()
              }}
            />
            <FilterSelect
              label="验证状态"
              choices={connectionStates}
              value={statusFilter}
              onChange={(v) => {
                setStatusFilter(v)
                paging.reset()
              }}
            />
            <Button type="submit" variant="outline">
              搜索
            </Button>
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                setInput("")
                setSearch("")
                setKindFilter("all")
                setStatusFilter("all")
                paging.reset()
              }}
            >
              清除筛选
            </Button>
          </form>
        </CardHeader>
        <CardContent className="flex min-w-0 flex-col gap-4">
          <ServerTable
            rows={data?.items || []}
            columns={columns}
            loading={query.isPending && !data}
            fetching={query.isFetching}
            error={query.error}
            retry={() => void query.refetch()}
            filtered={
              !!search || kindFilter !== "all" || statusFilter !== "all"
            }
            emptyTitle="尚未添加版权方连接"
          />
          {query.error && data && (
            <p role="status" className="text-sm text-muted-foreground">
              保留上次读取的列表，请重试以获取当前结果。
            </p>
          )}
        </CardContent>
        <CardFooter className="block">
          <Pager
            paging={paging}
            nextCursor={data?.next_cursor}
            total={data?.total}
            busy={query.isFetching}
          />
        </CardFooter>
      </Card>
      {editing && (
        <ConnectionEditor
          key={editing === "new" ? "new" : editing.id}
          tenantId={tenantId!}
          initial={editing === "new" ? undefined : editing}
          onClose={() => setEditing(null)}
        />
      )}{" "}
      {businessRate && <RateSettings tenantId={tenantId!} kind="provider" scopeId={businessRate.id}
        name={businessRate.display_name} onClose={() => setBusinessRate(null)} />}
      {detail && (
        <ManagementSheet
          title="版权方连接详情"
          description="当前租户内的连接与已发现应用"
          dirty={false}
          onClose={() => setDetail(null)}
        >
          <div className="flex flex-col gap-4">
            <h3 className="font-semibold">{detail.display_name}</h3>
            <Identifier value={detail.id} />
            <p>
              {kinds[detail.kind]} · {connectionStates[detail.status]} ·{" "}
              {detail.verified_at
                ? displayTime(detail.verified_at)
                : "尚未验证"}
            </p>
            {detail.error_code && <ProviderError error={detail.error_code} />}
            <Button variant="outline" onClick={() => onLinks(detail.id)}>
              该连接的链接记录
            </Button>
            <Applications
              tenantId={tenantId!}
              connection={detail}
              canEditDefaults={manage}
            />
          </div>
        </ManagementSheet>
      )}
    </div>
  )
}
function ConnectionEditor({
  tenantId,
  initial,
  onClose,
}: {
  tenantId: string
  initial?: ProviderConnectionPublic
  onClose: () => void
}) {
  const client = useQueryClient(),
    [confirmDisable, setConfirmDisable] = useState(false),
    [saved, setSaved] = useState(initial),
    [name, setName] = useState(initial?.display_name || ""),
    [kind, setKind] = useState<ProviderConnectionCreate["kind"]>(
      (initial?.kind as ProviderConnectionCreate["kind"]) || "wangyan",
    ),
    [account, setAccount] = useState(""),
    [portalId, setPortalId] = useState(""),
    [password, setPassword] = useState(""),
    [pending, setPending] = useState(false),
    [error, setError] = useState<string>()
  const dirty =
    name !== (saved?.display_name || "") ||
    !!account ||
    !!portalId ||
    !!password
  const accountKey =
    kind === "jiashu" || kind === "gangganhao"
      ? "username"
      : kind === "duiba" || kind === "minglue"
        ? "account"
        : "email"
  const submit = async () => {
    if (pending) return
    setPending(true)
    setError(undefined)
    let row = saved
    try {
      const credentials = password
        ? {
            [accountKey]: account,
            ...(kind === "gangganhao" ? { portal_id: portalId } : {}),
            password,
          }
        : undefined
      setPassword("")
      if (row) {
        row = (
          await ProvidersService.patchConnection({
            path: { tenant_id: tenantId, connection_id: row.id },
            body: { display_name: name, credentials },
          })
        ).data
      } else {
        row = (
          await ProvidersService.postConnection({
            path: { tenant_id: tenantId },
            body: {
              kind,
              display_name: name,
              credentials: credentials!,
            },
          })
        ).data
      }
      setSaved(row)
      await client.invalidateQueries({ queryKey: providerKey(tenantId) })
      await ProvidersService.postVerify({
        path: { tenant_id: tenantId, connection_id: row!.id },
      })
      await client.invalidateQueries({ queryKey: providerKey(tenantId) })
      onClose()
    } catch (e) {
      setError(safeError(e))
      await client.invalidateQueries({ queryKey: providerKey(tenantId) })
    } finally {
      setPassword("")
      setPending(false)
    }
  }
  return (
    <ManagementSheet
      title={initial ? "编辑版权方连接" : "新增版权方连接"}
      description="已保存凭据不会回显。验证只作用于当前连接。"
      dirty={dirty}
      pending={pending}
      onClose={onClose}
      actions={
        <Button
          type="submit"
          form="provider-connection-form"
          disabled={pending}
        >
          {pending ? "正在验证连接…" : "保存并验证"}
        </Button>
      }
    >
      <form
        id="provider-connection-form"
        onSubmit={(e) => {
          e.preventDefault()
          e.stopPropagation()
          void submit()
        }}
      >
        <FieldGroup>
          {error && <ProviderError error={error} />}
          <Field>
            <FieldLabel htmlFor="provider-name">连接名称</FieldLabel>
            <Input
              id="provider-name"
              required
              maxLength={255}
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={pending}
            />
          </Field>
          <Field>
            <FieldLabel>版权方</FieldLabel>
            <Select
              value={kind}
              disabled={!!saved || pending}
              onValueChange={(v) => {
                setKind(v as ProviderConnectionCreate["kind"])
                setAccount("")
                setPortalId("")
                setPassword("")
              }}
            >
              <SelectTrigger aria-label="版权方">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="wangyan">网眼</SelectItem>
                  <SelectItem value="jiashu">嘉书</SelectItem>
                  <SelectItem value="duiba">兑吧</SelectItem>
                  <SelectItem value="gangganhao">刚刚好</SelectItem>
                  <SelectItem value="rongliang">容量</SelectItem>
                  <SelectItem value="minglue">明略</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          {saved && <FieldDescription>连接 ID：{saved.id}</FieldDescription>}
          {kind === "gangganhao" && (
            <Field>
              <FieldLabel htmlFor="provider-portal-id">门户 ID</FieldLabel>
              <Input
                id="provider-portal-id"
                autoComplete="off"
                required={!saved || !!password}
                value={portalId}
                disabled={pending}
                onChange={(e) => setPortalId(e.target.value)}
              />
            </Field>
          )}
          <Field>
            <FieldLabel htmlFor="provider-account">
              {kind === "jiashu" || kind === "gangganhao"
                ? "用户名"
                : kind === "duiba" || kind === "minglue"
                  ? "账号"
                  : "邮箱"}
            </FieldLabel>
            <Input
              id="provider-account"
              type={
                kind === "wangyan" || kind === "rongliang" ? "email" : "text"
              }
              autoComplete="off"
              required={!saved || !!password}
              value={account}
              disabled={pending}
              onChange={(e) => setAccount(e.target.value)}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="provider-password">密码</FieldLabel>
            <Input
              id="provider-password"
              type="password"
              autoComplete="new-password"
              required={!saved || !!account}
              value={password}
              disabled={pending}
              onChange={(e) => setPassword(e.target.value)}
            />
            <FieldDescription>
              {saved
                ? "留空沿用已保存的凭据；更新时请同时填写账号与密码。"
                : "密码仅用于当前连接认证，提交后清空。"}
            </FieldDescription>
          </Field>
        </FieldGroup>
      </form>
      {initial && initial.status !== "disabled" && (
        <Button
          className="mt-6"
          variant="destructive"
          disabled={pending}
          onClick={() => setConfirmDisable(true)}
        >
          停用连接
        </Button>
      )}
      <Dialog
        open={confirmDisable}
        onOpenChange={(open) => {
          if (!pending) setConfirmDisable(open)
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>停用版权方连接？</DialogTitle>
            <DialogDescription>
              停用后不能重新启用或更新认证信息；已有链接记录保留。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              disabled={pending}
              onClick={() => setConfirmDisable(false)}
            >
              保留连接
            </Button>
            <Button
              variant="destructive"
              disabled={pending}
              onClick={async () => {
                setPending(true)
                try {
                  await ProvidersService.patchConnection({
                    path: { tenant_id: tenantId, connection_id: initial!.id },
                    body: { status: "disabled" },
                  })
                  await client.invalidateQueries({
                    queryKey: providerKey(tenantId),
                  })
                  onClose()
                } catch (e) {
                  setError(safeError(e))
                } finally {
                  setPending(false)
                }
              }}
            >
              确认停用
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </ManagementSheet>
  )
}
export function Applications({
  tenantId,
  connection,
  onSelect,
  canEditDefaults = false,
}: {
  tenantId: string
  connection: ProviderConnectionPublic
  onSelect?: (application: ProviderApplicationPublic) => void
  canEditDefaults?: boolean
}) {
  const paging = useCursorPage(),
    query = useQuery(
      applicationsQuery(tenantId, connection.id, paging.cursor, paging.limit),
    ),
    data = useRetainedData(query.data, query.error)
  const columns = useMemo<ColumnDef<ProviderApplicationPublic>[]>(
    () => [
      {
        header: "应用",
        cell: ({ row }) => (
          <div>
            <strong>{row.original.name}</strong>
            <Identifier value={row.original.external_id} />
          </div>
        ),
      },
      {
        header: "可用性",
        cell: ({ row }) => (row.original.available ? "可用" : "不可用"),
      },
      ...(onSelect
        ? [
            {
              header: "操作",
              cell: ({
                row,
              }: {
                row: { original: ProviderApplicationPublic }
              }) => (
                <Button
                  disabled={
                    !row.original.available ||
                    !["active", "reauth_required"].includes(connection.status)
                  }
                  onClick={() => onSelect(row.original)}
                >
                  使用此应用
                </Button>
              ),
            },
          ]
        : []),
      // 取链默认值只在连接管理视图提供，且仅有管理权限的成员可见（后端另行强制 403）。
      ...(!onSelect && canEditDefaults
        ? [
            {
              header: "取链默认值",
              cell: ({
                row,
              }: {
                row: { original: ProviderApplicationPublic }
              }) => (
                <ApplicationLinkDefaults
                  tenantId={tenantId}
                  connection={connection}
                  application={row.original}
                />
              ),
            },
          ]
        : []),
    ],
    [onSelect, connection, tenantId, canEditDefaults],
  )
  return (
    <section className="flex min-w-0 flex-col gap-4">
      <h3 className="font-semibold">已发现应用</h3>
      <ServerTable
        rows={data?.items || []}
        columns={columns}
        loading={query.isPending && !data}
        fetching={query.isFetching}
        error={query.error}
        retry={() => void query.refetch()}
        filtered={false}
        emptyTitle={
          connection.status === "pending"
            ? "连接尚未验证"
            : connection.status === "active"
              ? "已验证，但没有可见应用"
              : "连接尚无可用应用，请检查验证状态"
        }
      />
      <Pager
        paging={paging}
        nextCursor={data?.next_cursor}
        total={data?.total}
        busy={query.isFetching}
      />
    </section>
  )
}
