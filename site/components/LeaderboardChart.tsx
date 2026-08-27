"use client"

import { Bar, BarChart, XAxis, YAxis, Cell } from "recharts"
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

// Accent brand — mappati a leaderboard.json (allineati al sito)
const ACCENT: Record<string, string> = {
  "deepseek/deepseek-v4-pro": "#4F46E5",
  "openai/gpt-5.6-luna": "#10A37F",
  "poolside/laguna-s-2.1-free": "#9A65E8",
  "xiaomi/mimo-v2.5-pro": "#FF6900",
  "deepseek/deepseek-v4-flash": "#0EA5E9",
  "meta/muse-spark-1.2-contributor": "#58A9FF",
}

const chartConfig = {
  points: { label: "Punti", color: "var(--ink)" },
} satisfies Record<string, { label: string; color?: string }>

/**
 * LeaderboardChart — shadcn ChartContainer + BarChart vertical
 * Design system: Fraunces, --ink #171717, --line #d9d9d9, --bg white.
 * Barre con accent brand, radius 4, scala 0..max (default 13), tooltip con produttore + record V/N/P.
 * Usato sia standalone sia dentro <Leaderboard> (desktop left panel).
 */
export function LeaderboardChart({ data }: { data: Entry[] }) {
  const max = Math.max(...data.map((d) => d.points), 13)

  const chartData = [...data]
    .sort((a, b) => b.points - a.points)
    .map((d) => ({
      name: d.public_name,
      points: d.points,
      fill: ACCENT[d.model_id] ?? "var(--ink)",
      producer: d.producer,
      record: `${d.wins}V · ${d.draws}N · ${d.losses}P`,
      pct: d.pct,
    }))

  return (
    <ChartContainer config={chartConfig} className="h-[300px] w-full sm:h-[340px]">
      <BarChart
        accessibilityLayer
        data={chartData}
        layout="vertical"
        margin={{ left: 6, right: 28, top: 4, bottom: 4 }}
      >
        <XAxis type="number" domain={[0, max]} hide />
        <YAxis
          dataKey="name"
          type="category"
          tickLine={false}
          axisLine={false}
          width={134}
          tick={{ fontFamily: "Fraunces, Georgia, serif", fontSize: 13, fill: "#171717" }}
        />
        <ChartTooltip
          cursor={{ fill: "transparent" }}
          content={
            <ChartTooltipContent
              indicator="line"
              labelFormatter={(_, payload) => payload?.[0]?.payload?.name}
              formatter={(value, _name, item) => (
                <div className="flex w-full justify-between gap-6 text-[13px]">
                  <span className="text-muted-foreground">{item.payload.producer}</span>
                  <span className="font-semibold tabular-nums">
                    {String(value)} pt · {item.payload.record}
                  </span>
                </div>
              )}
            />
          }
        />
        <Bar dataKey="points" radius={[0, 6, 6, 0]} barSize={20}>
          {chartData.map((row) => (
            <Cell key={row.name} fill={row.fill} />
          ))}
        </Bar>
      </BarChart>
    </ChartContainer>
  )
}
