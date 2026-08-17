import type { ComponentProps } from "react"
import { ChevronDown } from "lucide-react"

import { cn } from "@/lib/utils"

export function Select({ className, children, ...props }: ComponentProps<"select">) {
  return (
    <span className={cn("relative inline-flex", className)}>
      <select
        className="h-8 w-full appearance-none rounded-md border border-border bg-card pl-2.5 pr-7 text-[13px] text-foreground outline-none transition-colors focus:border-accent/50 focus:ring-2 focus:ring-accent/20"
        {...props}
      >
        {children}
      </select>
      <ChevronDown className="pointer-events-none absolute right-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
    </span>
  )
}
