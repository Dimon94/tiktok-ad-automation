import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useBlocker, useNavigate } from "@tanstack/react-router"
import { AxiosError } from "axios"
import { Loader2 } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { toast } from "sonner"
import {
  StrategiesService,
  type StrategyConfig_Output,
  type StrategyPublic,
  type VersionPublic,
} from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
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
  FieldError,
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
import { tiktokCountryCodes } from "@/features/targeting/countries"
import {
  normalizedTargeting,
  TargetingForm,
  targetingError,
} from "@/features/targeting/TargetingForm"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { useWorkspacePageGuard } from "@/features/workspace/WorkspaceTabs"
import { handleApiError } from "@/lib/api-feedback"
import { CopyPoolSheet } from "./CopyPoolSheet"
import { StrategyError as RequestError } from "./feedback"
import {
  copyPoolQuery,
  DEFAULT_COPY_POOL,
  strategyKey,
  strategyQuery,
} from "./queries"
import { StrategyNamingExample } from "./StrategyNamingExample"
import { StrategyStructureExample } from "./StrategyStructureExample"
import {
  configFingerprint,
  DEFAULT_NAME_TEMPLATE,
  decimalError,
  displayNameTemplate,
  issueMessages,
  MAX_FIXED_ADS_PER_GROUP,
  MAX_FIXED_GROUP_COUNT,
  NAME_LABELS,
  nameTemplateError,
  parseNameTemplate,
} from "./validation"

const currencies = (
  Intl as typeof Intl & { supportedValuesOf: (key: string) => string[] }
).supportedValuesOf("currency")
const fieldNames: Record<string, string> = {
  budget: "日预算",
  currency: "预算币种",
  target_roas: "目标 ROAS",
  budget_strategy: "预算策略",
  bid_strategy: "竞价策略",
  creation_status: "创建状态",
  group_generation_mode: "广告组数量规则",
  group_count: "广告组数量",
  group_material_allocation: "广告组素材安排",
  max_materials_per_group: "每组最多素材数",
  ad_generation_mode: "广告数量规则",
  ads_per_group: "每组广告数量",
  ad_material_allocation: "广告素材安排",
  max_materials_per_ad: "每个广告最多素材数",
  creative_count: "每个广告创意数量",
  campaign_name_template: "广告名称格式",
  copy_pool_version: "文案池版本",
  cta_option_ids: "CTA 配置",
  config: "整体配置",
  name: "策略名称",
  targeting: "受众定向",
}
export function StrategyForm({
  strategy,
  version,
  copy = false,
  forceReadonly = false,
}: {
  strategy?: StrategyPublic
  version?: VersionPublic
  copy?: boolean
  forceReadonly?: boolean
}) {
  const { tenantId: currentTenantId, scope } = useTenantScope(),
    client = useQueryClient(),
    navigate = useNavigate()
  const [tenantId] = useState(currentTenantId!)
  const strategyId = copy ? undefined : strategy?.id,
    requestKey = `strategy-save-pending:${tenantId}:${strategyId || "new"}`
  const [initial] = useState(() => version?.config || strategy?.config)
  const [name, setName] = useState(
      copy ? `${strategy?.name || "策略"} 副本` : strategy?.name || "",
    ),
    [budget, setBudget] = useState(initial?.budget || ""),
    // 新建策略固定使用 USD；已有版本按原币种展示，避免隐式改币种或换汇。
    [currency] = useState(initial?.currency || "USD"),
    [roas, setRoas] = useState(initial?.target_roas || ""),
    [budgetStrategy, setBudgetStrategy] = useState<"SERIES" | "ADGROUP">(
      initial?.budget_strategy || "SERIES",
    ),
    [bidStrategy, setBidStrategy] = useState<"HIGHEST_VALUE" | "TARGET_ROAS">(
      initial?.bid_strategy ||
        (initial?.target_roas ? "TARGET_ROAS" : "HIGHEST_VALUE"),
    ),
    [creationStatus, setCreationStatus] = useState<"ENABLE" | "DISABLE">(
      initial?.creation_status || "ENABLE",
    ),
    [groupGenerationMode, setGroupGenerationMode] = useState<
      "FIXED" | "BY_MATERIAL"
    >(initial?.group_generation_mode || "FIXED"),
    [groupCount, setGroupCount] = useState(
      initial?.group_count != null ? String(initial.group_count) : "1",
    ),
    [groupMaterialAllocation, setGroupMaterialAllocation] = useState<
      "SHARED" | "SEQUENTIAL_AVERAGE"
    >(initial?.group_material_allocation || "SHARED"),
    [maxMaterialsPerGroup, setMaxMaterialsPerGroup] = useState(
      initial?.max_materials_per_group != null
        ? String(initial.max_materials_per_group)
        : "",
    ),
    [adGenerationMode, setAdGenerationMode] = useState<"FIXED" | "BY_MATERIAL">(
      initial?.ad_generation_mode || "BY_MATERIAL",
    ),
    [adsPerGroup, setAdsPerGroup] = useState(
      initial?.ads_per_group != null ? String(initial.ads_per_group) : "",
    ),
    [adMaterialAllocation, setAdMaterialAllocation] = useState<
      "SHARED" | "SEQUENTIAL_AVERAGE"
    >(initial?.ad_material_allocation || "SHARED"),
    [maxMaterialsPerAd, setMaxMaterialsPerAd] = useState(
      initial?.max_materials_per_ad != null
        ? String(initial.max_materials_per_ad)
        : "1",
    ),
    [creativeCount, setCreativeCount] = useState(
      initial?.creative_count != null ? String(initial.creative_count) : "1",
    ),
    [nameTemplate, setNameTemplate] = useState(
      initial?.campaign_name_template ?? DEFAULT_NAME_TEMPLATE,
    )
  const [targeting, setTargeting] = useState(() =>
    normalizedTargeting(initial?.targeting),
  )
  const [baseline, setBaseline] = useState(initial),
    [baselineName, setBaselineName] = useState(strategy?.name || ""),
    [baseNumber, setBaseNumber] = useState(
      version?.number || strategy?.latest_version || 0,
    ),
    [pending, setPending] = useState(false),
    [completed, setCompleted] = useState<VersionPublic>(),
    [unknownRequest, setUnknownRequest] = useState<string | null>(() =>
      sessionStorage.getItem(requestKey),
    ),
    [unknownNote, setUnknownNote] = useState(""),
    [error, setError] = useState<unknown>(),
    [serverErrors, setServerErrors] = useState<Record<string, string>>({}),
    [conflict, setConflict] = useState<StrategyPublic>(),
    [showConflict, setShowConflict] = useState(false),
    [poolOpen, setPoolOpen] = useState(false),
    [touched, setTouched] = useState<Record<string, boolean>>({}),
    [denied, setDenied] = useState(false)
  const copyPoolVersion = initial?.copy_pool_version || DEFAULT_COPY_POOL,
    ctaIds = initial?.cta_option_ids || []
  const pool = useQuery(copyPoolQuery(tenantId!, copyPoolVersion))
  const capacity = pool.data
    ? new Set(
        pool.data.entries
          .filter((row) => row.text.trim())
          .map((row) => row.text),
      ).size
    : undefined
  // 结构层只提交当前模式的数量和素材安排；其余字段显式清空，避免保存后残留旧决策。
  const cfg: StrategyConfig_Output = {
    budget,
    currency,
    budget_strategy: budgetStrategy,
    bid_strategy: bidStrategy,
    creation_status: creationStatus,
    target_roas: bidStrategy === "TARGET_ROAS" ? roas : null,
    group_generation_mode: groupGenerationMode,
    group_count: groupGenerationMode === "FIXED" ? Number(groupCount) : null,
    group_material_allocation:
      groupGenerationMode === "FIXED"
        ? Number(groupCount) > 1
          ? groupMaterialAllocation
          : "SHARED"
        : null,
    max_materials_per_group:
      groupGenerationMode === "BY_MATERIAL"
        ? Number(maxMaterialsPerGroup)
        : null,
    ad_generation_mode: adGenerationMode,
    ads_per_group: adGenerationMode === "FIXED" ? Number(adsPerGroup) : null,
    ad_material_allocation:
      adGenerationMode === "FIXED"
        ? Number(adsPerGroup) > 1
          ? adMaterialAllocation
          : "SHARED"
        : null,
    max_materials_per_ad:
      adGenerationMode === "BY_MATERIAL" ? Number(maxMaterialsPerAd) : null,
    creative_count: Number(creativeCount),
    copy_pool_version: copyPoolVersion,
    cta_option_ids: ctaIds,
    campaign_name_template: nameTemplate,
    targeting,
  }
  const readonly =
    forceReadonly ||
    scope?.role === "viewer" ||
    denied ||
    (!copy && strategy?.active === false)
  const configDirty = strategyId
      ? !!baseline && configFingerprint(cfg) !== configFingerprint(baseline)
      : true,
    nameDirty = !!strategyId && name.trim() !== baselineName,
    dirty = strategyId
      ? configDirty || nameDirty
      : !!name ||
        !!budget ||
        !!roas ||
        !!groupCount ||
        !!maxMaterialsPerGroup ||
        !!adsPerGroup ||
        !!maxMaterialsPerAd ||
        !!creativeCount ||
        nameTemplate !== DEFAULT_NAME_TEMPLATE ||
        JSON.stringify(targeting) !== JSON.stringify(normalizedTargeting())
  const local: Record<string, string> = {}
  const targetingIssue = targetingError(targeting)
  if (targetingIssue) local.targeting = targetingIssue
  if (!name.trim()) local.name = "请填写策略名称。"
  if (name.length > 120) local.name = "名称不能超过 120 个字符。"
  const budgetIssue = decimalError(budget),
    roasIssue = decimalError(roas),
    templateIssue = nameTemplateError(nameTemplate)
  if (budgetIssue) local.budget = budgetIssue
  if (roasIssue && bidStrategy === "TARGET_ROAS") local.target_roas = roasIssue
  if (!/^[A-Z]{3}$/.test(currency)) local.currency = "请选择预算币种。"
  const positiveInteger = (value: string) =>
    /^\d+$/.test(value) &&
    Number.isSafeInteger(Number(value)) &&
    Number(value) > 0
  const boundedInteger = (value: string, maximum: number) =>
    positiveInteger(value) && Number(value) <= maximum
  if (
    groupGenerationMode === "FIXED" &&
    !boundedInteger(groupCount, MAX_FIXED_GROUP_COUNT)
  )
    local.group_count = `请输入 1 至 ${MAX_FIXED_GROUP_COUNT} 的整数。`
  if (
    groupGenerationMode === "BY_MATERIAL" &&
    !positiveInteger(maxMaterialsPerGroup)
  )
    local.max_materials_per_group = "请输入大于 0 的整数。"
  if (
    adGenerationMode === "FIXED" &&
    !boundedInteger(adsPerGroup, MAX_FIXED_ADS_PER_GROUP)
  )
    local.ads_per_group = `请输入 1 至 ${MAX_FIXED_ADS_PER_GROUP} 的整数。`
  if (adGenerationMode === "BY_MATERIAL" && !positiveInteger(maxMaterialsPerAd))
    local.max_materials_per_ad = "请输入大于 0 的整数。"
  if (!positiveInteger(creativeCount))
    local.creative_count = "请输入大于 0 的整数。"
  else if (capacity !== undefined && Number(creativeCount) > capacity)
    local.creative_count = `创意数量不能超过 ${capacity} 条有效且不重复的英文文案。`
  if (templateIssue) local.campaign_name_template = templateIssue
  // 规则切换后互斥字段会卸载；过滤动态不可见字段，避免顶部定位和保存按钮被旧错误卡住。
  const visibleFields = new Set([
    "name",
    "budget",
    "currency",
    "budget_strategy",
    "bid_strategy",
    "creation_status",
    "group_generation_mode",
    "ad_generation_mode",
    "creative_count",
    "campaign_name_template",
    "targeting",
    "copy_pool_version",
    "cta_option_ids",
    "config",
  ])
  if (groupGenerationMode === "FIXED") {
    visibleFields.add("group_count")
    if (
      boundedInteger(groupCount, MAX_FIXED_GROUP_COUNT) &&
      Number(groupCount) > 1
    )
      visibleFields.add("group_material_allocation")
  } else {
    visibleFields.add("max_materials_per_group")
  }
  if (adGenerationMode === "FIXED") {
    visibleFields.add("ads_per_group")
    if (
      boundedInteger(adsPerGroup, MAX_FIXED_ADS_PER_GROUP) &&
      Number(adsPerGroup) > 1
    )
      visibleFields.add("ad_material_allocation")
  } else {
    visibleFields.add("max_materials_per_ad")
  }
  if (bidStrategy === "TARGET_ROAS") visibleFields.add("target_roas")
  const errors = Object.fromEntries(
      Object.entries({ ...local, ...serverErrors }).filter(([key]) =>
        visibleFields.has(key),
      ),
    ),
    firstError = Object.keys(errors)[0]
  useEffect(() => {
    if (
      strategy &&
      strategyId &&
      !readonly &&
      !completed &&
      strategy.latest_version !== baseNumber
    )
      setConflict(strategy)
  }, [strategy, strategyId, readonly, completed, baseNumber])
  const retainsPage = useWorkspacePageGuard(
    !completed && (dirty || pending || !!unknownRequest),
    !pending,
  )
  const blockers = useBlocker({
    shouldBlockFn: ({ next }) =>
      !completed &&
      (dirty || pending || !!unknownRequest) &&
      !retainsPage(next),
    enableBeforeUnload: !completed && (dirty || pending || !!unknownRequest),
    withResolver: true,
  })
  const requestController = useRef<AbortController | null>(null),
    inFlight = useRef(false),
    checking = useRef(false)
  useEffect(() => () => requestController.current?.abort(), [])
  const finish = (saved: VersionPublic) => {
    sessionStorage.removeItem(requestKey)
    setUnknownRequest(null)
    setCompleted(saved)
    setPending(false)
    client.setQueryData([...strategyKey(tenantId!), "version", saved.id], saved)
    void client.invalidateQueries({ queryKey: strategyKey(tenantId!) })
    toast.success(`已保存 v${saved.number}`)
  }
  const checkSaved = async (id: string) => {
    if (checking.current) return
    checking.current = true
    requestController.current ||= new AbortController()
    setPending(true)
    setUnknownNote("")
    try {
      const { data } = await StrategiesService.savedRequest({
        path: { tenant_id: tenantId!, request_id: id },
        signal: requestController.current?.signal,
      })
      if (data?.request_id === id) finish(data)
      else setUnknownNote("返回记录尚不能证明本次保存，请继续确认。")
    } catch (e) {
      if (e instanceof Error) handleApiError(e)
      setUnknownNote("暂未确认这次保存记录。请继续回查，不会再次提交保存请求。")
    } finally {
      checking.current = false
      setPending(false)
    }
  }
  const checkRef = useRef(checkSaved)
  checkRef.current = checkSaved
  useEffect(() => {
    if (unknownRequest) void checkRef.current(unknownRequest)
  }, [unknownRequest])
  useEffect(() => {
    if (completed)
      void navigate({
        to: "/tenants/$tenantId/strategies/$strategyId",
        params: { tenantId: tenantId!, strategyId: completed.strategy_id },
        search: { bc_id: scope?.bcId || undefined, version_id: completed.id },
        replace: true,
      })
  }, [completed, navigate, tenantId, scope?.bcId])
  const save = async () => {
    if (
      inFlight.current ||
      readonly ||
      unknownRequest ||
      !dirty ||
      Object.keys(errors).length ||
      (configDirty && capacity === undefined)
    )
      return
    inFlight.current = true
    setPending(true)
    setError(undefined)
    setServerErrors({})
    requestController.current = new AbortController()
    let attempted = false
    try {
      if (configDirty) {
        const validation = (
          await StrategiesService.validate({
            path: { tenant_id: tenantId! },
            body: cfg,
            signal: requestController.current.signal,
          })
        ).data!
        if (!validation.valid) {
          setServerErrors(
            Object.fromEntries(
              validation.errors.map((issue) => [
                issue.field || "config",
                issueMessages[issue.code] ||
                  (issue.field === "config"
                    ? "整体策略配置无效，请检查各项生成规则和互斥字段。"
                    : "该字段未通过策略校验。"),
              ]),
            ),
          )
          return
        }
      }
      if (strategyId && nameDirty && !configDirty) {
        const normalizedName = name.trim()
        const { data: renamed } = await StrategiesService.setState({
          path: { tenant_id: tenantId!, strategy_id: strategyId },
          body: { name: normalizedName },
          signal: requestController.current.signal,
        })
        if (!renamed) throw new Error("策略名称更新未返回结果")
        setName(renamed.name)
        setBaselineName(renamed.name)
        client.setQueryData(
          strategyQuery(tenantId!, strategyId).queryKey,
          renamed,
        )
        void client.invalidateQueries({ queryKey: strategyKey(tenantId!) })
        if (!configDirty) {
          toast.success("策略名称已更新")
          return
        }
      }
      const requestId = crypto.randomUUID()
      sessionStorage.setItem(requestKey, requestId)
      attempted = true
      const { data } = strategyId
        ? await StrategiesService.append({
            path: { tenant_id: tenantId!, strategy_id: strategyId },
            body: {
              ...(nameDirty ? { name: name.trim() } : {}),
              config: cfg,
              expected_version: baseNumber,
              request_id: requestId,
            },
            signal: requestController.current.signal,
          })
        : await StrategiesService.create({
            path: { tenant_id: tenantId! },
            body: { name: name.trim(), config: cfg, request_id: requestId },
            signal: requestController.current.signal,
          })
      if (!data || data.request_id !== requestId) {
        setUnknownRequest(requestId)
        return
      }
      if (strategyId && nameDirty) {
        setName(name.trim())
        setBaselineName(name.trim())
      }
      finish(data)
    } catch (e) {
      const status = e instanceof AxiosError ? e.response?.status : undefined
      if (
        attempted &&
        (!status || status >= 500 || status === 408 || status === 429)
      ) {
        setUnknownRequest(sessionStorage.getItem(requestKey))
        return
      }
      sessionStorage.removeItem(requestKey)
      setError(e)
      if (e instanceof Error) handleApiError(e)
      if (status === 403) {
        setDenied(true)
        void client.invalidateQueries({
          queryKey: ["tenant", tenantId, "scope"],
        })
      }
      const code = e instanceof AxiosError ? e.response?.data?.code : undefined
      if (status === 409 && strategyId) {
        try {
          setConflict(
            await client.fetchQuery({
              ...strategyQuery(tenantId!, strategyId),
              staleTime: 0,
            }),
          )
        } catch {
          /* Original input and error remain visible. */
        }
      }
      if (code === "copy_pool_exhausted")
        setServerErrors({ creative_count: issueMessages[code] })
      if (code === "invalid_name_template")
        setServerErrors({ campaign_name_template: issueMessages[code] })
    } finally {
      inFlight.current = false
      setPending(false)
    }
  }
  const change =
    (key: string, setter: (value: string) => void) => (value: string) => {
      setter(value)
      setTouched((old) => ({ ...old, [key]: true }))
      setServerErrors((old) => {
        const next = { ...old }
        delete next[key]
        delete next.config
        return next
      })
    }
  const invalid = (key: string) =>
    !!errors[key] && (!!touched[key] || !!initial || !!serverErrors[key])
  const input = (
    key: string,
    value: string,
    setter: (value: string) => void,
    description?: string,
  ) => (
    <Field data-invalid={invalid(key)}>
      <FieldLabel htmlFor={`strategy-${key}`}>{fieldNames[key]}</FieldLabel>
      <Input
        id={`strategy-${key}`}
        value={value}
        onChange={(e) => change(key, setter)(e.target.value)}
        disabled={pending || !!unknownRequest}
        readOnly={readonly}
        aria-invalid={invalid(key)}
        aria-describedby={`strategy-${key}-help`}
        inputMode={
          ["budget", "target_roas"].includes(key)
            ? "decimal"
            : [
                  "group_count",
                  "max_materials_per_group",
                  "ads_per_group",
                  "max_materials_per_ad",
                  "creative_count",
                ].includes(key)
              ? "numeric"
              : undefined
        }
      />
      <FieldDescription id={`strategy-${key}-help`}>
        {description}
      </FieldDescription>
      {invalid(key) && <FieldError>{errors[key]}</FieldError>}
    </Field>
  )
  const selectField = (
    key: string,
    value: string,
    setter: (value: string) => void,
    options: Array<{ value: string; label: string }>,
    description?: string,
  ) => (
    <Field data-invalid={invalid(key)}>
      <FieldLabel htmlFor={`strategy-${key}`}>{fieldNames[key]}</FieldLabel>
      <Select
        value={value}
        onValueChange={(next) => {
          setter(next)
          setTouched((old) => ({ ...old, [key]: true }))
          setServerErrors((old) => {
            const copy = { ...old }
            delete copy[key]
            delete copy.config
            return copy
          })
        }}
        disabled={pending || !!unknownRequest || readonly}
      >
        <SelectTrigger id={`strategy-${key}`} aria-invalid={invalid(key)}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            {options.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectGroup>
        </SelectContent>
      </Select>
      <FieldDescription>{description}</FieldDescription>
      {invalid(key) && <FieldError>{errors[key]}</FieldError>}
    </Field>
  )
  return (
    <>
      <div className="flex w-full max-w-7xl flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <WorkspacePageTitle>
            {strategyId
              ? readonly
                ? "策略版本详情"
                : "编辑投放策略"
              : "新建投放策略"}
          </WorkspacePageTitle>
          <p className="text-sm text-muted-foreground">
            当前租户策略 · {strategy?.name || "新策略"}{" "}
            {baseNumber > 0 && (
              <Badge variant="outline">
                {copy ? "复制来源" : "来源版本"} v{baseNumber}
              </Badge>
            )}
          </p>
        </div>
        {strategyId && !readonly && (
          <Button
            className="self-start"
            variant="outline"
            size="sm"
            disabled={pending || !!unknownRequest}
            onClick={() =>
              void client.invalidateQueries({
                queryKey: strategyQuery(tenantId, strategyId).queryKey,
              })
            }
          >
            检查最新版本
          </Button>
        )}
      </div>
      {!copy && strategy?.active === false && (
        <Alert>
          <AlertTitle>策略已停用</AlertTitle>
          <AlertDescription>
            可在列表恢复可用。已提交任务不受影响。
          </AlertDescription>
        </Alert>
      )}
      {unknownRequest && (
        <Alert>
          <AlertTitle>保存结果待确认</AlertTitle>
          <AlertDescription>
            <p>{unknownNote || "正在按本次保存请求 ID 精确回查结果…"}</p>
            <p className="break-all text-xs">请求 ID：{unknownRequest}</p>
            <Button
              variant="outline"
              disabled={pending}
              onClick={() => void checkSaved(unknownRequest)}
            >
              确认保存结果
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {!!error && !conflict && <RequestError error={error} />}
      {conflict && (
        <Alert variant="destructive">
          <AlertTitle>服务器版本已变化</AlertTitle>
          <AlertDescription>
            <p>服务器当前为 v{conflict.latest_version}，本地输入已保留。</p>
            <Button variant="outline" onClick={() => setShowConflict(true)}>
              查看服务器差异
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {dirty && firstError && (
        <Alert variant="destructive">
          <AlertTitle>请检查策略字段</AlertTitle>
          <AlertDescription>
            <Button
              variant="link"
              className="h-auto max-w-full whitespace-normal px-0 text-left"
              onClick={() => {
                setTouched((old) => ({ ...old, [firstError]: true }))
                // 整体配置错误没有对应的 DOM 字段；有元素时才定位，避免 focus 不存在节点。
                document.getElementById(`strategy-${firstError}`)?.focus()
              }}
            >
              {fieldNames[firstError] || firstError}：{errors[firstError]}
            </Button>
          </AlertDescription>
        </Alert>
      )}
      <div className="grid w-full min-w-0 max-w-7xl items-start gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
        <form
          id="strategy-form"
          noValidate
          className="flex min-w-0 flex-col gap-6 pb-6"
          onSubmit={(e) => {
            e.preventDefault()
            void save()
          }}
        >
          <Card>
            <CardHeader>
              <CardTitle>基本信息</CardTitle>
              <CardDescription>
                每次有效修改保存为新的不可变版本。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <FieldGroup>
                {input(
                  "name",
                  name,
                  setName,
                  strategyId
                    ? "名称是显示标签；修改后历史草稿和任务列表会显示新名称。"
                    : "填写租户内便于识别的策略名称。",
                )}
              </FieldGroup>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>搭建结构</CardTitle>
              <CardDescription>
                先设置广告组如何拆分素材，再设置每个广告组内广告如何生成和分配素材。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="space-y-8">
                <section
                  aria-labelledby="strategy-ad-group-structure"
                  className="space-y-4"
                >
                  <div>
                    <h3
                      id="strategy-ad-group-structure"
                      className="font-medium text-sm"
                    >
                      广告组
                    </h3>
                    <p className="text-sm text-muted-foreground">
                      决定一个系列拆成几个广告组，以及素材如何分给各组。
                    </p>
                  </div>
                  <FieldGroup className="sm:grid sm:grid-cols-2">
                    {selectField(
                      "group_generation_mode",
                      groupGenerationMode,
                      (value) => {
                        const next = value as "FIXED" | "BY_MATERIAL"
                        setGroupGenerationMode(next)
                        setServerErrors((old) => {
                          const nextErrors = { ...old }
                          delete nextErrors.group_count
                          delete nextErrors.max_materials_per_group
                          delete nextErrors.group_material_allocation
                          delete nextErrors.config
                          return nextErrors
                        })
                        if (next === "FIXED") {
                          setGroupCount((old) =>
                            boundedInteger(old, MAX_FIXED_GROUP_COUNT)
                              ? old
                              : "1",
                          )
                          setGroupMaterialAllocation("SHARED")
                          setMaxMaterialsPerGroup("")
                        } else {
                          setGroupCount("")
                          setGroupMaterialAllocation("SHARED")
                        }
                      },
                      [
                        { value: "FIXED", label: "固定数量" },
                        { value: "BY_MATERIAL", label: "按素材数量" },
                      ],
                      "固定数量按组数创建；按素材数量按顺序拆分素材。",
                    )}
                    {groupGenerationMode === "FIXED" &&
                      input(
                        "group_count",
                        groupCount,
                        setGroupCount,
                        "每个系列创建的广告组数量。",
                      )}
                    {groupGenerationMode === "BY_MATERIAL" &&
                      input(
                        "max_materials_per_group",
                        maxMaterialsPerGroup,
                        setMaxMaterialsPerGroup,
                        "每组最多使用的素材数量；按素材顺序拆分。",
                      )}
                    {groupGenerationMode === "FIXED" &&
                      Number(groupCount) > 1 &&
                      selectField(
                        "group_material_allocation",
                        groupMaterialAllocation,
                        (value) =>
                          setGroupMaterialAllocation(
                            value as "SHARED" | "SEQUENTIAL_AVERAGE",
                          ),
                        [
                          { value: "SHARED", label: "共用全部素材" },
                          {
                            value: "SEQUENTIAL_AVERAGE",
                            label: "按顺序平均分配",
                          },
                        ],
                        "平均分配在搭建预览中按素材顺序计算。",
                      )}
                  </FieldGroup>
                </section>
                <section
                  aria-labelledby="strategy-ad-structure"
                  className="space-y-4 border-t pt-6"
                >
                  <div>
                    <h3
                      id="strategy-ad-structure"
                      className="font-medium text-sm"
                    >
                      广告
                    </h3>
                    <p className="text-sm text-muted-foreground">
                      决定每个广告组创建几个广告，以及组内素材如何分配；创意数量会复制基础广告。
                    </p>
                  </div>
                  <FieldGroup className="sm:grid sm:grid-cols-2">
                    {selectField(
                      "ad_generation_mode",
                      adGenerationMode,
                      (value) => {
                        const next = value as "FIXED" | "BY_MATERIAL"
                        setAdGenerationMode(next)
                        setServerErrors((old) => {
                          const nextErrors = { ...old }
                          delete nextErrors.ads_per_group
                          delete nextErrors.max_materials_per_ad
                          delete nextErrors.ad_material_allocation
                          delete nextErrors.config
                          return nextErrors
                        })
                        if (next === "FIXED") {
                          setAdsPerGroup((old) =>
                            boundedInteger(old, MAX_FIXED_ADS_PER_GROUP)
                              ? old
                              : "1",
                          )
                          setAdMaterialAllocation("SHARED")
                          setMaxMaterialsPerAd("")
                        } else {
                          setAdsPerGroup("")
                          setAdMaterialAllocation("SHARED")
                        }
                      },
                      [
                        { value: "FIXED", label: "固定数量" },
                        { value: "BY_MATERIAL", label: "按素材数量" },
                      ],
                      "固定数量按广告数创建；按素材数量按上限拆分。",
                    )}
                    {adGenerationMode === "FIXED" &&
                      input(
                        "ads_per_group",
                        adsPerGroup,
                        setAdsPerGroup,
                        "每个广告组创建的广告数量。",
                      )}
                    {adGenerationMode === "BY_MATERIAL" &&
                      input(
                        "max_materials_per_ad",
                        maxMaterialsPerAd,
                        setMaxMaterialsPerAd,
                        "M 为每广告最多创意单元数；一个视频或完整轮播为一个单元。",
                      )}
                    {adGenerationMode === "FIXED" &&
                      Number(adsPerGroup) > 1 &&
                      selectField(
                        "ad_material_allocation",
                        adMaterialAllocation,
                        (value) =>
                          setAdMaterialAllocation(
                            value as "SHARED" | "SEQUENTIAL_AVERAGE",
                          ),
                        [
                          { value: "SHARED", label: "共用本组素材" },
                          {
                            value: "SEQUENTIAL_AVERAGE",
                            label: "按顺序平均分配",
                          },
                        ],
                        "平均分配在搭建预览中按本组素材顺序计算。",
                      )}
                    {input(
                      "creative_count",
                      creativeCount,
                      setCreativeCount,
                      "K 为每基础广告的文案版本数；每个基础广告生成 K 条广告，复用相同的完整创意单元。",
                    )}
                  </FieldGroup>
                </section>
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>预算与出价</CardTitle>
              <CardDescription>
                选择预算归属和竞价方式；目标 ROAS 仅在对应竞价策略下填写。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <FieldGroup className="sm:grid sm:grid-cols-2">
                {selectField(
                  "budget_strategy",
                  budgetStrategy,
                  (value) => setBudgetStrategy(value as "SERIES" | "ADGROUP"),
                  [
                    { value: "SERIES", label: "系列预算" },
                    { value: "ADGROUP", label: "组预算" },
                  ],
                )}
                {selectField(
                  "bid_strategy",
                  bidStrategy,
                  (value) => {
                    const next = value as "HIGHEST_VALUE" | "TARGET_ROAS"
                    setBidStrategy(next)
                    setServerErrors((old) => {
                      const nextErrors = { ...old }
                      delete nextErrors.target_roas
                      delete nextErrors.config
                      return nextErrors
                    })
                    if (next === "HIGHEST_VALUE") setRoas("")
                  },
                  [
                    { value: "HIGHEST_VALUE", label: "最高价值" },
                    { value: "TARGET_ROAS", label: "目标 ROAS" },
                  ],
                )}
                {input(
                  "budget",
                  budget,
                  setBudget,
                  "按日预算金额保存；不会自动换算币种。",
                )}
                <Field data-disabled data-invalid={invalid("currency")}>
                  <FieldLabel htmlFor="strategy-currency">预算币种</FieldLabel>
                  <Select value={currency} disabled>
                    <SelectTrigger
                      id="strategy-currency"
                      aria-label="预算币种"
                      aria-invalid={invalid("currency")}
                    >
                      <SelectValue placeholder="选择币种" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        {Array.from(
                          new Set([
                            ...currencies,
                            ...(currency ? [currency] : []),
                          ]),
                        )
                          .sort()
                          .map((code) => (
                            <SelectItem key={code} value={code}>
                              {code}
                            </SelectItem>
                          ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                  <FieldDescription>
                    暂不支持切换币种；须与目标账户一致，不自动换算。
                  </FieldDescription>
                  {invalid("currency") && (
                    <FieldError>{errors.currency}</FieldError>
                  )}
                </Field>
                {bidStrategy === "TARGET_ROAS" &&
                  input(
                    "target_roas",
                    roas,
                    setRoas,
                    "目标倍率，例如 1.08 倍；不表示百分比。",
                  )}
              </FieldGroup>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>创建状态</CardTitle>
              <CardDescription>
                控制广告对象创建后是启用还是停用；本次投放排期请在广告搭建页面设置。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <FieldGroup className="sm:grid sm:grid-cols-2">
                {selectField(
                  "creation_status",
                  creationStatus,
                  (value) => setCreationStatus(value as "ENABLE" | "DISABLE"),
                  [
                    { value: "ENABLE", label: "创建后启用" },
                    { value: "DISABLE", label: "创建后停用" },
                  ],
                  "停用状态会创建对象但不投放，需要后续手动启用。",
                )}
              </FieldGroup>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>受众定向</CardTitle>
              <CardDescription>
                策略保存常用设置，搭建时可仅修改本次批次。
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <TargetingForm
                value={targeting}
                onChange={(v) => {
                  setTargeting(v)
                  setServerErrors({})
                }}
                countries={tiktokCountryCodes}
                disabled={readonly || pending || !!unknownRequest}
                reference
              />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>文案与 CTA</CardTitle>
              <CardDescription>
                平台英文文案池；同一组无放回抽取不同正文。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <FieldGroup>
                <Field>
                  <FieldLabel>文案池版本</FieldLabel>
                  <p
                    id="strategy-copy_pool_version"
                    tabIndex={-1}
                    className="break-all text-sm"
                  >
                    {pool.data?.name || "正在读取文案池…"} · {copyPoolVersion}
                  </p>
                  {pool.error && (
                    <RequestError
                      error={pool.error}
                      retry={() => void pool.refetch()}
                    />
                  )}
                  <FieldDescription>
                    {capacity === undefined
                      ? "尚未取得有效文案数量。"
                      : `当前有效去重正文：${capacity} 条 · 英文`}
                  </FieldDescription>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => setPoolOpen(true)}
                  >
                    查看文案
                  </Button>
                  {serverErrors.copy_pool_version && (
                    <FieldError>{serverErrors.copy_pool_version}</FieldError>
                  )}
                </Field>
                <Field>
                  <FieldLabel>CTA 配置</FieldLabel>
                  <p className="text-sm">CTA 候选尚未就绪，待场景核实。</p>
                  {ctaIds.length ? (
                    <div className="flex flex-wrap gap-1">
                      {ctaIds.map((id) => (
                        <Badge
                          key={id}
                          variant="outline"
                          className="max-w-full whitespace-normal break-all"
                        >
                          已有 ID：{id}
                        </Badge>
                      ))}
                    </div>
                  ) : (
                    <p className="text-sm text-muted-foreground">
                      尚未配置 CTA 选项。
                    </p>
                  )}
                  <FieldDescription>
                    保留已有选项；具体账户的可用 CTA 将在搭建预览中校验。
                  </FieldDescription>
                  {serverErrors.cta_option_ids && (
                    <FieldError>{serverErrors.cta_option_ids}</FieldError>
                  )}
                </Field>
              </FieldGroup>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>广告命名</CardTitle>
              <CardDescription>
                剧名、版权方标识、剧目
                ID、日期可自由排列；网眼归因标识会完整保留。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <FieldGroup>
                {input(
                  "campaign_name_template",
                  displayNameTemplate(nameTemplate),
                  (value) => setNameTemplate(parseNameTemplate(value)),
                  "可直接编辑标签顺序、分隔符和固定文字。例如：[剧名]-[版权方标识]-[剧目 ID]-[日期]。使用独立标签时，请先删除“版权方＋剧名”组合标签；剧目 ID 必须保留。",
                )}
                {!readonly && (
                  <div className="flex flex-wrap gap-2">
                    {Object.entries(NAME_LABELS).map(([field, label]) => (
                      <Button
                        key={field}
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={
                          pending ||
                          !!unknownRequest ||
                          nameTemplate.includes(`{${field}}`)
                        }
                        onClick={() =>
                          change(
                            "campaign_name_template",
                            setNameTemplate,
                          )(
                            nameTemplate +
                              (nameTemplate && !nameTemplate.endsWith("-")
                                ? "-"
                                : "") +
                              `{${field}}`,
                          )
                        }
                      >
                        添加{label}
                      </Button>
                    ))}
                  </div>
                )}
                <div className="flex flex-col gap-2">
                  <Badge variant="secondary">末尾自动添加：批次编号</Badge>
                  <p className="text-sm text-muted-foreground">
                    例如
                    -A7K2，用于区分多次投放，无需填写。广告组和广告继续自动追加
                    -g01、-sp1 等编号。
                  </p>
                </div>
              </FieldGroup>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>生成规则</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-2 text-sm">
              <p>
                先分广告组，再按创意单元生成基础广告，最后按文案版本数 K
                复制广告。
              </p>
              <p>本批所有有效剧目覆盖全部有效目标账户。策略不保存账户池。</p>
              <p>提交具体搭建预览后，Campaign、Ad Group、Ad 直接启用。</p>
            </CardContent>
          </Card>
        </form>
        <aside className="flex min-w-0 flex-col gap-6">
          <StrategyStructureExample
            tenantId={tenantId}
            groupGenerationMode={groupGenerationMode}
            groupCount={positiveInteger(groupCount) ? Number(groupCount) : null}
            groupMaterialAllocation={groupMaterialAllocation}
            maxMaterialsPerGroup={
              positiveInteger(maxMaterialsPerGroup)
                ? Number(maxMaterialsPerGroup)
                : null
            }
            adGenerationMode={adGenerationMode}
            adsPerGroup={
              positiveInteger(adsPerGroup) ? Number(adsPerGroup) : null
            }
            adMaterialAllocation={adMaterialAllocation}
            maxMaterialsPerAd={
              positiveInteger(maxMaterialsPerAd)
                ? Number(maxMaterialsPerAd)
                : null
            }
            creativeCount={
              positiveInteger(creativeCount) ? Number(creativeCount) : 0
            }
            budget={budgetIssue ? "" : budget}
            currency={currency}
            budgetStrategy={budgetStrategy}
            bidStrategy={bidStrategy}
            targetRoas={roas}
          />
          <StrategyNamingExample nameTemplate={nameTemplate} />
        </aside>
      </div>
      <div className="sticky bottom-0 flex w-full max-w-7xl flex-wrap items-center justify-between gap-3 border-t bg-background px-4 py-3">
        <p className="text-sm text-muted-foreground">
          {readonly
            ? "只读版本，已提交任务继续使用原配置。"
            : !dirty
              ? "没有未保存的修改。"
              : configDirty
                ? "保存新版本后，搭建草稿需重新生成预览。"
                : "仅更新显示名称，不创建新的配置版本。"}
        </p>
        <div className="flex gap-2">
          <Button
            variant="outline"
            disabled={pending}
            onClick={() =>
              void navigate({
                to: "/tenants/$tenantId/strategies",
                params: { tenantId: tenantId! },
                search: { bc_id: scope?.bcId || undefined },
              })
            }
          >
            {readonly ? "返回列表" : "取消"}
          </Button>
          {!readonly && (
            <Button
              type="submit"
              form="strategy-form"
              disabled={
                pending ||
                !!unknownRequest ||
                !!conflict ||
                !dirty ||
                !!firstError ||
                (configDirty && capacity === undefined) ||
                (configDirty && !!pool.error)
              }
            >
              {pending && (
                <Loader2 className="animate-spin" data-icon="inline-start" />
              )}
              {pending
                ? "正在保存…"
                : strategyId
                  ? nameDirty && !configDirty
                    ? "保存名称"
                    : "保存为新版本"
                  : "创建策略"}
            </Button>
          )}
        </div>
      </div>
      {poolOpen && (
        <CopyPoolSheet
          tenantId={tenantId!}
          versionId={copyPoolVersion}
          onClose={() => setPoolOpen(false)}
        />
      )}{" "}
      {showConflict && conflict && (
        <ManagementSheet
          title="服务器版本差异"
          description={`本地基于 v${baseNumber}，服务器当前 v${conflict.latest_version}。原版本保留。`}
          dirty={false}
          onClose={() => setShowConflict(false)}
        >
          <div className="flex flex-col gap-4">
            <dl className="flex flex-col gap-3">
              {Object.entries(fieldNames)
                .filter(([key]) => key !== "name")
                .map(([key, label]) => (
                  <div key={key}>
                    <dt className="font-semibold">{label}</dt>
                    <dd className="break-all text-sm">
                      本地：
                      {key === "campaign_name_template"
                        ? displayNameTemplate(nameTemplate)
                        : String(cfg[key as keyof typeof cfg])}
                      <br />
                      服务器：
                      {key === "campaign_name_template"
                        ? displayNameTemplate(
                            conflict.config.campaign_name_template ??
                              DEFAULT_NAME_TEMPLATE,
                          )
                        : String(conflict.config[key as keyof typeof cfg])}
                    </dd>
                  </div>
                ))}
            </dl>
            <Button
              onClick={() => {
                setBaseNumber(conflict.latest_version)
                setBaseline(conflict.config)
                setConflict(undefined)
                setShowConflict(false)
                setError(undefined)
              }}
            >
              以服务器版本为基线，保留本地编辑
            </Button>
          </div>
        </ManagementSheet>
      )}
      <Dialog
        open={blockers.status === "blocked"}
        onOpenChange={(open) => {
          if (!open) blockers.reset?.()
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>有未保存的修改</DialogTitle>
            <DialogDescription>
              {unknownRequest
                ? "本次保存结果尚未确认，离开不会撤销服务器可能已完成的保存。"
                : "离开会丢弃当前表单的未保存修改，已保存版本会保留。"}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => blockers.reset?.()}>
              留在当前页
            </Button>
            <Button disabled={pending} onClick={() => blockers.proceed?.()}>
              丢弃未保存修改
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
