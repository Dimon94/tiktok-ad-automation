import type { AdDetailPublic } from "@/client"
import { SettingsTab } from "./SettingsTab"

function value(input: unknown): string {
  return input === null || input === undefined
    ? "未提供"
    : JSON.stringify(input)
}

function Fields({ entries }: { entries: Record<string, unknown> }) {
  return (
    <dl className="grid gap-2 md:grid-cols-2">
      {Object.entries(entries).map(([label, input]) => (
        <div key={label} className="border-b py-1">
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="break-all">{value(input)}</dd>
        </div>
      ))}
    </dl>
  )
}

const reasons: Record<string, string> = {
  parent_not_synced: "父广告组未同步",
  "creative_coverage:unknown": "创意清单覆盖未知；仅展示已观察关系",
  "creative_materials:not_covered": "创意素材关联未覆盖",
  "creative_materials:unresolved": "创意素材身份未解析",
  "creative_materials:conflict": "创意素材身份存在冲突",
  "materials:not_covered": "素材未覆盖",
  "creative_identity:not_provided": "创意身份未提供或不完整",
  "creative_id:not_provided": "创意 ID 未提供",
}

export function DirectoryConfiguration({ detail }: { detail: AdDetailPublic }) {
  const native = detail.native
  return (
    <div className="space-y-5 text-sm">
      <Fields
        entries={{
          来源连接: detail.source_connection_id,
          来源渠道: detail.source_channel,
          观察时间: detail.observed_at,
          发布版本: detail.published_version,
        }}
      />
      {native && (
        <section aria-label="原生 Series 与身份" className="space-y-3">
          <h3 className="font-medium">原生 Series 与身份</h3>
          <p className="text-muted-foreground">
            当前已发布目录观察；各层可能来自不同时间与版本，不代表实时或完整清单。
          </p>
          <Fields
            entries={{
              "Series ID": native.series_id,
              "Series 名称": native.series_name,
              组身份类型: native.group_identity?.identity_type,
              "组身份 ID": native.group_identity?.identity_id,
              "组授权 BC ID": native.group_identity?.identity_authorized_bc_id,
              版位类型: native.placement_type,
              版位: native.placements,
              "TikTok 子版位": native.tiktok_subplacements,
            }}
          />
          {detail.ref.kind === "ad" && (
            <Fields
              entries={{
                "父广告组 ID": native.parent_adgroup?.ref.remote_id ?? "未同步",
                父组观察时间: native.parent_adgroup?.observed_at,
                父组发布版本: native.parent_adgroup?.published_version,
                父组来源连接: native.parent_adgroup?.source_connection_id,
                父组来源渠道: native.parent_adgroup?.source_channel,
              }}
            />
          )}
          {native.coverage_reasons?.map((reason) => (
            <p key={reason}>
              {reasons[reason] ?? `${reason.split(":")[0]}：未提供`}
            </p>
          ))}
          {detail.ref.kind === "ad" && native.creatives == null && (
            <p>创意目录未覆盖，无法确认创意清单。</p>
          )}
          {native.creatives?.map((creative, index) => (
            <section
              key={creative.creative_id ?? `unknown-${index}`}
              aria-label={`创意 ${creative.creative_id ?? "未提供"}`}
              className="space-y-2 rounded-md border p-3"
            >
              <Fields
                entries={{
                  "Creative ID": creative.creative_id,
                  创意身份类型: creative.identity?.identity_type,
                  "创意身份 ID": creative.identity?.identity_id,
                  "创意授权 BC ID":
                    creative.identity?.identity_authorized_bc_id,
                  创意观察时间: creative.observed_at,
                  创意发布版本: creative.published_version,
                  创意来源连接: creative.source_connection_id,
                  创意来源渠道: creative.source_channel,
                }}
              />
              {creative.materials.map((material, ordinal) => (
                <Fields
                  key={`${material.material_type}:${material.platform_material_id}:${ordinal}`}
                  entries={{
                    "平台素材 ID": material.platform_material_id,
                    素材类型: material.material_type,
                    素材名称: material.name,
                    "广告内素材 ID": material.ad_material_id,
                    素材观察时间: material.observed_at,
                    素材发布版本: material.published_version,
                    素材来源连接: material.source_connection_id,
                    素材来源渠道: material.source_channel,
                  }}
                />
              ))}
              {creative.coverage_reasons.map((reason) => (
                <p key={reason}>{reasons[reason] ?? reason}</p>
              ))}
              <details>
                <summary>创意原始配置</summary>
                <pre className="whitespace-pre-wrap break-all">
                  {JSON.stringify(creative.configuration, null, 2)}
                </pre>
              </details>
            </section>
          ))}
          {native.parent_adgroup && (
            <details>
              <summary>父广告组原始配置</summary>
              <pre className="whitespace-pre-wrap break-all">
                {JSON.stringify(native.parent_adgroup.configuration, null, 2)}
              </pre>
            </details>
          )}
        </section>
      )}
      {detail.ref.kind === "creative" ? (
        <pre className="whitespace-pre-wrap break-all">
          {JSON.stringify(detail.configuration, null, 2)}
        </pre>
      ) : (
        <SettingsTab
          kind={detail.ref.kind as "campaign" | "adgroup" | "ad"}
          configuration={detail.configuration ?? {}}
          observedAt={detail.observed_at}
        />
      )}
    </div>
  )
}
