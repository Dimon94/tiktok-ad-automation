import { useQuery } from "@tanstack/react-query"
import { BuildsService } from "@/client"
import { Textarea } from "@/components/ui/textarea"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import type { NativeSeriesSelection } from "@/client"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"

export function NativeSelectionFields({
  value,
  onChange,
  disabled,
  tenantId,
  draftId,
}: {
  tenantId: string
  draftId?: string
  value: NativeSeriesSelection
  onChange: (value: NativeSeriesSelection) => void
  disabled: boolean
}) {
  const roleValue = (role: "series_identity" | "creative_identity") =>
    value[role] ?? {
      identity_type: "TT_USER" as const,
      identity_id: "",
      identity_authorized_bc_id: null,
    }
  const catalog = useQuery({
    queryKey: ["native-edit-catalog", tenantId, draftId],
    enabled: !!draftId,
    queryFn: async () =>
      (
        await BuildsService.nativeCheckpoint({
          path: { tenant_id: tenantId, draft_id: draftId! },
        })
      ).data,
  })
  const rows = (key: string): Record<string, unknown>[] => {
    const resource = catalog.data?.facts?.[key] as
      | { items?: unknown[] }
      | undefined
    return (resource?.items ?? []).filter(
      (item): item is Record<string, unknown> =>
        typeof item === "object" && item !== null,
    )
  }
  const assets =
    JSON.stringify(catalog.data?.selection.series_identity) ===
    JSON.stringify(value.series_identity)
      ? rows("native_series")
      : []
  const identities = [
    ...rows("native_tt_identity"),
    ...rows("native_bc_identity"),
  ]
  return (
    <div className="grid gap-4 md:grid-cols-2" aria-label="原生 Series 目的地">
      {!!assets.length && (
        <Field>
          <FieldLabel>当前身份可见的 Series</FieldLabel>
          <Select
            disabled={disabled}
            value={value.native_series_id ?? ""}
            onValueChange={(native_series_id) =>
              onChange({ ...value, native_series_id })
            }
          >
            <SelectTrigger>
              <SelectValue placeholder="选择真实 Series" />
            </SelectTrigger>
            <SelectContent>
              {assets.map((item) => (
                <SelectItem
                  key={String(item.native_series_id)}
                  value={String(item.native_series_id)}
                >
                  {String(item.native_series_name ?? "Series")} ·{" "}
                  {String(item.native_series_id)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
      )}
      <Field>
        <FieldLabel>精确 Series ID</FieldLabel>
        <Input
          aria-label="精确 Series ID"
          disabled={disabled}
          value={value.native_series_id ?? ""}
          onChange={(e) =>
            onChange({ ...value, native_series_id: e.target.value })
          }
        />
      </Field>
      <Field>
        <FieldLabel>投放地区（国家代码，逗号分隔）</FieldLabel>
        <Input
          aria-label="投放地区"
          disabled={disabled}
          value={value.regions.join(",")}
          onChange={(e) =>
            onChange({
              ...value,
              regions: e.target.value.toUpperCase().split(","),
            })
          }
        />
      </Field>
      {(["series_identity", "creative_identity"] as const).map((role) => (
        <fieldset key={role} className="space-y-3 rounded-lg border p-3">
          <legend className="px-1 text-sm">
            {role === "series_identity" ? "剧集承载身份" : "创意展示身份"}
          </legend>
          <RadioGroup
            disabled={disabled}
            value={roleValue(role).identity_type}
            onValueChange={(type) =>
              onChange({
                ...value,
                [role]: {
                  ...roleValue(role),
                  identity_type: type,
                  identity_authorized_bc_id: type === "TT_USER" ? null : "",
                },
              })
            }
            className="flex gap-4"
          >
            {(["TT_USER", "BC_AUTH_TT"] as const).map((type) => (
              <Field key={type} orientation="horizontal">
                <RadioGroupItem id={`${role}-${type}`} value={type} />
                <FieldLabel htmlFor={`${role}-${type}`}>{type}</FieldLabel>
              </Field>
            ))}
          </RadioGroup>
          {!!identities.length && (
            <Select
              disabled={disabled}
              onValueChange={(index) => {
                const item = identities[Number(index)]
                onChange({
                  ...value,
                  [role]: {
                    identity_id: String(item.identity_id),
                    identity_type: item.identity_type as
                      | "TT_USER"
                      | "BC_AUTH_TT",
                    identity_authorized_bc_id:
                      typeof item.identity_authorized_bc_id === "string"
                        ? item.identity_authorized_bc_id
                        : null,
                  },
                  ...(role === "series_identity"
                    ? { native_series_id: "" }
                    : {}),
                })
              }}
            >
              <SelectTrigger>
                <SelectValue placeholder="从实际身份目录选择" />
              </SelectTrigger>
              <SelectContent>
                {identities.map((item, index) => (
                  <SelectItem key={index} value={String(index)}>
                    {String(item.display_name ?? item.identity_id)} ·{" "}
                    {String(item.identity_type)} / {String(item.identity_id)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <Input
            aria-label={`${role} ID`}
            placeholder="精确身份 ID"
            disabled={disabled}
            value={roleValue(role).identity_id}
            onChange={(e) =>
              onChange({
                ...value,
                [role]: { ...roleValue(role), identity_id: e.target.value },
              })
            }
          />
          {roleValue(role).identity_type === "BC_AUTH_TT" && (
            <Input
              aria-label={`${role} 授权 BC`}
              placeholder="身份授权 BC ID"
              disabled={disabled}
              value={roleValue(role).identity_authorized_bc_id ?? ""}
              onChange={(e) =>
                onChange({
                  ...value,
                  [role]: {
                    ...roleValue(role),
                    identity_authorized_bc_id: e.target.value,
                  },
                })
              }
            />
          )}
        </fieldset>
      ))}
      <Field className="md:col-span-2">
        <FieldLabel>
          视频与封面来源（每行：素材 UUID，可选封面 UUID）
        </FieldLabel>
        <Textarea
          aria-label="视频与封面来源"
          disabled={disabled}
          value={(value.video_sources ?? [])
            .map((source) =>
              [source.material_id, source.cover_material_id]
                .filter(Boolean)
                .join(","),
            )
            .join("\n")}
          onChange={(event) =>
            onChange({
              ...value,
              video_sources: event.target.value
                ? event.target.value.split("\n").map((line) => {
                    const [material_id, cover_material_id] = line.split(",")
                    return {
                      material_id,
                      cover_material_id: cover_material_id || null,
                    }
                  })
                : [],
            })
          }
        />
      </Field>
      <p className="text-xs text-muted-foreground md:col-span-2">
        保存后从当前连接核对真实目录。身份可见或两个身份 ID
        相同，均不代表组合已验证。视频与封面准备另行完成。
      </p>
    </div>
  )
}
