import React from 'react';
import {
  AbsoluteFill,
  Composition,
  Easing,
  Sequence,
  interpolate,
  registerRoot,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import { ArenaVisual, type ArenaPlan } from './arena';

/* ------------------------------------------------------------------ */
/* Brand: deep navy-black, electric cobalt, monospace, CRT texture.    */
/* ------------------------------------------------------------------ */

const NAVY = '#030711';
const NAVY_2 = '#050b18';
const PANEL = '#071b35';
const COBALT = '#4d9cff';
const AMBER = '#f1782c';
const VIOLET = '#9a65e8';
const MUSE_BLUE = '#58a9ff';
const INK = '#f0eee7';
const DIM = '#8a97ad';
const RED = '#ff4d5e';
const CYAN = '#38e1c8';

const ACCENTS: Record<string, string> = {
  'Laguna S 2.1': VIOLET,
  'Muse Spark 1.2': MUSE_BLUE,
};

const MONO =
  "'JetBrains Mono', 'Fira Code', 'SFMono-Regular', 'Consolas', 'Courier New', monospace";

type TerminalEvent = {
  at_frame: number;
  pane: number;
  phase: string;
  event_type: string;
  text: string;
  event_id: string;
};

type Scene = {
  type: string;
  duration_frames: number;
  event_ids: string[];
  identities?: string[];
  metadata?: Record<string, Record<string, unknown>>;
  benchmarks?: { benchmark: string; values: Record<string, number> }[];
  rules_sentence?: string;
  match_number?: number;
  winner?: string;
  report_link?: string;
};

type CommentaryLine = {
  voice_role: string;
  model: string;
  start_frame: number;
  end_frame: number;
  text: string;
  event_ids: string[];
};

type Manifest = {
  schema: string;
  fps: number;
  identities: string[];
  source_bundle_hash: string;
  scenes: Scene[];
  commentary: CommentaryLine[];
  terminal?: TerminalEvent[];
  arena_visuals?: ArenaPlan | null;
};

/* ------------------------------------------------------------------ */
/* Shared chrome: grid, scanlines, vignette, timestamp                 */
/* ------------------------------------------------------------------ */

const Chrome: React.FC<{ showTimestamp?: boolean }> = ({ showTimestamp = true }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const flicker = 0.92 + 0.08 * Math.sin(frame * 0.9);
  const t = frame / fps;
  const mm = String(Math.floor(t / 60)).padStart(2, '0');
  const ss = String(Math.floor(t % 60)).padStart(2, '0');
  return (
    <>
      <AbsoluteFill
        style={{
          background:
            'repeating-linear-gradient(0deg, rgba(255,255,255,0.022) 0px, rgba(255,255,255,0.022) 1px, transparent 1px, transparent 3px)',
          opacity: flicker,
          mixBlendMode: 'overlay',
        }}
      />
      <AbsoluteFill
        style={{
          background:
            'linear-gradient(rgba(77,156,255,0.05), transparent 30%), radial-gradient(ellipse at center, transparent 55%, rgba(0,0,0,0.55) 100%)',
        }}
      />
      {showTimestamp && (
        <div
          style={{
            position: 'absolute',
            bottom: 18,
            right: 26,
            color: DIM,
            fontFamily: MONO,
            fontSize: 16,
            letterSpacing: 2,
          }}
        >
          SAND#001 · T+{mm}:{ss}
        </div>
      )}
    </>
  );
};

const Grid: React.FC = () => (
  <AbsoluteFill
    style={{
      background:
        'repeating-linear-gradient(90deg, rgba(77,156,255,0.05) 0px, rgba(77,156,255,0.05) 1px, transparent 1px, transparent 64px), repeating-linear-gradient(0deg, rgba(77,156,255,0.05) 0px, rgba(77,156,255,0.05) 1px, transparent 1px, transparent 64px)',
    }}
  />
);

/* ------------------------------------------------------------------ */
/* Scene 1: cold open                                                  */
/* ------------------------------------------------------------------ */

const ColdOpen: React.FC = () => {
  const frame = useCurrentFrame();
  const inP = spring({ frame, fps: 30, config: { damping: 14 } });
  const cursor = frame % 60 < 30 ? '▊' : ' ';
  return (
    <AbsoluteFill style={{ background: NAVY }}>
      <Grid />
      <Chrome showTimestamp={false} />
      <AbsoluteFill style={{ justifyContent: 'center', alignItems: 'center', textAlign: 'center' }}>
        <div
          style={{
            color: DIM,
            fontFamily: MONO,
            fontSize: 20,
            letterSpacing: 8,
            marginBottom: 28,
          }}
        >
          SERIES 001 · MATCH PREVIEW
        </div>
        <div
          style={{
            color: INK,
            fontFamily: MONO,
            fontWeight: 700,
            fontSize: 118,
            letterSpacing: 14,
            textShadow: `0 0 42px ${COBALT}, 0 0 8px ${COBALT}`,
            opacity: inP,
            transform: `translateY(${interpolate(inP, [0, 1], [40, 0])}px)`,
          }}
        >
          SANDBOXER
        </div>
        <div
          style={{
            color: COBALT,
            fontFamily: MONO,
            fontSize: 22,
            letterSpacing: 3,
            marginTop: 18,
            opacity: inP,
          }}
        >
          A simulated capture-the-flag between two isolated models
        </div>
        <div
          style={{
            color: DIM,
            fontFamily: MONO,
            fontSize: 18,
            marginTop: 54,
          }}
        >
          initializing arena{cursor}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

/* ------------------------------------------------------------------ */
/* Scene 2: model cards + rules                                        */
/* ------------------------------------------------------------------ */

const BenchmarkBars: React.FC<{ scene: Scene }> = ({ scene }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const rows = scene.benchmarks ?? [];
  return (
    <div style={{ marginTop: 22, width: '100%' }}>
      {rows.map((row, r) => {
        const [left, right] = scene.identities ?? [];
        const lv = row.values[left] ?? 0;
        const rv = row.values[right] ?? 0;
        const grow = spring({ frame: frame - r * 4, fps, config: { damping: 16 } });
        return (
          <div key={row.benchmark} style={{ marginBottom: 14 }}>
            <div style={{ color: DIM, fontFamily: MONO, fontSize: 15, marginBottom: 6 }}>
              {row.benchmark}
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <div
                style={{
                  height: 10,
                  flex: 1,
                  background: 'rgba(255,255,255,0.06)',
                  borderRadius: 2,
                  overflow: 'hidden',
                }}
              >
                <div
                  style={{
                    width: `${lv * 100 * grow}%`,
                    height: '100%',
                    background: ACCENTS[left] ?? COBALT,
                    boxShadow: `0 0 10px ${ACCENTS[left] ?? COBALT}`,
                  }}
                />
              </div>
              <span style={{ color: INK, fontFamily: MONO, fontSize: 14, width: 42, textAlign: 'right' }}>
                {Math.round(lv * 100)}%
              </span>
              <div
                style={{
                  height: 10,
                  flex: 1,
                  background: 'rgba(255,255,255,0.06)',
                  borderRadius: 2,
                  overflow: 'hidden',
                }}
              >
                <div
                  style={{
                    width: `${rv * 100 * grow}%`,
                    height: '100%',
                    background: ACCENTS[right] ?? AMBER,
                    boxShadow: `0 0 10px ${ACCENTS[right] ?? AMBER}`,
                    marginLeft: 'auto',
                  }}
                />
              </div>
              <span style={{ color: INK, fontFamily: MONO, fontSize: 14, width: 42 }}>
                {Math.round(rv * 100)}%
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
};

const ModelCard: React.FC<{ name: string; meta: Record<string, unknown>; accent: string; side: 'left' | 'right' }> = ({
  name,
  meta,
  accent,
  side,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const inP = spring({ frame, fps, config: { damping: 16 } });
  const translate = side === 'left' ? -60 : 60;
  return (
    <div
      style={{
        width: '46%',
        padding: '34px 30px',
        background: PANEL,
        border: `1px solid ${accent}44`,
        boxShadow: `0 0 34px ${accent}22, inset 0 0 24px ${accent}0d`,
        opacity: inP,
        transform: `translateX(${interpolate(inP, [0, 1], [translate, 0])}px)`,
      }}
    >
      <div style={{ color: accent, fontFamily: MONO, fontSize: 15, letterSpacing: 3, marginBottom: 8 }}>
        {side === 'left' ? '◤ COMPETITOR A' : 'COMPETITOR B ◢'}
      </div>
      <div style={{ color: INK, fontFamily: MONO, fontWeight: 700, fontSize: 34, marginBottom: 6 }}>
        {name}
      </div>
      <div style={{ color: DIM, fontFamily: MONO, fontSize: 17, marginBottom: 4 }}>
        producer: {String(meta.producer ?? 'unknown')}
      </div>
      <div style={{ color: DIM, fontFamily: MONO, fontSize: 17 }}>
        {Object.entries(meta)
          .filter(([k]) => k !== 'producer' && k !== 'architecture')
          .map(([k, v]) => `${k}: ${String(v)}`)
          .join(' · ')}
      </div>
    </div>
  );
};

const ModelCardsAndRules: React.FC<{ scene: Scene }> = ({ scene }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const [a, b] = scene.identities ?? [];
  const rulesIn = spring({ frame: frame - 30, fps, config: { damping: 18 } });
  return (
    <AbsoluteFill style={{ background: NAVY_2 }}>
      <Grid />
      <Chrome />
      <AbsoluteFill style={{ padding: '70px 90px' }}>
        <div style={{ color: DIM, fontFamily: MONO, fontSize: 18, letterSpacing: 6, marginBottom: 30 }}>
          // COMPETITORS
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <ModelCard name={a} meta={scene.metadata?.[a] ?? {}} accent={ACCENTS[a] ?? COBALT} side="left" />
          <ModelCard name={b} meta={scene.metadata?.[b] ?? {}} accent={ACCENTS[b] ?? AMBER} side="right" />
        </div>
        <BenchmarkBars scene={scene} />
        <div
          style={{
            marginTop: 30,
            padding: '20px 24px',
            border: '1px solid rgba(138,151,173,0.3)',
            color: INK,
            fontFamily: MONO,
            fontSize: 18,
            lineHeight: 1.6,
            opacity: rulesIn,
          }}
        >
          <span style={{ color: CYAN }}>RULES ▸ </span>
          {scene.rules_sentence}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

/* ------------------------------------------------------------------ */
/* Scene 3: the uncut Match — permanent split, live terminals          */
/* ------------------------------------------------------------------ */

const TerminalPane: React.FC<{
  name: string;
  accent: string;
  events: TerminalEvent[];
  localBase: number;
}> = ({ name, accent, events, localBase }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const cursor = frame % 50 < 25 ? '▊' : ' ';
  const visible = events.filter((e) => frame >= e.at_frame - localBase - 60);
  return (
    <div
      style={{
        width: '50%',
        height: '100%',
        boxSizing: 'border-box',
        display: 'flex',
        flexDirection: 'column',
        background: 'linear-gradient(180deg, rgba(0,0,0,0.25), rgba(0,0,0,0.55))',
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '16px 20px',
          borderBottom: `1px solid ${accent}33`,
          background: `${accent}14`,
        }}
      >
        <span style={{ color: accent, fontFamily: MONO, fontSize: 16, fontWeight: 700, letterSpacing: 2 }}>
          ◈ {name}
        </span>
        <span style={{ color: DIM, fontFamily: MONO, fontSize: 13 }}>
          isolated runner · no egress
        </span>
      </div>
      <div style={{ flex: 1, padding: '20px 22px', fontFamily: MONO, fontSize: 18, lineHeight: 1.75, overflow: 'hidden' }}>
        {visible.map((e, i) => {
          const appear = e.at_frame - localBase;
          const p = interpolate(frame, [appear, appear + 24], [0, 1], {
            extrapolateLeft: 'clamp',
            extrapolateRight: 'clamp',
          });
          const phase = e.phase === 'red' ? RED : e.phase === 'blue' ? CYAN : DIM;
          return (
            <div key={e.event_id} style={{ opacity: p, transform: `translateY(${(1 - p) * 8}px)` }}>
              <span style={{ color: phase }}>[{e.phase.toUpperCase()}]</span>{' '}
              <span style={{ color: INK }}>{e.text}</span>{' '}
              <span style={{ color: DIM, fontSize: 13 }}>#{e.event_id}</span>
            </div>
          );
        })}
        {frame > 10 && <span style={{ color: accent }}>{cursor}</span>}
      </div>
    </div>
  );
};

const CaptionBar: React.FC<{ manifest: Manifest; localBase: number; bottom?: number }> = ({ manifest, localBase, bottom = 64 }) => {
  const frame = useCurrentFrame();
  const global = frame + localBase;
  const line = manifest.commentary.find((c) => global >= c.start_frame && global < c.end_frame);
  if (!line) return null;
  const p = interpolate(global, [line.start_frame, line.start_frame + 8], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  return (
    <div
      style={{
        position: 'absolute',
        bottom,
        left: '50%',
        transform: 'translateX(-50%)',
        maxWidth: '80%',
        padding: '12px 22px',
        background: 'rgba(3,7,17,0.88)',
        border: '1px solid rgba(77,156,255,0.35)',
        color: INK,
        fontFamily: MONO,
        fontSize: 20,
        textAlign: 'center',
        opacity: p,
      }}
    >
      <span style={{ color: ACCENTS[line.model] ?? COBALT, marginRight: 10 }}>{line.voice_role === 'analyst' ? '△' : '▸'}</span>
      {line.text}
    </div>
  );
};

const MatchScene: React.FC<{ manifest: Manifest; scene: Scene; localBase: number }> = ({ manifest, scene, localBase }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const [a, b] = manifest.identities;
  const events = (manifest.terminal ?? []).filter((e) => e.at_frame >= localBase && e.at_frame < localBase + scene.duration_frames);
  const left = events.filter((e) => e.pane === 0);
  const right = events.filter((e) => e.pane === 1);
  const phasePulse = 0.5 + 0.5 * Math.sin(frame * 0.12);
  const redActive = events.some((e) => e.phase === 'red' && frame >= e.at_frame - localBase - 30);
  const phaseColor = redActive ? RED : CYAN;
  const arena = manifest.arena_visuals;
  const arenaHeight = arena ? 280 : 0;
  return (
    <AbsoluteFill style={{ background: NAVY }}>
      <Grid />
      <Chrome />
      <div
        style={{
          position: 'absolute',
          top: 16,
          left: '50%',
          transform: 'translateX(-50%)',
          zIndex: 5,
          padding: '8px 26px',
          background: 'rgba(3,7,17,0.9)',
          border: `1px solid ${phaseColor}66`,
          color: phaseColor,
          fontFamily: MONO,
          fontSize: 15,
          letterSpacing: 4,
          opacity: 0.7 + 0.3 * phasePulse,
        }}
      >
        {redActive ? '● RED PHASE — ATTACK' : '◉ BLUE PHASE — DEFENSE'}
      </div>
      <div style={{ position: 'absolute', top: 46, left: 0, right: 0, bottom: arenaHeight, display: 'flex', flexDirection: 'row' }}>
        <TerminalPane name={a} accent={ACCENTS[a] ?? COBALT} events={left} localBase={localBase} />
        <div style={{ width: 2, background: 'rgba(77,156,255,0.25)' }} />
        <TerminalPane name={b} accent={ACCENTS[b] ?? AMBER} events={right} localBase={localBase} />
      </div>
      {arena && (
        <div style={{ position: 'absolute', bottom: 0, left: 0, right: 0, height: arenaHeight, zIndex: 3 }}>
          <ArenaVisual plan={arena} identities={[a, b]} terminal={manifest.terminal ?? []} localBase={localBase} height={arenaHeight} />
        </div>
      )}
      <div
        style={{
          position: 'absolute',
          top: 18,
          right: 26,
          color: DIM,
          fontFamily: MONO,
          fontSize: 13,
        }}
      >
        MATCH 1 · UNCUT · {fps}fps
      </div>
    </AbsoluteFill>
  );
};

/* ------------------------------------------------------------------ */
/* Scene 4: factual recap                                              */
/* ------------------------------------------------------------------ */

const FactualRecap: React.FC<{ scene: Scene; manifest: Manifest }> = ({ scene, manifest }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const winner = scene.winner;
  const accent = winner ? ACCENTS[winner] ?? COBALT : COBALT;
  const inP = spring({ frame, fps, config: { damping: 14 } });
  return (
    <AbsoluteFill style={{ background: NAVY }}>
      <Grid />
      <Chrome />
      <AbsoluteFill style={{ justifyContent: 'center', alignItems: 'center', textAlign: 'center' }}>
        <div style={{ color: DIM, fontFamily: MONO, fontSize: 18, letterSpacing: 6, marginBottom: 26 }}>
          // FACTUAL RECAP
        </div>
        <div
          style={{
            color: INK,
            fontFamily: MONO,
            fontSize: 30,
            lineHeight: 1.7,
            maxWidth: 900,
            opacity: inP,
          }}
        >
          Both models defended their service and retrieved the objective.
          <br />
          <span style={{ color: accent, textShadow: `0 0 24px ${accent}` }}>{winner}</span>{' '}
          exhausted its declared budget first.
        </div>
        <div
          style={{
            marginTop: 44,
            padding: '14px 26px',
            border: '1px solid rgba(138,151,173,0.35)',
            color: DIM,
            fontFamily: MONO,
            fontSize: 16,
          }}
        >
          report: <span style={{ color: COBALT }}>{scene.report_link}</span>
        </div>
        <div style={{ marginTop: 30, color: DIM, fontFamily: MONO, fontSize: 14 }}>
          source_bundle {manifest.source_bundle_hash.slice(0, 16)}…
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

/* ------------------------------------------------------------------ */
/* Root composition                                                    */
/* ------------------------------------------------------------------ */

export const SeriesVideo: React.FC<{ manifest: Manifest }> = ({ manifest }) => {
  const frame = useCurrentFrame();
  const coldOpenFrames = manifest.scenes[0]?.duration_frames ?? 0;
  const modelCardsFrames = manifest.scenes[1]?.duration_frames ?? 0;
  const matchStart = coldOpenFrames + modelCardsFrames;
  const arenaHeight = manifest.arena_visuals ? 280 : 0;
  const captionBottom = frame >= matchStart ? arenaHeight + 16 : 64;
  let at = 0;
  return (
    <AbsoluteFill style={{ background: NAVY }}>
      {manifest.scenes.map((scene, index) => {
        const from = at;
        at += scene.duration_frames;
        return (
          <Sequence key={`${scene.type}-${index}`} from={from} durationInFrames={scene.duration_frames}>
            {scene.type === 'cold_open' && <ColdOpen />}
            {scene.type === 'model_cards_and_rules' && <ModelCardsAndRules scene={scene} />}
            {scene.type === 'match' && <MatchScene manifest={manifest} scene={scene} localBase={from} />}
            {scene.type === 'factual_recap' && <FactualRecap scene={scene} manifest={manifest} />}
            {scene.type === 'intermission' && (
              <AbsoluteFill style={{ justifyContent: 'center', alignItems: 'center' }}>
                <div style={{ color: DIM, fontFamily: MONO, fontSize: 30, letterSpacing: 10 }}>INTERMISSION</div>
              </AbsoluteFill>
            )}
          </Sequence>
        );
      })}
      <CaptionBar manifest={manifest} localBase={0} bottom={captionBottom} />
    </AbsoluteFill>
  );
};

const fallbackManifest: Manifest = {
  schema: 'sandboxer.video-manifest.v1',
  fps: 30,
  identities: ['Laguna S 2.1', 'Muse Spark 1.2'],
  source_bundle_hash: 'runtime-required',
  scenes: [
    { type: 'cold_open', duration_frames: 240, event_ids: ['runtime'] },
    { type: 'model_cards_and_rules', duration_frames: 600, identities: ['Laguna S 2.1', 'Muse Spark 1.2'], event_ids: ['runtime'] },
    { type: 'match', duration_frames: 300, match_number: 1, event_ids: ['runtime'] },
    { type: 'factual_recap', duration_frames: 360, event_ids: ['runtime'] },
  ],
  commentary: [],
  terminal: [],
};

export const RemotionRoot: React.FC = () => (
  <Composition
    id="SandboxerSeries"
    component={SeriesVideo}
    width={1920}
    height={1080}
    defaultProps={{ manifest: fallbackManifest }}
    calculateMetadata={({ props }) => ({
      fps: props.manifest.fps,
      durationInFrames: props.manifest.scenes.reduce((sum, s) => sum + s.duration_frames, 0),
    })}
  />
);

registerRoot(RemotionRoot);
