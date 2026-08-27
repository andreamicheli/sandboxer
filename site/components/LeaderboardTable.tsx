"use client"

import * as React from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
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
  form?: string[]
  tie_break?: string
}

const LOGO: Record<string, string> = {
  "deepseek/deepseek-v4-pro": "assets/models/deepseek-v4-pro.jpg",
  "deepseek/deepseek-v4-flash": "assets/models/deepseek-v4-flash.jpg",
  "openai/gpt-5.6-luna": "assets/models/gpt-5.6-luna.jpg",
  "poolside/laguna-s-2.1-free": "assets/models/laguna-s-2.1-free.jpg",
  "xiaomi/mimo-v2.5-pro": "assets/models/mimo-v2.5-pro.jpg",
  "meta/muse-spark-1.2-contributor": "assets/models/meta-muse-spark-1.2-contributor.jpg",
}

const INITIALS: Record<string, string> = {
  "deepseek/deepseek-v4-pro": "DS",
  "deepseek/deepseek-v4-flash": "DS",
  "openai/gpt-5.6-luna": "L",
  "poolside/laguna-s-2.1-free": "LS",
  "xiaomi/mimo-v2.5-pro": "XM",
  "meta/muse-spark-1.2-contributor": "MS",
}

const ACCENT: Record<string, string> = {
  "deepseek/deepseek-v4-pro": "#2563EB",
  "deepseek/deepseek-v4-flash": "#2563EB",
  "openai/gpt-5.6-luna": "#171717",
  "poolside/laguna-s-2.1-free": "#9A65E8",
  "xiaomi/mimo-v2.5-pro": "#FF6900",
  "meta/muse-spark-1.2-contributor": "#58A9FF",
}

function Logo({ model_id }: { model_id: string }) {
  const [failed, setFailed] = React.useState(false)
  const src = LOGO[model_id]
  const initials = INITIALS[model_id] ?? "??"
  const bg = ACCENT[model_id] ?? "#171717"
  if (!src || failed) {
    return (
      <span
        className="inline-flex h-7 w-7 shrink-0 items-center justify-center border border-[var(--line)] text-[9px] font-bold tracking-wide text-white"
        style={{ background: bg }}
      >
        {initials}
      </span>
    )
  }
  return (
    <img
      src={src}
      alt=""
      className="h-7 w-7 shrink-0 border border-[var(--line)] object-cover"
      onError={() => setFailed(true)}
      loading="lazy"
    />
  )
}

export function LeaderboardTable({ data }: { data: Entry[] }) {
  return (
    <Card className="rounded-none border-[var(--line)] bg-white shadow-none">
      <CardHeader className="flex flex-row items-baseline justify-between border-b border-[var(--line)] px-4 py-3.5 space-y-0">
        <CardTitle className="text-[12px] font-semibold tracking-[0.07em] uppercase text-[var(--ink)]">
          Table
        </CardTitle>
        <span className="text-[12px] tracking-wide text-[#737373] tabular-nums">3 pts win</span>
      </CardHeader>
      <CardContent className="p-0">
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow className="border-[var(--line)] hover:bg-transparent">
                <TableHead className="w-[32px] text-center font-semibold tracking-[0.06em] uppercase text-[#737373] text-[11px]">#</TableHead>
                <TableHead className="font-semibold tracking-[0.06em] uppercase text-[#737373] text-[11px]">Team</TableHead>
                <TableHead className="text-center font-semibold tracking-[0.06em] uppercase text-[var(--ink)] text-[11px]" title="Points">Pts</TableHead>
                <TableHead className="text-center font-semibold tracking-[0.06em] uppercase text-[#737373] text-[11px]" title="Played">P</TableHead>
                <TableHead className="text-center font-semibold tracking-[0.06em] uppercase text-[#737373] text-[11px]" title="Wins">W</TableHead>
                <TableHead className="text-center font-semibold tracking-[0.06em] uppercase text-[#737373] text-[11px]" title="Draws">D</TableHead>
                <TableHead className="text-center font-semibold tracking-[0.06em] uppercase text-[#737373] text-[11px]" title="Losses">L</TableHead>
                <TableHead className="text-right font-semibold tracking-[0.06em] uppercase text-[#737373] text-[11px]">Form</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data
                .slice()
                .sort((a, b) => a.pos - b.pos)
                .map((r) => (
                  <TableRow key={r.model_id} className="border-[#f0f0f0] hover:bg-[#fafafa]">
                    <TableCell className="text-center tabular-nums text-[12px] font-semibold text-[#525252]">{r.pos}</TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <Logo model_id={r.model_id} />
                        <div>
                          <div className="text-[13px] font-medium leading-none text-[var(--ink)]">{r.public_name}</div>
                          <div className="mt-[2px] text-[10px] tracking-[0.04em] uppercase text-[#a3a3a3] leading-none">{r.producer}</div>
                        </div>
                      </div>
                    </TableCell>
                    <TableCell className="text-center font-bold tabular-nums text-[14px] tracking-[-0.02em] text-[var(--ink)]">{r.points}</TableCell>
                    <TableCell className="text-center tabular-nums text-[13px] text-[#525252]">{r.played}</TableCell>
                    <TableCell className="text-center tabular-nums text-[13px] text-[#525252]">{r.wins}</TableCell>
                    <TableCell className="text-center tabular-nums text-[13px] text-[#525252]">{r.draws}</TableCell>
                    <TableCell className="text-center tabular-nums text-[13px] text-[#525252]">{r.losses}</TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-[3px]">
                        {(r.form ?? []).slice(-5).map((c, i) => (
                          <span
                            key={i}
                            className={
                              "inline-flex h-4 w-4 items-center justify-center border text-[9px] font-bold tracking-wide " +
                              (c === "W"
                                ? "bg-[#171717] text-white border-[#171717]"
                                : c === "D"
                                  ? "bg-white text-[#525252] border-[var(--line)]"
                                  : "bg-[#fafafa] text-[#a3a3a3] border-[var(--line)]")
                            }
                          >
                            {c}
                          </span>
                        ))}
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  )
}
