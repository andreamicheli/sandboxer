import React from 'react';
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from 'remotion';

/* ------------------------------------------------------------------ */
/* Arena visualization: two avatars, defense artifacts, attack beats. */
/* The plan is LLM-drafted (sandboxer_v0/arena_visual.py); geometry   */
/* and timing here are deterministic so the render is reproducible.   */
/* ------------------------------------------------------------------ */

export type ArenaAvatar = {
  competitor: string;
  label: string;
  shape: string;
  accent?: string;
};

export type ArenaDefense = {
  id: string;
  competitor: string;
  label: string;
  shape: string;
  color?: string;
  event_ids?: string[];
};

export type ArenaBeat = {
  type: 'build' | 'attack';
  defense?: string;
  attacker?: string;
  target?: string;
  kind?: string;
  outcome?: string;
  start_frame: number;
  duration_frames?: number;
  event_ids?: string[];
};

export type ArenaPlan = {
  schema: string;
  avatars: ArenaAvatar[];
  defenses: ArenaDefense[];
  beats: ArenaBeat[];
};

type TerminalEvent = { at_frame: number; event_id: string };

const NAVY = '#030711';
const INK = '#f0eee7';
const DIM = '#8a97ad';
const RED = '#ff4d5e';
const CYAN = '#38e1c8';
const MONO = "'JetBrains Mono', 'Fira Code', 'SFMono-Regular', 'Consolas', monospace";

const AVATAR_HOME = { left: { x: 180, y: 210 }, right: { x: 1740, y: 210 } };
const SLOT_X = (index: number, side: 'left' | 'right') =>
  side === 'left' ? 480 + index * 130 : 1440 - index * 130;

const shapeStyle = (shape: string, color: string): React.CSSProperties => {
  const base: React.CSSProperties = { width: 96, height: 96, background: color, border: `1px solid ${color}` };
  switch (shape) {
    case 'sphere':
      return { ...base, borderRadius: '50%', background: `radial-gradient(circle at 35% 30%, ${color}cc, ${color}55)` };
    case 'pyramid':
      return { ...base, clipPath: 'polygon(50% 0%, 100% 100%, 0% 100%)', background: color };
    case 'hex':
      return { ...base, clipPath: 'polygon(25% 5%, 75% 5%, 100% 50%, 75% 95%, 25% 95%, 0% 50%)' };
    case 'shield':
      return { ...base, clipPath: 'polygon(50% 0%, 92% 14%, 92% 60%, 50% 100%, 8% 60%, 8% 14%)' };
    case 'cube':
    default:
      return { ...base, borderRadius: 10, background: `linear-gradient(135deg, ${color}, ${color}66)` };
  }
};

const avatarStyle = (shape: string, accent: string): React.CSSProperties => {
  const base: React.CSSProperties = {
    width: 104,
    height: 104,
    background: `${accent}22`,
    border: `2px solid ${accent}`,
    boxShadow: `0 0 24px ${accent}55`,
    borderRadius: '50%',
  };
  switch (shape) {
    case 'diamond':
      return { ...base, borderRadius: 0, transform: 'rotate(45deg)' };
    case 'hexagon':
      return { ...base, borderRadius: 0, clipPath: 'polygon(25% 5%, 75% 5%, 100% 50%, 75% 95%, 25% 95%, 0% 50%)' };
    case 'triangle':
      return { ...base, borderRadius: 0, clipPath: 'polygon(50% 0%, 100% 100%, 0% 100%)' };
    case 'circle':
    default:
      return base;
  }
};

export const ArenaVisual: React.FC<{
  plan: ArenaPlan | null | undefined;
  identities: string[];
  terminal: TerminalEvent[];
  localBase: number;
}> = ({ plan, identities, terminal, localBase }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  if (!plan) return null;
  const [a, b] = identities;

  const avatarBy = (name: string) => plan.avatars.find((av) => av.competitor === name);
  const accentOf = (name: string, fallback: string) => avatarBy(name)?.accent ?? fallback;
  const accentA = accentOf(a, '#9a65e8');
  const accentB = accentOf(b, '#58a9ff');

  const defensesFor = (name: string) => plan.defenses.filter((d) => d.competitor === name);

  const localTime = (beat: { event_ids?: string[]; start_frame: number }) => {
    if (beat.event_ids?.length) {
      const ev = terminal.find((t) => beat.event_ids?.includes(t.event_id));
      if (ev) return ev.at_frame - localBase;
    }
    return beat.start_frame;
  };

  const buildTime = (d: ArenaDefense) => {
    const ev = terminal.find((t) => d.event_ids?.includes(t.event_id));
    return ev ? ev.at_frame - localBase : 0;
  };

  const activeAttack = plan.beats.find((b) => {
    if (b.type !== 'attack') return false;
    const start = localTime(b);
    const dur = b.duration_frames ?? 120;
    return frame >= start && frame < start + dur;
  });

  const renderAvatar = (name: string, home: { x: number; y: number }, accent: string) => {
    let dx = 0;
    let dy = 0;
    if (activeAttack && activeAttack.attacker === name) {
      const target = plan.defenses.find((d) => d.id === activeAttack.target);
      const side = name === a ? 'right' : 'left';
      const tIndex = defensesFor(target?.competitor ?? '').findIndex((d) => d.id === target?.id);
      const tx = target ? SLOT_X(Math.max(tIndex, 0), side) : home.x;
      const ty = 210;
      const start = localTime(activeAttack);
      const p = interpolate(frame, [start, start + 24], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
      dx = (tx - home.x) * 0.55 * p;
      dy = (ty - home.y) * p;
    }
    const bob = Math.sin(frame * 0.1) * 4;
    return (
      <div
        key={name}
        style={{
          position: 'absolute',
          left: home.x + dx - 52,
          top: home.y + dy - 52 + bob,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <div style={avatarStyle(avatarBy(name)?.shape ?? 'circle', accent)} />
        <div
          style={{
            position: 'absolute',
            bottom: -34,
            color: accent,
            fontFamily: MONO,
            fontSize: 16,
            letterSpacing: 2,
            fontWeight: 700,
          }}
        >
          {avatarBy(name)?.label ?? name}
        </div>
      </div>
    );
  };

  const renderDefense = (d: ArenaDefense, side: 'left' | 'right', index: number) => {
    const x = SLOT_X(index, side);
    const built = buildTime(d);
    const p = spring({ frame: frame - built, fps, config: { damping: 14 } });
    const isTarget = activeAttack?.target === d.id;
    const shake = isTarget
      ? Math.sin(frame * 0.9) * 8 * (activeAttack?.outcome === 'breached' ? 1 : 0.6)
      : 0;
    const flash = isTarget && activeAttack?.outcome === 'breached' ? RED : isTarget ? CYAN : (d.color ?? '#4d9cff');
    const color = isTarget ? flash : (d.color ?? '#4d9cff');
    return (
      <div
        key={d.id}
        style={{
          position: 'absolute',
          left: x - 48,
          top: 210 - 48,
          opacity: p,
          transform: `scale(${p}) translateX(${shake}px)`,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
        }}
      >
        <div style={{ ...shapeStyle(d.shape, color), boxShadow: isTarget ? `0 0 26px ${flash}` : undefined }} />
        <div style={{ color: DIM, fontFamily: MONO, fontSize: 13, marginTop: 10, letterSpacing: 1 }}>{d.label}</div>
      </div>
    );
  };

  const renderAttackPath = () => {
    if (!activeAttack) return null;
    const target = plan.defenses.find((d) => d.id === activeAttack.target);
    if (!target) return null;
    const side = activeAttack.attacker === a ? 'right' : 'left';
    const home = activeAttack.attacker === a ? AVATAR_HOME.left : AVATAR_HOME.right;
    const tIndex = defensesFor(target.competitor).findIndex((d) => d.id === target.id);
    const tx = SLOT_X(Math.max(tIndex, 0), side);
    const ty = 210;
    const start = localTime(activeAttack);
    const dur = activeAttack.duration_frames ?? 120;
    const p = interpolate(frame, [start, start + dur], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
    const cx = home.x + (tx - home.x) * p;
    const cy = home.y + (ty - home.y) * p;
    return (
      <svg width="1920" height="420" style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
        <line x1={home.x} y1={home.y} x2={tx} y2={ty} stroke={activeAttack.outcome === 'breached' ? RED : CYAN} strokeOpacity={0.35} strokeDasharray="6 8" strokeWidth={2} />
        <circle cx={cx} cy={cy} r={7} fill={activeAttack.outcome === 'breached' ? RED : CYAN} style={{ filter: `drop-shadow(0 0 8px ${activeAttack.outcome === 'breached' ? RED : CYAN})` }} />
      </svg>
    );
  };

  return (
    <AbsoluteFill style={{ position: 'absolute', inset: 0, height: 420, background: `linear-gradient(180deg, ${NAVY}, rgba(5,11,24,0.2))`, borderBottom: '1px solid rgba(77,156,255,0.2)', overflow: 'hidden' }}>
      <svg width="1920" height="420" style={{ position: 'absolute', inset: 0 }}>
        <line x1={960} y1={20} x2={960} y2={400} stroke="rgba(77,156,255,0.18)" strokeDasharray="3 6" />
      </svg>
      <div style={{ position: 'absolute', top: 10, left: 20, color: DIM, fontFamily: MONO, fontSize: 12, letterSpacing: 3 }}>
        ARENA · {a.toUpperCase()} <span style={{ color: accentA }}>◈</span> vs <span style={{ color: accentB }}>◈</span> {b.toUpperCase()}
      </div>
      {renderAvatar(a, AVATAR_HOME.left, accentA)}
      {renderAvatar(b, AVATAR_HOME.right, accentB)}
      {defensesFor(a).map((d, i) => renderDefense(d, 'left', i))}
      {defensesFor(b).map((d, i) => renderDefense(d, 'right', i))}
      {renderAttackPath()}
      {activeAttack && (
        <div
          style={{
            position: 'absolute',
            bottom: 8,
            left: '50%',
            transform: 'translateX(-50%)',
            color: activeAttack.outcome === 'breached' ? RED : CYAN,
            fontFamily: MONO,
            fontSize: 13,
            letterSpacing: 3,
          }}
        >
          {activeAttack.outcome === 'breached' ? '▲ BREACH' : '◆ BLOCKED'} · {activeAttack.kind}
        </div>
      )}
    </AbsoluteFill>
  );
};
