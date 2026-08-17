import type { ComponentProps } from "react"

import { cn } from "@/lib/utils"

export function Empty({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "flex items-center justify-center rounded-lg border border-dashed border-border px-6 py-10 text-center text-[13px] text-muted-foreground",
        className,
      )}
      {...props}
    />
  )
}
