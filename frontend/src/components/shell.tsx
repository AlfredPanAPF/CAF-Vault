import { NavLink, Outlet } from "react-router-dom"
import { Building2, Gauge, Lightbulb, Quote, Rss } from "lucide-react"
import type { LucideIcon } from "lucide-react"

import { cn } from "@/lib/utils"

const nav: { to: string; label: string; icon: LucideIcon; end?: boolean }[] = [
  { to: "/", label: "Dashboard", icon: Gauge, end: true },
  { to: "/claims", label: "Claims", icon: Quote },
  { to: "/entities", label: "Entities", icon: Building2 },
  { to: "/sources", label: "Sources", icon: Rss },
  { to: "/hypotheses", label: "Hypotheses", icon: Lightbulb },
]

function NavItem({ to, label, icon: Icon, end }: (typeof nav)[number]) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        cn(
          "flex shrink-0 items-center gap-2 rounded-md px-2.5 py-1.5 text-[13px] transition-colors",
          isActive
            ? "bg-muted font-medium text-foreground"
            : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
        )
      }
    >
      <Icon className="size-3.5 shrink-0" />
      {label}
    </NavLink>
  )
}

function Brand() {
  return (
    <span className="flex items-center gap-2 px-2.5 text-[13px] font-semibold tracking-tight text-foreground">
      <span className="size-2 rounded-full bg-accent" />
      CAF Vault
    </span>
  )
}

export function Shell() {
  return (
    <div className="flex min-h-screen flex-col bg-background md:flex-row">
      {/* Top bar on small screens */}
      <header className="flex items-center gap-1 overflow-x-auto border-b border-border px-3 py-2 md:hidden">
        <Brand />
        <nav className="ml-2 flex items-center gap-1">
          {nav.map((item) => (
            <NavItem key={item.to} {...item} />
          ))}
        </nav>
      </header>

      {/* Sidebar on wide screens */}
      <aside className="sticky top-0 hidden h-screen w-52 shrink-0 flex-col gap-1 border-r border-border px-3 py-4 md:flex">
        <div className="mb-4">
          <Brand />
        </div>
        {nav.map((item) => (
          <NavItem key={item.to} {...item} />
        ))}
      </aside>

      <main className="min-w-0 flex-1">
        <Outlet />
      </main>
    </div>
  )
}
