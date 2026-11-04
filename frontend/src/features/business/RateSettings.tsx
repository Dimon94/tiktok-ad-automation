import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef, useState } from "react"
import { BusinessService } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { businessKey, percentageRate, rateKey, shiftedDay } from "./queries"

export function RateSettings({
  tenantId,
  kind,
  scopeId,
  name,
  onClose,
}: {
  tenantId: string
  kind: "provider" | "rebate"
  scopeId: string
  name: string
  onClose: () => void
}) {
  const query = useQuery({
    queryKey: rateKey(tenantId, kind, scopeId),
    queryFn: async ({ signal }) =>
      (
        await BusinessService.rates({
          path: { tenant_id: tenantId },
          query: { kind, scope_id: scopeId },
          signal,
        })
      ).data,
  })
  const [rate, setRate] = useState("0")
  const [initialRate, setInitialRate] = useState("0")
  const [day, setDay] = useState(
    shiftedDay(new Date().toISOString().slice(0, 10), 1),
  )
  const [initialDay] = useState(day)
  const initialised = useRef(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const client = useQueryClient()
  const label =
    kind === "provider" ? "版权方抽成比例（%）" : "其他返点比例（%）"
  useEffect(() => {
    if (query.data && !initialised.current) {
      initialised.current = true
      const current = query.data.find((r) => r.effective_date <= day)
      const value = current
        ? String(Number((Number(current.rate) * 100).toFixed(6)))
        : "0"
      setRate(value)
      setInitialRate(value)
    }
  }, [query.data, day])
  const save = async () => {
    if (pending) return
    setPending(true)
    setError("")
    try {
      await BusinessService.setRate({
        path: { tenant_id: tenantId },
        body: {
          kind,
          scope_id: scopeId,
          effective_date: day,
          rate: percentageRate(rate),
        },
      })
      await client.invalidateQueries({ queryKey: businessKey(tenantId) })
      onClose()
    } catch {
      setError("比例保存失败，请检查日期、比例及当前管理权限后重试。")
    } finally {
      setPending(false)
    }
  }
  return (
    <ManagementSheet
      title={kind === "provider" ? "版权方抽成设置" : "经营设置"}
      description={`${name} · 比例仅对当前租户生效，未配置时为0%。`}
      dirty={rate !== initialRate || day !== initialDay}
      pending={pending}
      onClose={onClose}
      actions={
        <Button
          type="submit"
          form="business-rate-form"
          disabled={pending || query.isPending || query.isError}
        >
          保存比例
        </Button>
      }
    >
      <form
        id="business-rate-form"
        onSubmit={(e) => {
          e.preventDefault()
          void save()
        }}
      >
        <FieldGroup>
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <Field>
            <FieldLabel htmlFor="business-rate">{label}</FieldLabel>
            <Input
              id="business-rate"
              type="number"
              min="0"
              max="100"
              step="0.0001"
              required
              value={rate}
              onChange={(e) => setRate(e.target.value)}
            />
            <FieldDescription>
              {kind === "provider"
                ? "按平台收入计算抽成，剩余部分计入分成后收入。"
                : "按平台消耗计算，填写代理、主体等返点的合计比例。"}
            </FieldDescription>
          </Field>
          <Field>
            <FieldLabel htmlFor="business-rate-date">生效日期</FieldLabel>
            <Input
              id="business-rate-date"
              type="date"
              required
              value={day}
              onChange={(e) => setDay(e.target.value)}
            />
            <FieldDescription>
              按账户当地日期生效。选择历史日期将重算该日起的经营数据，旧版本保留供追溯。
            </FieldDescription>
          </Field>
          <FieldDescription>
            影响范围：{name}，业务日期 {day}{" "}
            起。保存后由后台重新核算，完成前继续显示上次结果。
          </FieldDescription>
        </FieldGroup>
      </form>
      <div className="mt-6 flex flex-col gap-3">
        <h3 className="font-medium">比例记录</h3>
        {query.isError ? (
          <Alert variant="destructive">
            <AlertDescription>
              比例记录读取失败，请重新打开后重试。
            </AlertDescription>
          </Alert>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>生效日期</TableHead>
                <TableHead>比例</TableHead>
                <TableHead>版本</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {query.data?.map((r) => (
                <TableRow key={r.id}>
                  <TableCell>{r.effective_date}</TableCell>
                  <TableCell>{(Number(r.rate) * 100).toFixed(2)}%</TableCell>
                  <TableCell>{r.revision}</TableCell>
                </TableRow>
              ))}
              {!query.data?.length && (
                <TableRow>
                  <TableCell colSpan={3}>默认 0%，尚无修改记录。</TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        )}
      </div>
    </ManagementSheet>
  )
}
