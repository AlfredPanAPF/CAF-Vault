import type { ComponentProps } from "react"

import { cn } from "@/lib/utils"

const tones = {
  neutral: "border-border bg-muted text-muted-foreground",
  ok: "border-ok/25 bg-ok/10 text-ok",
  warn: "border-warn/25 bg-warn/10 text-warn",
  fail: "border-fail/25 bg-fail/10 text-fail",
  idle: "border-idle/25 bg-idle/10 text-idle",
  outline: "border-border text-muted-foreground",
} as const

export type BadgeTone = keyof typeof tones

export function Badge({
  className,
  tone = "neutral",
  ...props
}: ComponentProps<"span"> & { tone?: BadgeTone }) {
  return (
    <span
      className={cn(
        "inline-flex h-[18px] shrink-0 items-center whitespace-nowrap rounded-full border px-1.5 text-[11px] font-medium leading-none",
        tones[tone],
        className,
      )}
      {...props}
    />
  )
}
