// Original owner component; material fields adapted to the collection-context API.
import type { MaterialRef } from "../../lib/workbench"

export function MaterialList({
  materials,
  selectedIds,
  onToggle,
}: {
  materials: MaterialRef[]
  selectedIds: string[]
  onToggle: (id: string) => void
}) {
  return (
    <div className="space-y-2">
      {materials.map((material) => {
        const selected = selectedIds.includes(material.id)
        return (
          <button
            key={material.id}
            type="button"
            aria-pressed={selected}
            onClick={() => onToggle(material.id)}
            className={`flex w-full items-center gap-3 rounded-2xl border p-3 text-left transition ${
              selected
                ? "border-zinc-900 bg-zinc-50"
                : "border-zinc-200 bg-white hover:border-zinc-300"
            }`}
          >
            <span className="grid h-5 w-5 place-items-center rounded border border-zinc-300 text-xs">
              {selected ? "✓" : ""}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm text-zinc-900">
                {material.title}
              </span>
              <span className="text-xs text-zinc-500">
                {material.platform} · {material.mediaType}
              </span>
              <span className="mt-1 block truncate text-xs text-zinc-500">{material.authorName} · {material.sources}</span>
              <span className="mt-1 block text-xs text-zinc-400">{material.status}</span>
            </span>
          </button>
        )
      })}
    </div>
  )
}
