// Original owner component from creator-sync-cloud-v1 @ 6a9c3a9.
import type { ReactNode } from "react"

export function WorkbenchLayout({
  title,
  description,
  middle,
  main,
}: {
  title: string
  description: string
  middle: ReactNode
  main: ReactNode
}) {
  return (
    <div className="grid h-full grid-cols-1 overflow-hidden xl:grid-cols-[360px_minmax(0,1fr)]">
      <section className="max-h-[42vh] overflow-y-auto border-b border-zinc-100 bg-white/70 p-4 sm:p-5 xl:max-h-none xl:border-r xl:border-b-0">
        <div className="text-xs text-zinc-400">工作台</div>
        <h1 className="mt-1 text-xl font-semibold text-zinc-900">{title}</h1>
        <p className="mt-1 text-sm text-zinc-500">{description}</p>
        <div className="mt-5">{middle}</div>
      </section>
      <section className="overflow-y-auto p-4 sm:p-6">{main}</section>
    </div>
  )
}
