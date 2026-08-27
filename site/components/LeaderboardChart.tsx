"use client"

import { Bar, BarChart, XAxis, YAxis, Cell, LabelList } from "recharts"
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
} from "@/components/ui/chart"

type Entry = {
  model_id: string
  public_name: string
  producer: string
  points: number
  played: number
  wins: number
  draws: number
  losses: number
  pct?: number
}

const ACCENT: Record<string, string> = {
  "deepseek/deepseek-v4-pro": "#2563EB",
  "deepseek/deepseek-v4-flash": "#2563EB",
  "openai/gpt-5.6-luna": "#171717",
  "poolside/laguna-s-2.1-free": "#9A65E8",
  "xiaomi/mimo-v2.5-pro": "#FF6900",
  "meta/muse-spark-1.2-contributor": "#58A9FF",
}

const SHORT: Record<string, string> = {
  "deepseek/deepseek-v4-pro": "DS Pro",
  "deepseek/deepseek-v4-flash": "DS Flash",
  "openai/gpt-5.6-luna": "Luna",
  "poolside/laguna-s-2.1-free": "Laguna",
  "xiaomi/mimo-v2.5-pro": "Mimo",
  "meta/muse-spark-1.2-contributor": "Muse",
}

const INITIALS: Record<string, string> = {
  "deepseek/deepseek-v4-pro": "DS",
  "deepseek/deepseek-v4-flash": "DS",
  "openai/gpt-5.6-luna": "L",
  "poolside/laguna-s-2.1-free": "LS",
  "xiaomi/mimo-v2.5-pro": "XM",
  "meta/muse-spark-1.2-contributor": "MS",
}

const chartConfig = {
  points: { label: "Points", color: "var(--ink)" },
} satisfies Record<string, { label: string; color?: string }>

export function LeaderboardChart({ data }: { data: Entry[] }) {
  const chartData = [...data]
    .sort((a, b) => b.points - a.points)
    .map((d) => ({
      model_id: d.model_id,
      name: SHORT[d.model_id] ?? d.public_name,
      fullName: d.public_name,
      points: d.points,
      fill: ACCENT[d.model_id] ?? "var(--ink)",
      initials: INITIALS[d.model_id] ?? d.producer.slice(0, 2).toUpperCase(),
      producer: d.producer,
    }))

  return (
    <ChartContainer config={chartConfig} className="h-[320px] w-full sm:h-[340px]">
      <BarChart
        accessibilityLayer
        data={chartData}
        margin={{ left: 8, right: 8, top: 24, bottom: 8 }}
      >
        <YAxis domain={[0, 13]} hide />
        <XAxis
          dataKey="name"
          axisLine={false}
          tickLine={false}
          interval={0}
          tick={{ fontFamily: "Fraunces, Georgia, serif", fontSize: 11, fill: "#525252" }}
          height={28}
        />
        <ChartTooltip
          cursor={{ fill: "rgba(0,0,0,0.04)" }}
          content={
            <ChartTooltipContent
              indicator="dot"
              labelFormatter={(_, payload) => payload?.[0]?.payload?.fullName ?? ""}
              formatter={(value, _name, item) => (
                <div className="flex w-full items-center justify-between gap-4 text-[13px]">
                  <span className="flex items-center gap-2">
                    <span
                      className="inline-flex h-5 w-5 items-center justify-center text-[10px] font-bold text-white"
                      style={{ background: item.payload.fill }}
                    >
                      {item.payload.initials}
                    </span>
                    <span className="text-muted-foreground">{item.payload.producer}</span>
                  </span>
                  <span className="font-bold tabular-nums">{String(value)} pts</span>
                </div>
              )}
            />
          }
        />
        <Bar dataKey="points" radius={0} barSize={44} maxBarSize={56}>
          <LabelList
            dataKey="points"
            position="top"
            style={{ fontFamily: "Fraunces, Georgia, serif", fontSize: 13, fontWeight: 600, fill: "#171717" }}
          />
          {chartData.map((row) => (
            <Cell key={row.model_id} fill={row.fill} />
          ))}
        </Bar>
      </BarChart>
    </ChartContainer>
  )
}
