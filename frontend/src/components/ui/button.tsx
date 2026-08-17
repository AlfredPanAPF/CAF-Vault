import type { ComponentProps } from "react"

import { cn } from "@/lib/utils"

const variants = {
  default: "border-border bg-muted text-foreground hover:bg-border/60",
  primary: "border-accent/30 bg-accent/10 text-accent hover:bg-accent/20",
  ghost: "border-transparent text-muted-foreground hover:bg-muted hover:text-foreground",
  destructive: "border-fail/30 bg-fail/10 text-fail hover:bg-fail/20",
} as const

const sizes = {
  default: "h-8 px-3",
  sm: "h-7 px-2.5 text-xs",
  icon: "size-8",
} as const

export type ButtonProps = ComponentProps<"button"> & {
  variant?: keyof typeof variants
  size?: keyof typeof sizes
}

export function Button({ className, variant = "default", size = "default", ...props }: ButtonProps) {
  return (
    <button
      className={cn(
        "inline-flex shrink-0 select-none items-center justify-center gap-1.5 whitespace-nowrap rounded-md border text-[13px] font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent/40 disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-3.5 [&_svg]:shrink-0",
        variants[variant],
        sizes[size],
        className,
      )}
      {...props}
    />
  )
}
