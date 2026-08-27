"use client"

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

/**
 * LeaderboardTable — shadcn Table dentro Card, stile calcio
 * Colonne: # | Squadra | Pt (bold) | G | V | N | P | Forma
 * Design system SandBoxer: --ink, --line, Fraunces, tabular-nums, accent invariato.
 */
export function LeaderboardTable({ data }: { data: Entry[] }) {
  const tie = data.find((d) => d.tie_break)
  return (
    <Card className="rounded-[14px] border-[var(--line)] bg-white shadow-none">
      <CardHeader className="flex flex-row items-baseline justify-between border-b border-[var(--line)] px-4 py-3.5 space-y-0">
        <CardTitle className="text-[12px] font-semibold tracking-[0.07em] uppercase text-[var(--ink)]">
          Classifica
        </CardTitle>
        <span className="text-[12px] tracking-wide text-[#737373] tabular-nums">3V · 1N · 0P</span>
      </CardHeader>
      <CardContent className="p-0">
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow className="border-[var(--line)] hover:bg-transparent">
                <TableHead className="w-[32px] text-center font-semibold tracking-[0.06em] uppercase text-[#737373] text-[11px]">#</TableHead>
                <TableHead className="font-semibold tracking-[0.06em] uppercase text-[#737373] text-[11px]">Squadra</TableHead>
                <TableHead className="text-center font-semibold tracking-[0.06em] uppercase text-[var(--ink)] text-[11px]">Pt</TableHead>
                <TableHead className="text-center font-semibold tracking-[0.06em] uppercase text-[#737373] text-[11px]" title="Giocate">G</TableHead>
                <TableHead className="text-center font-semibold tracking-[0.06em] uppercase text-[#737373] text-[11px]" title="Vinte">V</TableHead>
                <TableHead className="text-center font-semibold tracking-[0.06em] uppercase text-[#737373] text-[11px]" title="Pareggiate">N</TableHead>
                <TableHead className="text-center font-semibold tracking-[0.06em] uppercase text-[#737373] text-[11px]" title="Perse">P</TableHead>
                <TableHead className="text-right font-semibold tracking-[0.06em] uppercase text-[#737373] text-[11px]">Forma</TableHead>
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
                      <div className="text-[13px] font-medium leading-none text-[var(--ink)]">{r.public_name}</div>
                      <div className="mt-[2px] text-[10px] tracking-[0.04em] uppercase text-[#a3a3a3] leading-none">{r.producer}</div>
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
                              "inline-flex h-4 w-4 items-center justify-center rounded-[4px] border text-[9px] font-bold tracking-wide " +
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
        {tie ? (
          <div className="border-t border-[var(--line)] px-4 py-2.5 text-[12px] leading-snug text-[#737373]">
            Nota: <span className="font-semibold text-[var(--ink)]">{tie.public_name}</span> — {tie.tie_break}.
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}
