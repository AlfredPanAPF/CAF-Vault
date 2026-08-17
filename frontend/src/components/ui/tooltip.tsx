import type { ReactNode } from "react"

import { cn } from "@/lib/utils"

/**
 * Small CSS-only tooltip: shows on hover and on focus within the trigger.
 */
export function Tooltip({
  label,
  children,
  className,
}: {
  label: string
  children: ReactNode
  className?: string
}) {
  return (
    <span className={cn("group/tip relative inline-flex", className)}>
      {children}
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full left-1/2 z-50 mb-1.5 w-max max-w-64 -translate-x-1/2 rounded-md border border-border bg-muted px-2.5 py-1.5 text-xs leading-snug text-foreground opacity-0 transition-opacity duration-100 group-focus-within/tip:opacity-100 group-hover/tip:opacity-100"
      >
        {label}
      </span>
    </span>
  )
}
