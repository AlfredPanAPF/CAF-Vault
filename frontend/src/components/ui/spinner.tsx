import { Loader2 } from "lucide-react"

import { cn } from "@/lib/utils"

export function Spinner({ className }: { className?: string }) {
  return (
    <Loader2
      aria-label="Loading"
      className={cn("size-4 animate-spin text-muted-foreground", className)}
    />
  )
}
