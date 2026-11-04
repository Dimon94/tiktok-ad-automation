import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
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

type DramaOption = {
  id: string
  title: string
  external_id: string
  application_id: string
}
export function MappingEditor({
  tenantId,
  bcId,
  row,
  onClose,
}: {
  tenantId: string
  bcId: string
  row: BusinessRow
  onClose: () => void
}) {
  const [text, setText] = useState("")
  const [q, setQuery] = useState("")
  const [selected, setSelected] = useState("")
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const client = useQueryClient()
  const query = useQuery({
    queryKey: [...businessKey(tenantId), "dramas", q],
    queryFn: async ({ signal }) =>
      (
        await BusinessService.dramas({
          path: { tenant_id: tenantId },
          query: { q, limit: 50 },
          signal,
        })
      ).data as unknown as DramaOption[],
  })
  const save = async () => {
    setPending(true)
    try {
      await BusinessService.mapping({
        path: { tenant_id: tenantId },
        body: {
          bc_id: bcId,
          advertiser_id: row.advertiser_id!,
          campaign_id: row.campaign_id!,
          drama_id: selected,
        },
      })
      await client.invalidateQueries({ queryKey: businessKey(tenantId) })
      onClose()
    } catch {
      setError("归属保存失败，请检查账户权限及剧目后重试。")
    } finally {
      setPending(false)
    }
  }
  return (
    <ManagementSheet
      title="设置剧目归属"
      description={row.name}
      dirty={!!selected}
      pending={pending}
      onClose={onClose}
      actions={
        <Button disabled={!selected || pending} onClick={() => void save()}>
          保存归属
        </Button>
      }
    >
      <FieldGroup>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <Field>
          <FieldLabel htmlFor="business-drama-search">
            剧名或版权方剧目编号
          </FieldLabel>
          <Input
            id="business-drama-search"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <Button
            type="button"
            variant="outline"
            onClick={() => setQuery(text)}
          >
            查找剧目
          </Button>
        </Field>
        <Field>
          <FieldLabel htmlFor="business-drama-choice">对应剧目</FieldLabel>
          <Select value={selected} onValueChange={setSelected}>
            <SelectTrigger id="business-drama-choice">
              <SelectValue placeholder="选择已登记剧目" />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {query.data?.map((d) => (
                  <SelectItem key={d.id} value={d.id}>
                    {d.title} · {d.external_id} · {d.application_id}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
          <FieldDescription>
            只按明确剧目ID建立关系；保存后会追溯重算，系列改名不会改变归属。
          </FieldDescription>
        </Field>
      </FieldGroup>
    </ManagementSheet>
  )
}
