import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import type { MusicItemPublic } from "@/client"
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
import { Button } from "@/components/ui/button"
import { RequestError } from "@/features/tenants/shared"
import { imageApi, imagePath } from "./imageCompositionApi"
export function MusicPicker({
  tenantId,
  draftId,
  onClose,
  onSelect,
}: {
  tenantId: string
  draftId: string
  onClose: () => void
  onSelect: (song: MusicItemPublic) => Promise<void> | void
}) {
  const [text, setText] = useState(""),
    [keyword, setKeyword] = useState(""),
    [cursor, setCursor] = useState<string | null>(null),
    [selected, setSelected] = useState<MusicItemPublic | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>()
  const query = useQuery({
    queryKey: ["music-search", tenantId, draftId, keyword, cursor],
    enabled: !!keyword,
    queryFn: async ({ signal }) =>
      (
        await imageApi.searchMusic({
          path: imagePath(tenantId, draftId),
          query: { keyword, cursor },
          signal,
        })
      ).data,
  })
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose()
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>选择音乐</DialogTitle>
          <DialogDescription>
            搜索并试听，确认使用后才会更换音乐。
          </DialogDescription>
        </DialogHeader>
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="music-search">搜索音乐</FieldLabel>
            <Input
              id="music-search"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
          </Field>
        </FieldGroup>
        <Button
          variant="outline"
          disabled={!text.trim()}
          onClick={() => {
            setKeyword(text.trim())
            setCursor(null)
          }}
        >
          搜索
        </Button>
        {!!(error || query.error) && (
          <RequestError error={error || query.error} />
        )}
        {query.isFetching && <p role="status">正在搜索音乐…</p>}
        {query.isSuccess && !query.isFetching && !query.data.items.length && (
          <p role="status">没有找到匹配的音乐，请更换关键词。</p>
        )}
        <div className="flex max-h-72 flex-col gap-3 overflow-y-auto">
          {query.data?.items.map((song) => (
            <div key={song.music_id} className="flex flex-col gap-2">
              <Button
                variant={
                  selected?.music_id === song.music_id ? "default" : "outline"
                }
                onClick={() => setSelected(song)}
              >
                {song.display_name} {song.author && `· ${song.author}`}
              </Button>
              {song.preview_url && (
                <audio
                  controls
                  preload="none"
                  src={song.preview_url}
                  aria-label={`试听 ${song.display_name}`}
                />
              )}
            </div>
          ))}
        </div>
        {query.data?.next_cursor && (
          <Button
            variant="outline"
            onClick={() => setCursor(query.data!.next_cursor!)}
          >
            下一页音乐
          </Button>
        )}
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={onClose}>
            取消
          </Button>
          <Button
            disabled={!selected || busy}
            onClick={async () => {
              if (!selected) return
              setBusy(true)
              try {
                await onSelect(selected)
                onClose()
              } catch (e) {
                setError(e)
              } finally {
                setBusy(false)
              }
            }}
          >
            使用这首音乐
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
