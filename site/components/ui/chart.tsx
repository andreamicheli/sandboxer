"use client"

import * as React from "react"
import * as RechartsPrimitive from "recharts"

function cn(...c: Array<string | false | null | undefined>) { return c.filter(Boolean).join(" ") }

// Minimal shadcn ChartContainer adapted to SandBoxer design system
// Uses --ink / --line / Fraunces; no external deps beyond recharts.

export type ChartConfig = Record<string, { label?: string; color?: string; icon?: React.ComponentType }>

const ChartContext = React.createContext<{ config: ChartConfig } | null>(null)

function useChart() {
  const ctx = React.useContext(ChartContext)
  if (!ctx) throw new Error("useChart must be used within ChartContainer")
  return ctx
}

const ChartContainer = React.forwardRef<
  HTMLDivElement,
  React.ComponentProps<"div"> & { config: ChartConfig; children: React.ComponentProps<typeof RechartsPrimitive.ResponsiveContainer>["children"] }
>(({ id, className, children, config, ...props }, ref) => {
  const chartId = `chart-${id || React.useId().replace(/:/g, "")}`
  return (
    <ChartContext.Provider value={{ config }}>
      <div
        data-chart={chartId}
        ref={ref}
        className={cn(
          "flex aspect-video justify-center text-[var(--ink)] [&_.recharts-cartesian-axis-tick_text]:fill-[#737373] [&_.recharts-tooltip-cursor]:fill-transparent",
          className
        )}
        {...props}
      >
        <RechartsPrimitive.ResponsiveContainer>{children as any}</RechartsPrimitive.ResponsiveContainer>
      </div>
    </ChartContext.Provider>
  )
})
ChartContainer.displayName = "Chart"

const ChartTooltip = RechartsPrimitive.Tooltip

const ChartTooltipContent = React.forwardRef<
  HTMLDivElement,
  React.ComponentProps<typeof RechartsPrimitive.Tooltip> &
    React.ComponentProps<"div"> & {
      hideLabel?: boolean
      hideIndicator?: boolean
      indicator?: "line" | "dot" | "dashed"
      nameKey?: string
      labelKey?: string
    }
>(({ active, payload, label, className, indicator = "dot", hideLabel = false, hideIndicator = false, labelFormatter, formatter, color, nameKey, labelKey }, ref) => {
  const { config } = useChart()
  if (!active || !payload?.length) return null
  return (
    <div
      ref={ref}
      className={cn(
        "grid min-w-[10rem] items-start gap-1.5 rounded-none border border-[var(--line)] bg-white px-3 py-2 text-xs shadow-md",
        className
      )}
    >
      {!hideLabel && label ? (
        <div className="font-['Fraunces',Georgia,serif] font-semibold text-[var(--ink)]">
          {labelFormatter ? labelFormatter(label as any, payload as any) : (label as string)}
        </div>
      ) : null}
      <div className="grid gap-1.5">
        {payload.map((item: any, i: number) => {
          const key = `${nameKey || item.dataKey || item.name || "value"}`
          const itemConfig = config[key as keyof typeof config]
          const display = formatter ? formatter(item.value, item.name, item, i, payload) : item.value
          return (
            <div key={i} className="flex items-center justify-between gap-3">
              {!hideIndicator && (
                <span
                  className="h-2 w-2 shrink-0 rounded-none"
                  style={{ background: item.payload?.fill || item.color || "var(--ink)" }}
                />
              )}
              <span className="text-[#737373]">{itemConfig?.label || item.name}</span>
              <span className="font-medium tabular-nums text-[var(--ink)]">{display as any}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
})
ChartTooltipContent.displayName = "ChartTooltip"

const ChartLegend = RechartsPrimitive.Legend
const ChartLegendContent = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement> & { payload?: any[]; verticalAlign?: string; hideIcon?: boolean; nameKey?: string }>(
  ({ className, hideIcon = false, payload, verticalAlign = "bottom", nameKey }, ref) => {
    const { config } = useChart()
    if (!payload?.length) return null
    return (
      <div ref={ref} className={cn("flex items-center justify-center gap-4", verticalAlign === "top" ? "pb-3" : "pt-3", className)}>
        {payload.map((item: any) => {
          const key = `${nameKey || item.dataKey || "value"}`
          const itemConfig = config[key as keyof typeof config]
          return (
            <div key={item.value} className="flex items-center gap-1.5 [&_svg]:h-3 [&_svg]:w-3">
              {itemConfig?.icon && !hideIcon ? <itemConfig.icon /> : null}
              <span className="text-xs text-muted-foreground">{itemConfig?.label}</span>
            </div>
          )
        })}
      </div>
    )
  }
)
ChartLegendContent.displayName = "ChartLegend"

export { ChartContainer, ChartTooltip, ChartTooltipContent, ChartLegend, ChartLegendContent }
