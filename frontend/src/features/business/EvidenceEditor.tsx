import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useState } from "react"
import { type BusinessRow, BusinessService } from "@/client"
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
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { businessKey } from "./queries"

export function EvidenceEditor({
  tenantId,
  bcId,
  row,
  endDate,
  onClose,
}: {
  tenantId: string
  bcId: string
  row: BusinessRow
  endDate: string
  onClose: () => void
}) {
  const [day, setDay] = useState(endDate)
  const [bid, setBid] = useState("")
  const [verified, setVerified] = useState("unknown")
  const [source, setSource] = useState("")
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const [baseline, setBaseline] = useState("")
  const client = useQueryClient()
  const current = useQuery({
    queryKey: [
      ...businessKey(tenantId),
      "evidence",
      bcId,
      row.advertiser_id,
      row.campaign_id,
      day,
    ],
    enabled: !!day,
    queryFn: async ({ signal }) =>
      (
        await BusinessService.readEvidence({
          path: { tenant_id: tenantId },
          query: {
            bc_id: bcId,
            advertiser_id: row.advertiser_id!,
            campaign_id: row.campaign_id!,
            stat_date: day,
          },
          signal,
        })
      ).data,
  })
  useEffect(() => {
    if (!current.data) return
    const b = current.data.bid == null ? "" : String(current.data.bid)
    const v =
      current.data.eligible == null
        ? "unknown"
        : current.data.eligible
          ? "eligible"
          : "excluded"
    const s = current.data.source ?? ""
    setBid(b)
    setVerified(v)
    setSource(s)
    setBaseline(JSON.stringify([b, v, s]))
  }, [current.data])
  const save = async () => {
    if (pending) return
    setPending(true)
    try {
      await BusinessService.evidence({
        path: { tenant_id: tenantId },
        body: {
          bc_id: bcId,
          advertiser_id: row.advertiser_id!,
          campaign_id: row.campaign_id!,
          stat_date: day,
          bid: bid || null,
          eligible: verified === "unknown" ? null : verified === "eligible",
          source,
        },
      })
      await client.invalidateQueries({ queryKey: businessKey(tenantId) })
      onClose()
    } catch {
      setError("赔付依据保存失败，请检查日期、出价与账户权限后重试。")
    } finally {
      setPending(false)
    }
  }
  return (
    <ManagementSheet
      title="赔付核算依据"
      description={`${row.name} · 按系列和账户当地日期分别登记。`}
      dirty={!!baseline && JSON.stringify([bid, verified, source]) !== baseline}
      pending={pending}
      onClose={onClose}
      actions={
        <Button
          type="submit"
          form="business-evidence-form"
          disabled={pending || current.isFetching || current.isError}
        >
          保存依据
        </Button>
      }
    >
      <form
        id="business-evidence-form"
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
          {current.isError && (
            <Alert variant="destructive">
              <AlertDescription>
                已有依据读取失败，请关闭后重试。
              </AlertDescription>
            </Alert>
          )}
          <Field>
            <FieldLabel htmlFor="business-evidence-date">核算日期</FieldLabel>
            <Input
              id="business-evidence-date"
              type="date"
              required
              value={day}
              max={endDate}
              onChange={(e) => setDay(e.target.value)}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="business-evidence-bid">
              当日最低 ROAS 出价
            </FieldLabel>
            <Input
              id="business-evidence-bid"
              type="number"
              min="0.000001"
              step="any"
              required={verified === "eligible"}
              value={bid}
              onChange={(e) => setBid(e.target.value)}
            />
            <FieldDescription>
              填写有历史依据的当日出价，不能直接用当前出价代替。
            </FieldDescription>
          </Field>
          <Field>
            <FieldLabel htmlFor="business-evidence-verification">
              当日核验依据
            </FieldLabel>
            <Select value={verified} onValueChange={setVerified}>
              <SelectTrigger id="business-evidence-verification">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="unknown">
                    资格或操作记录尚未核实
                  </SelectItem>
                  <SelectItem value="eligible">
                    已核实适用且无排除操作
                  </SelectItem>
                  <SelectItem value="excluded">已核实存在排除条件</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
            <FieldDescription>
              适用于 CBO Mini Dramas、D0 IAA
              tROAS。核验包括当日暂停、降预算、改价及其他政策排除条件；事件和收入使用已同步的系列日报。
            </FieldDescription>
          </Field>
          <Field>
            <FieldLabel htmlFor="business-evidence-source">依据说明</FieldLabel>
            <Input
              id="business-evidence-source"
              maxLength={1000}
              required
              value={source}
              onChange={(e) => setSource(e.target.value)}
              placeholder="历史配置、日志或核验凭证的说明"
            />
          </Field>
        </FieldGroup>
      </form>
    </ManagementSheet>
  )
}
