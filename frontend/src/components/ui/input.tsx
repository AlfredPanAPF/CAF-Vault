import type { ComponentProps } from "react"

import { cn } from "@/lib/utils"

export function Input({ className, ...props }: ComponentProps<"input">) {
  return (
    <input
      className={cn(
        "h-8 rounded-md border border-border bg-card px-2.5 text-[13px] text-foreground outline-none transition-colors placeholder:text-muted-foreground/70 focus:border-accent/50 focus:ring-2 focus:ring-accent/20",
        className,
      )}
      {...props}
    />
  )
}
