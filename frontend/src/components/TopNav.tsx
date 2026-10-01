// Adapted from creator-sync-cloud-v1 @ 6a9c3a9; layout and component classes retained.
import {
  Bell,
  BriefcaseBusiness,
  Database,
  Home,
  Images,
  Search,
  Send,
  Settings,
} from "lucide-react"
import type { UiDensity } from "../lib/settings"
import type { PageKey } from "./Sidebar"

const items: { key: PageKey; label: string; Icon: typeof Home }[] = [
  { key: "home", label: "收藏库", Icon: Home },
  { key: "workbench", label: "同步与处理", Icon: BriefcaseBusiness },
  { key: "settings", label: "设置", Icon: Settings },
  { key: "profile", label: "AI 接入", Icon: Database },
]

const ACTIVE_NAV_CLASSES: Partial<Record<PageKey, string>> = {
  home: "bg-white text-zinc-900 ring-1 ring-zinc-200/80 shadow-sm shadow-zinc-900/5",
  workbench:
    "bg-zinc-900 text-white ring-1 ring-zinc-900/5 shadow-sm shadow-zinc-900/10",
  image_studio:
    "bg-orange-50 text-orange-700 ring-1 ring-orange-100/80 shadow-sm shadow-orange-100/70",
  publish:
    "bg-emerald-50 text-emerald-700 ring-1 ring-emerald-100/80 shadow-sm shadow-emerald-100/70",
  xhs: "bg-rose-50 text-rose-600 ring-1 ring-rose-100/80 shadow-sm shadow-rose-100/70",
  douyin:
    "bg-zinc-900 text-white ring-1 ring-zinc-900/5 shadow-sm shadow-zinc-900/10",
  feishu: "bg-sky-50 text-sky-700 ring-1 ring-sky-100/80 shadow-sm shadow-sky-100/70",
  tasks:
    "bg-amber-50 text-amber-700 ring-1 ring-amber-100/80 shadow-sm shadow-amber-100/70",
}

export function TopNav({
  page,
  onChange,
  density = "standard",
  onSearch,
}: {
  page: PageKey
  onChange: (k: PageKey) => void
  density?: UiDensity
  onSearch?: (query: string) => void
}) {
  const settingsActive = page === "settings"
  const profileActive = page === "profile"
  const activeNavClass =
    ACTIVE_NAV_CLASSES[page] ??
    "bg-white text-zinc-900 ring-1 ring-zinc-200/80 shadow-sm shadow-zinc-900/5"
  const navScale =
    density === "compact"
      ? {
          wrapper: "h-14 gap-3 px-3 sm:gap-4 sm:px-4 lg:gap-6 lg:px-6",
          brandWrap: "gap-2",
          brandIcon: "h-7 w-7 rounded-lg",
          brandText: "hidden text-[15px] sm:block",
          navButton:
            "h-9 w-9 rounded-lg px-0 text-sm md:h-auto md:w-auto md:px-3 md:py-1.5",
          search: "w-56 rounded-lg px-9 py-1.5 text-sm",
          iconButton: "h-8 w-8 rounded-lg",
          icon: "h-4 w-4",
          avatar: "ml-1 h-7 w-7",
        }
      : density === "relaxed"
        ? {
            wrapper: "h-14 gap-3 px-3 sm:h-16 sm:gap-4 sm:px-4 lg:h-[72px] lg:gap-8 lg:px-8",
            brandWrap: "gap-3",
            brandIcon: "h-9 w-9 rounded-[14px]",
            brandText: "hidden text-[17px] sm:block",
            navButton:
              "h-10 w-10 rounded-xl px-0 text-[15px] md:h-auto md:w-auto md:px-4 md:py-2.5 md:text-[16px]",
            search: "w-60 rounded-xl px-10 py-2.5 text-sm lg:w-72 lg:text-[15px]",
            iconButton: "h-10 w-10 rounded-xl",
            icon: "h-5 w-5",
            avatar: "ml-1 h-9 w-9",
          }
        : {
            wrapper: "h-14 gap-3 px-3 sm:h-16 sm:gap-4 sm:px-4 lg:gap-7 lg:px-7",
            brandWrap: "gap-2.5",
            brandIcon: "h-8 w-8 rounded-xl",
            brandText: "hidden text-base sm:block",
            navButton:
              "h-10 w-10 rounded-xl px-0 text-[14px] md:h-auto md:w-auto md:px-3.5 md:py-2 md:text-[15px]",
            search: "w-56 rounded-lg px-9 py-2 text-sm xl:w-64 xl:text-[15px]",
            iconButton: "h-9 w-9 rounded-lg",
            icon: "h-[18px] w-[18px]",
            avatar: "ml-1 h-8 w-8",
          }

  return (
    <div
      className={`flex items-center border-b border-zinc-100/90 bg-white/85 shadow-[0_14px_40px_-34px_rgba(15,23,42,0.18)] backdrop-blur-xl ${navScale.wrapper}`}
    >
      <div className={`flex items-center ${navScale.brandWrap}`}>
        <div
          className={`grid place-items-center bg-gradient-to-br from-indigo-500 to-violet-500 text-white ${navScale.brandIcon}`}
        >
          <span className="text-[13px]">⌘</span>
        </div>
        <div className={`font-semibold text-zinc-900 ${navScale.brandText}`}>
          收藏上下文
        </div>
      </div>

      <nav className="flex min-w-0 flex-1 items-center justify-center gap-1.5 md:justify-start">
        {items.map((it) => {
          const on = page === it.key
          return (
            <button
              key={it.key}
              title={it.label}
              aria-label={it.label}
              onClick={() => onChange(it.key)}
              className={`${navScale.navButton} inline-flex shrink-0 items-center justify-center gap-2 transition ${
                on
                  ? activeNavClass
                  : "text-zinc-500 hover:bg-white/70 hover:text-zinc-900"
              }`}
            >
              <it.Icon className="h-4 w-4 md:hidden" />
              <span className="hidden md:inline">{it.label}</span>
            </button>
          )
        })}
      </nav>

      <div className="ml-auto flex items-center gap-2 sm:gap-2.5">
        <div className="relative hidden xl:block">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-400" />
          <input
            aria-label="搜索收藏库"
            placeholder="搜索收藏里的内容..."
            onKeyDown={(event) => { if (event.key === "Enter") onSearch?.(event.currentTarget.value) }}
            className={`border border-zinc-200 bg-white placeholder:text-zinc-400 focus:border-zinc-300 focus:outline-none ${navScale.search}`}
          />
        </div>
        <button
          className={`grid place-items-center text-zinc-500 hover:bg-zinc-100 xl:hidden ${navScale.iconButton}`}
          onClick={() => onChange("home")}
          title="回到收藏库搜索"
          aria-label="搜索"
        >
          <Search className={navScale.icon} />
        </button>
        <button
          aria-label="模型设置"
          onClick={() => onChange("settings")}
          className={`grid place-items-center transition ${navScale.iconButton} ${
            settingsActive
              ? "bg-zinc-900 text-white"
              : "text-zinc-500 hover:bg-zinc-100"
          }`}
        >
          <Settings className={navScale.icon} />
        </button>
        <button
          aria-label="AI 接入说明"
          onClick={() => onChange("profile")}
          className={`${navScale.avatar} overflow-hidden rounded-full ring-2 transition ${
            profileActive
              ? "ring-zinc-900"
              : "ring-transparent hover:ring-zinc-200"
          }`}
        >
          <div className="h-full w-full bg-gradient-to-br from-amber-300 to-rose-400" />
        </button>
      </div>
    </div>
  )
}
