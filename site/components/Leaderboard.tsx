"use client"

import * as React from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { LeaderboardChart } from "./LeaderboardChart"
import { LeaderboardTable } from "./LeaderboardTable"

type Entry = {
  pos: number
  model_id: string
  public_name: string
  producer: string
  points: number
  played: number
  wins: number
  draws: number
  losses: number
  pct?: number
  form?: string[]
  tie_break?: string
}

export function Leaderboard({ data }: { data: Entry[] }) {
  const max = 13
  return (
    <div className="mx-auto max-w-[980px]">
      <h2 className="mb-2 text-[clamp(27px,3.5vw,40px)] leading-none tracking-[-0.015em] text-[var(--ink)]">Leaderboard</h2>
      <p className="mb-7 text-[13px] leading-relaxed text-[#525252]">
        Round-robin · 6 models · 15 series · best-of-3 · 3 pts win · 1 draw
      </p>

      <div className="hidden gap-5 lg:grid lg:grid-cols-[1.05fr_0.95fr] lg:items-start">
        <Card className="overflow-hidden rounded-none border-[var(--line)] bg-white shadow-none">
          <CardHeader className="flex flex-row items-baseline justify-between border-b border-[var(--line)] px-4 py-3.5 space-y-0">
            <CardTitle className="text-[12px] font-semibold tracking-[0.07em] uppercase">Points</CardTitle>
            <span className="text-[12px] tabular-nums text-[#737373]">0–{max}</span>
          </CardHeader>
          <CardContent className="px-2 py-2">
            <LeaderboardChart data={data} />
          </CardContent>
        </Card>

        <LeaderboardTable data={data} />
      </div>

      <div className="lg:hidden">
        <Tabs defaultValue="chart" className="w-full">
          <TabsList className="mb-[18px] inline-flex h-auto gap-0 rounded-none border border-[var(--line)] bg-[#f5f5f5] p-[3px]">
            <TabsTrigger
              value="chart"
              className="rounded-none px-[18px] py-[7px] font-['Fraunces',Georgia,serif] text-[13px] font-semibold tracking-wide text-[#737373] data-[state=active]:bg-white data-[state=active]:text-[var(--ink)] data-[state=active]:shadow-sm border border-transparent data-[state=active]:border-[var(--line)]"
            >
              Chart
            </TabsTrigger>
            <TabsTrigger
              value="table"
              className="rounded-none px-[18px] py-[7px] font-['Fraunces',Georgia,serif] text-[13px] font-semibold tracking-wide text-[#737373] data-[state=active]:bg-white data-[state=active]:text-[var(--ink)] data-[state=active]:shadow-sm border border-transparent data-[state=active]:border-[var(--line)]"
            >
              Table
            </TabsTrigger>
          </TabsList>

          <TabsContent
            value="chart"
            className="mt-0 data-[state=active]:animate-in data-[state=inactive]:animate-out data-[state=active]:fade-in-0 data-[state=active]:slide-in-from-bottom-1 duration-300"
          >
            <Card className="overflow-hidden rounded-none border-[var(--line)] bg-white shadow-none">
              <CardHeader className="flex flex-row items-baseline justify-between border-b border-[var(--line)] px-4 py-3.5 space-y-0">
                <CardTitle className="text-[12px] font-semibold tracking-[0.07em] uppercase">Points</CardTitle>
                <span className="text-[12px] tabular-nums text-[#737373]">0–{max}</span>
              </CardHeader>
              <CardContent className="px-2 py-2">
                <LeaderboardChart data={data} />
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent
            value="table"
            className="mt-0 data-[state=active]:animate-in data-[state=inactive]:animate-out data-[state=active]:fade-in-0 data-[state=active]:slide-in-from-bottom-1 duration-300"
          >
            <LeaderboardTable data={data} />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  )
}
