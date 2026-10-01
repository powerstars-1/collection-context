// Adapted from creator-sync-cloud-v1 @ 6a9c3a9; navigation labels adapted only.
import type { LucideIcon } from "lucide-react"
import {
  Activity,
  Archive,
  Database,
  Grid2x2,
  Info,
  KeyRound,
  Link2,
  ListChecks,
  Loader2,
  Lock,
  Palette,
  Search,
  Send,
  ShieldCheck,
  Star,
  Table2,
  User,
  UserCircle,
  XCircle,
} from "lucide-react"
import type { UiDensity } from "../lib/settings"
import type { PageKey } from "./Sidebar"

export type SubKey = string

type SubItem = { key: SubKey; label: string; Icon: LucideIcon; muted?: boolean }
type Group = { title?: string; items: SubItem[] }

export const subNavConfig: Partial<Record<PageKey, { title: string; subtitle?: string; groups: Group[] }>> = {
  home: { title: "收藏库", subtitle: "把收藏留给下一次灵感", groups: [
    { title: "我的资料", items: [
      {key:"all",label:"全部资料",Icon:Archive},
      {key:"liked",label:"喜欢",Icon:Star},
      {key:"saved",label:"收藏",Icon:Grid2x2},
      {key:"collection",label:"收藏夹",Icon:Database},
      {key:"creator",label:"博主作品",Icon:User},
      {key:"link",label:"单条链接",Icon:Link2}
    ] }
  ] },
  workbench: {title:"同步与处理",subtitle:"范围、连接与执行状态",groups:[
    {title:"同步",items:[{key:"sources",label:"添加与来源",Icon:Link2}]},
    {title:"处理",items:[{key:"tasks",label:"任务与处理",Icon:ListChecks}]}
  ]},
  settings: {title:"设置",subtitle:"使用自己的模型",groups:[
    {items:[{key:"models",label:"模型与接口",Icon:KeyRound}]}
  ]},
  profile: {title:"AI 接入",subtitle:"共用一个本地后端",groups:[
    {items:[{key:"access",label:"接入说明",Icon:ShieldCheck}]}
  ]}
}

function getActiveNavTone(page: PageKey) {
  switch (page) {
    case "workbench":
      return {
        button:
          "bg-zinc-900 text-white ring-1 ring-zinc-900/5 shadow-sm shadow-zinc-900/10",
        icon: "text-white",
      }
    case "xhs":
      return {
        button:
          "bg-rose-50 text-rose-600 ring-1 ring-rose-100/80 shadow-sm shadow-rose-100/70",
        icon: "text-rose-500",
      }
    case "feishu":
      return {
        button:
          "bg-sky-50 text-sky-700 ring-1 ring-sky-100/80 shadow-sm shadow-sky-100/70",
        icon: "text-sky-600",
      }
    case "tasks":
      return {
        button:
          "bg-amber-50 text-amber-700 ring-1 ring-amber-100/80 shadow-sm shadow-amber-100/70",
        icon: "text-amber-600",
      }
    case "douyin":
      return {
        button:
          "bg-zinc-900 text-white ring-1 ring-zinc-900/5 shadow-sm shadow-zinc-900/10",
        icon: "text-white",
      }
    default:
      return {
        button:
          "bg-white text-zinc-900 ring-1 ring-zinc-200/80 shadow-sm shadow-zinc-900/5",
        icon: "text-zinc-700",
      }
  }
}

export function SubNav({
  page,
  active,
  onChange,
  density = "standard",
}: {
  page: PageKey
  active: SubKey
  onChange: (k: SubKey) => void
  density?: UiDensity
}) {
  const cfg = subNavConfig[page]
  if (!cfg) return null
  const tone = getActiveNavTone(page)
  const scale =
    density === "compact"
      ? {
          aside: "w-[68px] md:w-[220px]",
          header: "px-3 pt-5 pb-4 md:px-5",
          title: "text-[15px]",
          subtitle: "text-xs",
          nav: "px-2 md:px-2.5",
          group: "mb-2",
          button:
            "rounded-lg px-0 py-2 text-sm md:px-3 md:text-sm",
          icon: "h-[17px] w-[17px]",
        }
      : density === "relaxed"
        ? {
            aside: "w-[82px] md:w-[260px]",
            header: "px-3 pt-6 pb-4 md:px-7 md:pt-7 md:pb-5",
            title: "text-[17px]",
            subtitle: "text-[13px]",
            nav: "px-2 md:px-3.5",
            group: "mb-3",
            button:
              "rounded-[14px] px-0 py-3 text-[15px] md:px-3.5 md:text-[16px]",
            icon: "h-[19px] w-[19px]",
          }
        : {
            aside: "w-[74px] md:w-[240px]",
            header: "px-3 pt-5 pb-4 md:px-6 md:pt-6",
            title: "text-base",
            subtitle: "text-[13px]",
            nav: "px-2 md:px-3",
            group: "mb-2.5",
            button:
              "rounded-xl px-0 py-2.5 text-[14px] md:px-3 md:text-[15px]",
            icon: "h-[18px] w-[18px]",
          }

  return (
    <aside
      className={`flex h-full shrink-0 flex-col border-r border-zinc-100/80 bg-white/60 backdrop-blur-xl ${scale.aside}`}
    >
      <div className={scale.header}>
        <div className={`hidden font-semibold text-zinc-900 md:block ${scale.title}`}>
          {cfg.title}
        </div>
        {cfg.subtitle && (
          <div className={`mt-1 hidden text-zinc-400 md:block ${scale.subtitle}`}>
            {cfg.subtitle}
          </div>
        )}
      </div>
      <nav className={`flex-1 overflow-y-auto ${scale.nav}`}>
        {cfg.groups.map((g, gi) => (
          <div key={gi} className={scale.group}>
            {g.title && (
              <div className="hidden px-3 pt-2 pb-1 text-[11px] tracking-wide text-zinc-400 uppercase md:block">
                {g.title}
              </div>
            )}
            {g.items.map(({ key, label, Icon, muted }) => {
              const on = key === active
              return (
                <button
                  key={key}
                  onClick={() => onChange(key)}
                  disabled={muted}
                  title={label}
                  aria-label={label}
                  className={`mb-1 flex w-full items-center justify-center gap-3 text-left transition md:justify-start ${scale.button} ${
                    on
                      ? tone.button
                      : muted
                        ? "cursor-not-allowed text-zinc-400"
                        : "text-zinc-600 hover:bg-white/80"
                  }`}
                >
                  <Icon
                    className={`${scale.icon} ${
                      on ? tone.icon : "text-zinc-500"
                    }`}
                  />
                  <span className="hidden md:inline">{label}</span>
                </button>
              )
            })}
          </div>
        ))}
      </nav>
    </aside>
  )
}
