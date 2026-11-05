import { useQuery } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { AxiosError } from "axios"
import { useEffect, useRef, useState } from "react"
import { flushSync } from "react-dom"
import {
  BuildsService,
  type DraftSummary,
  type LinkConfigSchema,
  type ProviderConnectionPublic,
  ProvidersService,
  StrategiesService,
  type StrategyPublic,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import {
  Field,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { Textarea } from "@/components/ui/textarea"
import { AccountPicker } from "@/features/accounts/AccountPicker"
import { capabilitiesQuery } from "@/features/providers/queries"
import { versionQuery } from "@/features/strategies/queries"
import { DirectoryPicker } from "@/features/tenants/DirectoryPicker"
import { ApplicationPicker } from "./ApplicationPicker"
import { mutationKey } from "./api"
import { BuildStrategySummary } from "./BuildStrategySummary"
import { DraftConflict } from "./DraftConflict"
import { LinkConfigSummary } from "./LinkConfigSummary"
import { ManualLinksDialog } from "./ManualLinksDialog"
import { linksForLines, type NamedManualLink } from "./manualLinks"
import { ProviderLinkConfigFields } from "./ProviderLinkConfigFields"
import {
  BuildError,
  BuildGuard,
  BuildSteps,
  reportError,
  unknownOutcome,
} from "./presentation"

type Values = {
  mediaKind: "VIDEO" | "CAROUSEL"
  customProvider: string
  manualLinks: NamedManualLink[]
  drama: string
  account: string
  connection: string
  application: string
  linkConfig: Record<string, number>
  version: string
}

// 付费应用的支付模板既无本次调整也无应用默认时，提交前提示，避免取链阶段才失败。
const templateMissing = (
  schema: LinkConfigSchema,
  overrides: Record<string, number>,
) =>
  schema.fields.some(
    (field) =>
      field.name === "payment_template_id" &&
      field.required &&
      overrides[field.name] === undefined &&
      schema.application_defaults?.[field.name] === undefined,
  )

type Pending = { requestId: string; prepare: boolean; values: Values }
export function BuildInputPage({
  tenantId,
  bcId,
  write,
  summary: sourceSummary,
  original,
  onSaved,
  onCancel,
}: {
  tenantId: string
  bcId: string
  write: boolean
  summary?: DraftSummary
  original?: {
    drama: string[]
    account: string[]
    manualLinks?: NamedManualLink[]
  }
  onSaved?: (prepare: boolean) => void
  onCancel?: () => void
}) {
  const [copyOther, setCopyOther] = useState(false)
  const summary = copyOther ? undefined : sourceSummary
  const key = summary
    ? mutationKey(tenantId, bcId, summary.draft_id)
    : `build-create:${tenantId}:${bcId}`
  const [pending, setPending] = useState<Pending | null>(() => {
    try {
      const value = JSON.parse(sessionStorage.getItem(key) || "null")
      return value && typeof value.requestId === "string" ? value : null
    } catch {
      return null
    }
  })
  const initial = useRef<Values>({
    mediaKind: summary?.media_kind ?? "VIDEO",
    drama: original?.drama.join("\n") || "",
    account: original?.account.join("\n") || "",
    connection: summary?.custom_provider_name
      ? "other"
      : summary?.provider_connection_id || "",
    customProvider: summary?.custom_provider_name || "",
    manualLinks: original?.manualLinks || [],
    application: summary?.application_id || "",
    linkConfig: (summary?.link_config || {}) as Record<string, number>,
    version: summary?.strategy_version_id || "",
  })
  const [values, setValues] = useState<Values>({
      ...initial.current,
      ...pending?.values,
    }),
    [labels, setLabels] = useState<Record<string, string>>({}),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(),
    [forbidden, setForbidden] = useState(false),
    [leaving, setLeaving] = useState(false),
    [revision, setRevision] = useState(summary?.revision),
    [manualOpen, setManualOpen] = useState(false),
    [inputError, setInputError] = useState(""),
    [adjusting, setAdjusting] = useState(
      () => Object.keys(summary?.link_config || {}).length > 0,
    )
  const controller = useRef(new AbortController()),
    navigate = useNavigate()
  useEffect(() => {
    const ctrl = new AbortController()
    controller.current = ctrl
    return () => ctrl.abort()
  }, [])
  const version = useQuery({
    ...versionQuery(tenantId, values.version),
    enabled: !!values.version,
  })
  const capabilities = useQuery({
    ...capabilitiesQuery(tenantId, values.connection, values.application),
    enabled:
      values.connection !== "other" &&
      !!values.connection &&
      !!values.application,
  })
  const dirty = JSON.stringify(values) !== JSON.stringify(initial.current)
  const disabled = busy || !!pending || !write || forbidden
  const change = (patch: Partial<Values>) =>
    setValues((v) => ({ ...v, ...patch }))
  async function finish(draftId: string, prepare: boolean) {
    if (prepare)
      sessionStorage.setItem(`build-start:${tenantId}:${bcId}:${draftId}`, "1")
    sessionStorage.removeItem(key)
    setPending(null)
    flushSync(() => setLeaving(true))
    await navigate({
      to: "/tenants/$tenantId/build-drafts/$draftId",
      params: { tenantId, draftId },
      search: { bc_id: bcId, prepare: prepare ? true : undefined },
    })
  }
  async function recover() {
    if (!pending || busy) return
    setBusy(true)
    setError(undefined)
    try {
      const { data } = await (summary
        ? BuildsService.savedMutation
        : BuildsService.savedRequest)({
        path: { tenant_id: tenantId, request_id: pending.requestId },
        signal: controller.current.signal,
      })
      if (!controller.current.signal.aborted) {
        if (summary) {
          sessionStorage.removeItem(key)
          setPending(null)
          flushSync(() => setLeaving(true))
          onSaved?.(pending.prepare && write && !forbidden)
        } else
          await finish(data.draft_id, pending.prepare && write && !forbidden)
      }
    } catch (e) {
      if (!controller.current.signal.aborted) {
        reportError(e)
        setError(e)
      }
    } finally {
      setBusy(false)
    }
  }
  async function save(prepare: boolean) {
    if (
      disabled ||
      !values.version ||
      !values.connection ||
      (values.connection === "other"
        ? !values.customProvider.trim()
        : !values.application) ||
      !values.drama.trim() ||
      !values.account.trim()
    )
      return
    let manualLinks: ReturnType<typeof linksForLines>
    try {
      manualLinks = linksForLines(values.drama, values.manualLinks)
      setInputError("")
    } catch (e) {
      setInputError(e instanceof Error ? e.message : "请检查剧目与链接")
      return
    }
    setBusy(true)
    setError(undefined)
    try {
      if (summary) {
        // 编辑旧草稿后统一使用 BC 配置；原预览和已执行任务的冻结授权不变。
        if (!dirty && !summary.execution_connection_id) {
          onSaved?.(prepare)
          return
        }
        const operation = {
          requestId: crypto.randomUUID(),
          kind: "input",
          prepare,
          values,
        }
        sessionStorage.setItem(key, JSON.stringify(operation))
        setPending(operation)
        try {
          await BuildsService.update({
            path: { tenant_id: tenantId, draft_id: summary.draft_id },
            body: {
              request_id: operation.requestId,
              expected_revision: revision!,
              strategy_version_id: values.version,
              provider_connection_id:
                values.connection === "other" ? null : values.connection,
              custom_provider_name:
                values.connection === "other"
                  ? values.customProvider.trim()
                  : null,
              manual_links: manualLinks,
              execution_connection_id: null,
              application_id:
                values.connection === "other" ? null : values.application,
              link_config: values.linkConfig,
              drama_lines: values.drama.split("\n"),
              account_lines: values.account.split("\n"),
            },
            signal: controller.current.signal,
          })
          if (!controller.current.signal.aborted) {
            sessionStorage.removeItem(key)
            setPending(null)
            flushSync(() => setLeaving(true))
            onSaved?.(prepare)
          }
        } catch (e) {
          if (!unknownOutcome(e)) {
            sessionStorage.removeItem(key)
            setPending(null)
          }
          throw e
        }
      } else {
        const operation = { requestId: crypto.randomUUID(), prepare, values }
        sessionStorage.setItem(key, JSON.stringify(operation))
        setPending(operation)
        try {
          const { data } = await BuildsService.create({
            path: { tenant_id: tenantId },
            body: {
              request_id: operation.requestId,
              bc_id: bcId,
              media_kind: values.mediaKind,
              strategy_version_id: values.version,
              provider_connection_id:
                values.connection === "other" ? null : values.connection,
              custom_provider_name:
                values.connection === "other"
                  ? values.customProvider.trim()
                  : null,
              manual_links: manualLinks,
              execution_connection_id: null,
              application_id:
                values.connection === "other" ? null : values.application,
              drama_lines: values.drama.split("\n"),
              account_lines: values.account.split("\n"),
              link_config: values.linkConfig,
            },
            signal: controller.current.signal,
          })
          if (!controller.current.signal.aborted)
            await finish(data.draft_id, prepare)
        } catch (e) {
          if (!unknownOutcome(e)) {
            sessionStorage.removeItem(key)
            setPending(null)
          }
          throw e
        }
      }
    } catch (e) {
      if (!controller.current.signal.aborted) {
        reportError(e)
        setError(e)
        if ((e as { response?: { status: number } }).response?.status === 403)
          setForbidden(true)
      }
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="flex w-full min-w-0 max-w-7xl flex-col gap-6">
      <BuildSteps step={1} />
      <BuildGuard dirty={!leaving && (dirty || busy || !!pending)} />
      {!!error && <BuildError error={error} />}
      {inputError && (
        <Alert variant="destructive">
          <AlertDescription>{inputError}</AlertDescription>
        </Alert>
      )}
      {manualOpen && (
        <ManualLinksDialog
          text={values.drama}
          links={values.manualLinks}
          provider={
            values.connection === "other"
              ? "other"
              : capabilities.data?.provider_kind || ""
          }
          onClose={() => setManualOpen(false)}
          onSave={(rows) =>
            change({
              drama: rows.map((row) => row.title).join("\n"),
              manualLinks: rows.filter(
                (row) =>
                  row.url ||
                  row.material_identifier ||
                  row.external_drama_id ||
                  row.protected_base,
              ),
            })
          }
        />
      )}
      {summary &&
        error instanceof AxiosError &&
        error.response?.status === 409 && (
          <DraftConflict
            tenantId={tenantId}
            draftId={summary.draft_id}
            onApply={(revision) => {
              setRevision(revision)
              setError(undefined)
            }}
          />
        )}{" "}
      {pending && (
        <Alert>
          <AlertDescription>
            <p>正在确认原草稿保存结果。原请求标识已保留，不会重复创建。</p>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => void recover()}
            >
              {summary ? "查询原修改结果" : "查询原保存结果"}
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {!write && <p role="status">当前为只读角色，可以查看已保存草稿。</p>}
      <Card className="min-w-0">
        <CardContent>
          <FieldGroup className="grid min-w-0 gap-6 md:grid-cols-3">
            {sourceSummary && (
              <Button
                variant="outline"
                disabled={disabled || copyOther}
                onClick={() => {
                  setCopyOther(true)
                  change({
                    mediaKind:
                      sourceSummary.media_kind === "CAROUSEL"
                        ? "VIDEO"
                        : "CAROUSEL",
                  })
                }}
              >
                以另一类型新建草稿
              </Button>
            )}
            <FieldSet className="md:col-span-3">
              <FieldLegend variant="label">素材类型</FieldLegend>
              <RadioGroup
                aria-label="素材类型"
                className="flex gap-6"
                value={values.mediaKind}
                disabled={disabled || !!summary}
                onValueChange={(value) =>
                  change({ mediaKind: value as Values["mediaKind"] })
                }
              >
                <Field orientation="horizontal" className="w-auto">
                  <RadioGroupItem id="build-video" value="VIDEO" />
                  <FieldLabel htmlFor="build-video">视频</FieldLabel>
                </Field>
                <Field orientation="horizontal" className="w-auto">
                  <RadioGroupItem id="build-carousel" value="CAROUSEL" />
                  <FieldLabel htmlFor="build-carousel">图片</FieldLabel>
                </Field>
              </RadioGroup>
            </FieldSet>
            <Field>
              <FieldLabel>版权方连接</FieldLabel>
              <DirectoryPicker<ProviderConnectionPublic>
                label="版权方连接"
                selectedId={values.connection}
                valueLabel={
                  values.connection === "other"
                    ? "其他版权方"
                    : labels.connection || values.connection
                }
                alternative={{
                  label: "其他版权方",
                  description: "填写版权方名称，使用已有推广链接。",
                  selected: values.connection === "other",
                  onSelect: () => {
                    change({
                      connection: "other",
                      application: "",
                      linkConfig: {},
                    })
                    setLabels((l) => ({
                      ...l,
                      connection: "",
                      application: "",
                    }))
                  },
                }}
                disabled={disabled}
                queryKey={["tenant", tenantId, "builds", "provider-picker"]}
                load={async (query, cursor, limit, signal) =>
                  (
                    await ProvidersService.listConnections({
                      path: { tenant_id: tenantId },
                      query: { query, cursor, limit, status: "active" },
                      signal,
                    })
                  ).data
                }
                renderItem={(item) => <span>{item.display_name}</span>}
                onSelect={(item) => {
                  change({
                    connection: item.id,
                    application: "",
                    linkConfig: {},
                  })
                  setLabels((l) => ({
                    ...l,
                    connection: item.display_name,
                    application: "",
                  }))
                }}
              />
            </Field>
            {values.connection === "other" ? (
              <Field>
                <FieldLabel htmlFor="custom-provider-name">
                  版权方名称
                </FieldLabel>
                <Input
                  id="custom-provider-name"
                  value={values.customProvider}
                  maxLength={100}
                  disabled={disabled}
                  onChange={(e) => change({ customProvider: e.target.value })}
                  placeholder="填写实际版权方名称"
                />
                <p className="text-xs text-muted-foreground">
                  无需连接后台，使用你提供的推广链接。
                </p>
              </Field>
            ) : (
              <Field>
                <FieldLabel>推广应用</FieldLabel>
                <ApplicationPicker
                  tenantId={tenantId}
                  connectionId={values.connection}
                  value={labels.application || values.application}
                  disabled={disabled || !values.connection}
                  onSelect={(item) => {
                    change({ application: item.external_id })
                    setLabels((l) => ({ ...l, application: item.name }))
                  }}
                />
              </Field>
            )}
            <Field>
              <FieldLabel>投放策略</FieldLabel>
              <DirectoryPicker<StrategyPublic>
                label="投放策略"
                valueLabel={labels.version || values.version}
                disabled={disabled}
                queryKey={["tenant", tenantId, "builds", "strategy-picker"]}
                load={async (query, cursor, limit, signal) =>
                  (
                    await StrategiesService.getStrategies({
                      path: { tenant_id: tenantId },
                      query: { query, cursor, limit, active: true },
                      signal,
                    })
                  ).data
                }
                renderItem={(item) => (
                  <span>
                    {item.name} · v{item.latest_version}
                  </span>
                )}
                onSelect={(item) => {
                  change({ version: item.version_id })
                  setLabels((l) => ({
                    ...l,
                    version: `${item.name} · v${item.latest_version}`,
                  }))
                }}
              />
            </Field>
          </FieldGroup>
          {values.connection !== "other" && capabilities.data && (
            <section className="mt-4 border-t pt-3" aria-label="自动取链参数">
              <div className="flex items-start justify-between gap-4">
                <span className="shrink-0 text-xs text-muted-foreground">
                  取链参数
                </span>
                <div className="min-w-0 flex-1">
                  <LinkConfigSummary
                    schema={capabilities.data}
                    overrides={values.linkConfig}
                    compact
                  />
                </div>
                {capabilities.data.fields.length > 0 && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={disabled}
                    aria-expanded={
                      adjusting ||
                      templateMissing(capabilities.data, values.linkConfig)
                    }
                    onClick={() => setAdjusting((open) => !open)}
                  >
                    {adjusting ? "收起" : "修改"}
                  </Button>
                )}
              </div>
              {templateMissing(capabilities.data, values.linkConfig) && (
                <Alert variant="destructive" className="mt-3">
                  <AlertDescription>
                    付费或混合变现应用需要支付模板，请在版权方连接的推广应用中设置
                  </AlertDescription>
                </Alert>
              )}
              {(adjusting ||
                templateMissing(capabilities.data, values.linkConfig)) && (
                <div className="pt-3">
                  <ProviderLinkConfigFields
                    schema={capabilities.data}
                    value={values.linkConfig}
                    mode="overrides"
                    disabled={disabled}
                    onChange={(linkConfig) => change({ linkConfig })}
                  />
                </div>
              )}
            </section>
          )}
          {version.data && <BuildStrategySummary version={version.data} />}
        </CardContent>
      </Card>
      <div className="grid min-w-0 gap-6 md:grid-cols-2">
        {(["drama", "account"] as const).map((kind) => (
          <Card key={kind} className="min-w-0">
            <CardContent className="flex min-w-0 flex-col gap-3">
              <Field>
                <div className="flex min-h-9 flex-wrap items-center justify-between gap-2">
                  <FieldLabel htmlFor={`build-${kind}`}>
                    {kind === "drama" ? "剧目名称" : "广告账户"}
                  </FieldLabel>
                  {kind === "account" ? (
                    <AccountPicker
                      tenantId={tenantId}
                      bcId={bcId}
                      value={values.account}
                      disabled={disabled}
                      onChange={(account) => change({ account })}
                    />
                  ) : (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={disabled}
                      onClick={() => setManualOpen(true)}
                    >
                      填写更多剧目信息
                    </Button>
                  )}
                </div>
                <Textarea
                  id={`build-${kind}`}
                  className="min-h-44 resize-y font-mono"
                  spellCheck={false}
                  value={values[kind]}
                  disabled={disabled}
                  onChange={(e) => change({ [kind]: e.target.value })}
                />
              </Field>
              {kind === "drama" && (
                <>
                  {!!values.manualLinks.length && (
                    <p className="text-sm">
                      已添加{" "}
                      {
                        values.drama.split("\n").filter((line) => line.trim())
                          .length
                      }{" "}
                      部剧，其中{" "}
                      {values.manualLinks.filter((row) => row.url).length}{" "}
                      部已填写推广链接；
                      {
                        values.manualLinks.filter(
                          (row) => row.material_identifier,
                        ).length
                      }{" "}
                      部已填写素材标识
                    </p>
                  )}
                  {values.connection === "other" && (
                    <p className="text-xs text-muted-foreground">
                      其他版权方需提供推广链接，可在“填写更多剧目信息”中添加。
                    </p>
                  )}
                </>
              )}
              <p className="text-xs text-muted-foreground">
                {values[kind] ? values[kind].split("\n").length : 0}{" "}
                行（输入计数，尚未核实） ·{" "}
                {kind === "drama"
                  ? "每行一部完整剧名；可在“填写更多剧目信息”中补充链接与素材标识。"
                  : "每行一个完整账户 ID 或完整名称；重复账户将自动去重。"}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>
      <Card className="sticky bottom-0 min-w-0">
        <CardContent className="flex min-w-0 flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-muted-foreground">
            {values.drama.split("\n").filter((line) => line.trim()).length} 部剧
            ·{" "}
            {
              new Set(
                values.account
                  .split("\n")
                  .map((line) => line.trim())
                  .filter(Boolean),
              ).size
            }{" "}
            个账户
            <span className="ml-3 text-xs">此步不会创建广告</span>
          </p>
          <div className="flex flex-wrap gap-2">
            {onCancel && (
              <Button variant="outline" disabled={busy} onClick={onCancel}>
                返回准备
              </Button>
            )}
            {write && (
              <>
                <Button
                  variant="outline"
                  disabled={
                    disabled ||
                    !values.version ||
                    (values.connection === "other"
                      ? !values.customProvider.trim()
                      : !values.application) ||
                    !values.drama.trim() ||
                    !values.account.trim()
                  }
                  onClick={() => void save(false)}
                >
                  保存草稿
                </Button>
                <Button
                  disabled={
                    disabled ||
                    !values.version ||
                    (values.connection === "other"
                      ? !values.customProvider.trim()
                      : !values.application) ||
                    !values.drama.trim() ||
                    !values.account.trim() ||
                    // 缺支付模板只阻止准备，草稿仍可保存
                    (capabilities.data
                      ? templateMissing(capabilities.data, values.linkConfig)
                      : false)
                  }
                  onClick={() => void save(true)}
                >
                  {busy ? "正在保存…" : "下一步：准备链接与素材"}
                </Button>
              </>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
