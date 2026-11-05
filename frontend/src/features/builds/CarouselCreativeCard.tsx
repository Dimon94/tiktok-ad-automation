import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { MaterialsService, type ImageUnitPublic, type Edit } from "@/client"
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
  CardFooter,
} from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { RequestError } from "@/features/tenants/shared"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { MusicPicker } from "./MusicPicker"
import { imageApi, imagePath, musicLabel, change } from "./imageCompositionApi"
import type { ImageEditorState } from "./imageWorkspaceState"
export function ImageThumb({
  tenantId,
  bcId,
  id,
  index,
}: {
  tenantId: string
  bcId: string
  id: string
  index: number
}) {
  const [remoteOnly, setRemoteOnly] = useState(false)
  const [failed, setFailed] = useState(false)
  const query = useQuery({
    queryKey: ["image-thumbnail", tenantId, bcId, id, remoteOnly],
    queryFn: async ({ signal }) => {
      const path = { tenant_id: tenantId, material_id: id }
      // 原件是临时资源；失效后只读当前 BC 已授权的平台副本，不补传或猜 URL。
      if (!remoteOnly) {
        try {
          const { data } = await MaterialsService.readOriginalPreview({
            path,
            signal,
          })
          return { url: data.url, source: "original" as const }
        } catch (error) {
          if (signal.aborted) throw error
        }
      }
      const { data } = await MaterialsService.readRemotePreview({
        path,
        query: { bc_id: bcId },
        signal,
      })
      return { url: data.url, source: "remote" as const }
    },
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: 30000,
  })
  return query.data && !failed ? (
    <img
      src={query.data.url}
      alt={`图片 ${index + 1}`}
      className="size-20 rounded object-contain"
      onError={() => {
        // URL 本身也可能过期。最多原件、平台各一次，平台失败后明确停止。
        if (query.data.source === "original") setRemoteOnly(true)
        else setFailed(true)
      }}
    />
  ) : (
    <div className="flex flex-col items-start gap-1">
      <span className="text-xs text-muted-foreground">
        图片 {index + 1}
        {query.isError || failed ? " · 预览不可用" : " · 加载中"}
      </span>
      {(query.isError || failed) && (
        <Button
          variant="ghost"
          size="sm"
          aria-label={`重试图片 ${index + 1} 预览`}
          onClick={() => {
            // 原件清理或平台短暂失败后，由用户显式重读当前 BC 的平台副本。
            setFailed(false)
            if (!remoteOnly) setRemoteOnly(true)
            else void query.refetch()
          }}
        >
          重试预览
        </Button>
      )}
    </div>
  )
}

export function CarouselCreativeCard({
  tenantId,
  bcId,
  draftId,
  revision,
  unit,
  write,
  recommend,
  onEdit,
  editor,
  onEditor,
  onMusicChanged,
  onAdd,
}: {
  tenantId: string
  bcId: string
  draftId: string
  revision: number
  unit: ImageUnitPublic
  write: boolean
  recommend: boolean
  onEdit: (
    command: Edit["command"],
    expectedRevision?: number,
  ) => Promise<number>
  editor?: ImageEditorState
  onEditor: (value: ImageEditorState | null) => void
  onMusicChanged: (revision: number) => void
  onAdd: (
    unit: ImageUnitPublic,
    ids: string[],
    expectedRevision: number,
  ) => void
}) {
  const ids = editor?.ids ?? unit.ordered_material_ids
  const [music, setMusic] = useState(false),
    [confirm, setConfirm] = useState<
      "DUPLICATE" | "DELETE" | "SPLIT" | "PRESET" | null
    >(null),
    [name, setName] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(),
    [imageOffset, setImageOffset] = useState(0)
  const visibleIds = ids
    .map((id, index) => ({ id, index }))
    .slice(imageOffset, imageOffset + 5)
  const dirty = !!editor
  const stale = dirty && editor.revision !== unit.revision
  async function requestSong(action: "RECOMMEND" | "RETRY") {
    setBusy(true)
    try {
      const result = await imageApi.requestMusic({
        path: { ...imagePath(tenantId, draftId), unit_id: unit.id },
        body: {
          request_id: crypto.randomUUID(),
          expected_content_revision: unit.revision!,
          expected_music_revision: unit.music_revision!,
          action,
        },
      })
      onMusicChanged(result.data.revision)
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  const changeIds = (value: string[]) => {
    if (imageOffset >= value.length)
      setImageOffset(Math.max(0, imageOffset - 5))
    onEditor(
      JSON.stringify(value) === JSON.stringify(unit.ordered_material_ids)
        ? null
        : {
            ids: value,
            revision: editor?.revision ?? unit.revision,
            draftRevision: editor?.draftRevision ?? revision,
          },
    )
  }
  const save = async () => {
    setBusy(true)
    try {
      // 保留开始编辑时的草稿版本，轮询不能替旧内容取得新版本的写入资格。
      await onEdit(
        { kind: "SET_IMAGES", unit_id: unit.id, material_ids: ids },
        editor?.draftRevision ?? revision,
      )
      onEditor(null)
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          创意 {unit.position} · {ids.length} 张
        </CardTitle>
        <CardDescription>
          {unit.music_snapshot?.display_name ||
            (unit.music_id
              ? "已选音乐"
              : musicLabel[unit.music_status ?? "PENDING"])}
        </CardDescription>
        <div className="flex gap-2">
          {unit.composition_protected && (
            <Badge variant="secondary">图片已保护</Badge>
          )}
          {unit.music_protected && (
            <Badge variant="secondary">音乐已保护</Badge>
          )}
          {unit.music_snapshot?.images_changed && (
            <Badge variant="outline">图片已变，可重新推荐</Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {!!error && <RequestError error={error} />}
        {stale && (
          <Alert>
            <AlertDescription>
              图片已被其他操作更新。请取消本地调整并核对最新内容后再编辑。
            </AlertDescription>
          </Alert>
        )}
        <div className="flex flex-wrap gap-3">
          {visibleIds.map(({ id, index: i }) => (
            <div
              key={id}
              draggable={write}
              onDragStart={(e) =>
                e.dataTransfer.setData("text/plain", String(i))
              }
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault()
                const from = Number(e.dataTransfer.getData("text/plain"))
                if (!Number.isInteger(from) || from < 0 || from >= ids.length)
                  return
                const next = [...ids]
                next.splice(i, 0, next.splice(from, 1)[0])
                changeIds(next)
              }}
              className="flex flex-col gap-1"
            >
              <ImageThumb
                key={`${tenantId}:${bcId}:${id}`}
                tenantId={tenantId}
                bcId={bcId}
                id={id}
                index={i}
              />
              {write && (
                <div className="flex gap-1">
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={!i}
                    onClick={() => {
                      const next = [...ids]
                      ;[next[i - 1], next[i]] = [next[i], next[i - 1]]
                      changeIds(next)
                    }}
                    aria-label={`图片 ${i + 1} 前移`}
                  >
                    前移
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={ids.length === 1}
                    onClick={() => changeIds(ids.filter((v) => v !== id))}
                    aria-label={`移除图片 ${i + 1}`}
                  >
                    移除
                  </Button>
                </div>
              )}
            </div>
          ))}
        </div>
        {ids.length > 5 && (
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              disabled={imageOffset === 0}
              onClick={() => setImageOffset(Math.max(0, imageOffset - 5))}
            >
              上一组图片
            </Button>
            <span className="text-sm">
              {imageOffset + 1}–{Math.min(imageOffset + 5, ids.length)} /{" "}
              {ids.length}
            </span>
            <Button
              variant="outline"
              disabled={imageOffset + 5 >= ids.length}
              onClick={() => setImageOffset(imageOffset + 5)}
            >
              下一组图片
            </Button>
          </div>
        )}
        {write && (
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              disabled={ids.length >= 35 || busy || stale}
              onClick={() =>
                onAdd(unit, ids, editor?.draftRevision ?? revision)
              }
            >
              添加图片
            </Button>
            <Button
              disabled={!dirty || busy || stale}
              onClick={() => void save()}
            >
              保存图片调整
            </Button>
            {dirty && (
              <Button
                variant="outline"
                onClick={() => changeIds(unit.ordered_material_ids)}
              >
                取消图片调整
              </Button>
            )}
            <Button variant="outline" onClick={() => setMusic(true)}>
              换音乐
            </Button>
            {unit.music_status === "FAILED" && (
              <Button
                variant="outline"
                onClick={() => void requestSong("RETRY")}
              >
                重试失败配乐
              </Button>
            )}
            {recommend && ids.length > 1 && (
              <Button
                variant="outline"
                onClick={() => void requestSong("RECOMMEND")}
              >
                重新推荐
              </Button>
            )}
          </div>
        )}
      </CardContent>
      {write && (
        <CardFooter className="flex flex-wrap gap-2">
          <Button variant="ghost" onClick={() => setConfirm("DUPLICATE")}>
            复制
          </Button>
          <Button
            variant="ghost"
            disabled={ids.length < 2}
            onClick={() => setConfirm("SPLIT")}
          >
            拆分
          </Button>
          <Button variant="ghost" onClick={() => setConfirm("DELETE")}>
            删除
          </Button>
          <Button variant="ghost" onClick={() => setConfirm("PRESET")}>
            存入轮播库
          </Button>
          {unit.composition_protected && (
            <Button
              variant="ghost"
              onClick={() =>
                void onEdit({
                  kind: "SET_PROTECTION",
                  unit_id: unit.id,
                  composition_protected: false,
                }).catch(setError)
              }
            >
              解除图片保护
            </Button>
          )}
          {unit.music_protected && (
            <Button
              variant="ghost"
              onClick={() =>
                void onEdit({
                  kind: "SET_PROTECTION",
                  unit_id: unit.id,
                  music_protected: false,
                }).catch(setError)
              }
            >
              解除音乐保护
            </Button>
          )}
        </CardFooter>
      )}
      {music && (
        <MusicPicker
          tenantId={tenantId}
          draftId={draftId}
          onClose={() => setMusic(false)}
          onSelect={async (song) => {
            const result = await imageApi.requestMusic({
              path: { ...imagePath(tenantId, draftId), unit_id: unit.id },
              body: {
                request_id: crypto.randomUUID(),
                expected_content_revision: unit.revision!,
                expected_music_revision: unit.music_revision!,
                action: "SET_MANUAL",
                music_id: song.music_id,
              },
            })
            onMusicChanged(result.data.revision)
          }}
        />
      )}
      {confirm && (
        <Dialog
          open
          onOpenChange={(v) => {
            if (!v) setConfirm(null)
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>
                {confirm === "PRESET"
                  ? "存入轮播库"
                  : confirm === "DELETE"
                    ? "确认删除轮播"
                    : confirm === "SPLIT"
                      ? "确认拆分轮播"
                      : "确认复制轮播"}
              </DialogTitle>
              <DialogDescription>
                {confirm === "DELETE"
                  ? "仅此创意使用的图片将标记为本次不再自动使用。"
                  : confirm === "SPLIT"
                    ? "拆为单图创意，新创意需要配乐。请先解除音乐保护。"
                    : confirm === "DUPLICATE"
                      ? "明确复用这些图片，副本继承原曲且独立保存。"
                      : "保存为可复用的独立版本。"}
              </DialogDescription>
            </DialogHeader>
            {confirm === "PRESET" && (
              <FieldGroup>
                <Field>
                  <FieldLabel htmlFor="preset-name">轮播名称</FieldLabel>
                  <Input
                    id="preset-name"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                  />
                </Field>
              </FieldGroup>
            )}
            <DialogFooter>
              <Button variant="outline" onClick={() => setConfirm(null)}>
                取消
              </Button>
              <Button
                disabled={busy || (confirm === "PRESET" && !name.trim())}
                onClick={async () => {
                  setBusy(true)
                  try {
                    if (confirm === "PRESET")
                      await MaterialsService.saveVersion({
                        path: { tenant_id: tenantId },
                        body: {
                          ...change(revision),
                          draft_id: draftId,
                          source_unit_id: unit.id,
                          name: name.trim(),
                        },
                      })
                    else
                      await onEdit(
                        confirm === "SPLIT"
                          ? {
                              kind: "SPLIT",
                              unit_id: unit.id,
                              split_positions: unit.ordered_material_ids
                                .slice(1)
                                .map((_, i) => i + 1),
                            }
                          : { kind: confirm, unit_id: unit.id },
                      )
                    setConfirm(null)
                  } catch (e) {
                    setError(e)
                  } finally {
                    setBusy(false)
                  }
                }}
              >
                确认
                {confirm === "DELETE"
                  ? "删除"
                  : confirm === "DUPLICATE"
                    ? "复制"
                    : confirm === "SPLIT"
                      ? "拆分"
                      : "保存"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </Card>
  )
}
