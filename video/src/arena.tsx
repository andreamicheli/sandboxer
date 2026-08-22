import React from 'react';
import {
  AbsoluteFill,
  Easing,
  Img,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import { logoFile, logoOf } from './logos';

/* ------------------------------------------------------------------ */
/* Arena visualization: two competitor avatars (official brand logos), */
/* defense artifacts, and attack beats.  The plan is LLM-drafted        */
/* (sandboxer_v0/arena_visual.py); geometry and timing here remain      */
/* deterministic so the render is reproducible.  When a competitor has  */
/* no registered logo we fall back to the legacy geometric avatar.      */
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

/* Deterministic particle field: the same attack always yields the same
 * burst, so the composition stays reproducible frame-for-frame. */
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
const PARTICLE_COUNT = 20;

/* Win-probability bar: each beat contributes head-to-head "momentum" that
 * ramps in over MOMENTUM_RAMP frames.  Defense quality (builds), movement
 * quality (attack activity) and outcomes (breach/block/partial) all count;
 * the differential is mapped to a bounded probability with a logistic so the
 * bar starts at 50/50 and leans toward whoever is ahead, never fully 0/100. */
const WIN_BAR_HEIGHT = 48;
const MOMENTUM_RAMP = 40;
const WIN_K = 0.4;
const WIN_FLOOR = 0.06;
const WIN_CEIL = 0.94;

/* ---- per-side defense layout -------------------------------------- */
/* Defenses are laid out per side from the count of artifacts plus the
 * avatar home: slots start near the center line and march toward the
 * avatar at a fixed full-size pitch.  When that row would overrun the
 * span between the center line (960) and the avatar home (180 / 1740),
 * the whole arena content is scaled down by a deterministic factor so
 * everything shrinks instead of overlapping. */
const CANVAS_W = 1920;
const CENTER_X = CANVAS_W / 2;
const HOME_X = { left: 180, right: 1740 };
const SIDE_SPAN = CENTER_X - HOME_X.left; // 780px on both sides
const SHAPE_BASE = 96;
const HALF_SHAPE = SHAPE_BASE / 2;
const DEFENSE_STEP = 150; // pitch between neighbouring defense centers
const FIRST_SLOT_OFFSET = 140; // first slot distance from the center line
const AVATAR_CLEARANCE = 72; // breathing room before the avatar badge

const slotX = (index: number, side: 'left' | 'right') => {
  const dist = FIRST_SLOT_OFFSET + index * DEFENSE_STEP;
  return side === 'left' ? CENTER_X - dist : CENTER_X + dist;
};

/* Span from the center line toward the avatar home needed by n defenses. */
const spanNeeded = (count: number) =>
  count > 0 ? FIRST_SLOT_OFFSET + (count - 1) * DEFENSE_STEP + HALF_SHAPE + AVATAR_CLEARANCE : 0;

const shapeStyle = (shape: string, color: string, size = SHAPE_BASE): React.CSSProperties => {
  const base: React.CSSProperties = { width: size, height: size, background: color, border: `1px solid ${color}` };
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

const avatarShapeStyle = (shape: string, accent: string): React.CSSProperties => {
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
  height?: number;
}> = ({ plan, identities, terminal, localBase, height = 420 }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  if (!plan) return null;
  const H = height;
  const midY = (H - WIN_BAR_HEIGHT) / 2;
  const HOME = { left: { x: 180, y: midY }, right: { x: 1740, y: midY } };
  const [a, b] = identities;

  const avatarBy = (name: string) => plan.avatars.find((av) => av.competitor === name);
  const accentOf = (name: string, fallback: string) => {
    const spec = logoOf(name);
    return spec?.accent ?? avatarBy(name)?.accent ?? fallback;
  };
  const accentA = accentOf(a, '#9a65e8');
  const accentB = accentOf(b, '#58a9ff');

  const defensesFor = (name: string) => plan.defenses.filter((d) => d.competitor === name);

  /* Global zoom-out factor: the tighter side dictates how much the whole
   * arena shrinks (1 = natural size).  Deterministic in the defense counts. */
  const fitScale = Math.min(
    1,
    SIDE_SPAN / Math.max(spanNeeded(defensesFor(a).length), spanNeeded(defensesFor(b).length), 1),
  );

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

  const attackFor = (b: ArenaBeat) => {
    const start = localTime(b);
    const dur = b.duration_frames ?? 120;
    return { start, dur, end: start + dur };
  };

  /* ---- win probability: momentum from defense quality, movement, attacks ---- */
  const winProb = (() => {
    let ma = 0;
    let mb = 0;
    for (const beat of plan.beats) {
      const t = localTime(beat);
      const progress = interpolate(frame, [t, t + MOMENTUM_RAMP], [0, 1], {
        extrapolateLeft: 'clamp',
        extrapolateRight: 'clamp',
        easing: Easing.inOut(Easing.quad),
      });
      let da = 0;
      let db = 0;
      if (beat.type === 'build') {
        // defense quality: establishing a defense
        const owner = plan.defenses.find((d) => d.id === beat.defense)?.competitor;
        if (owner === a) da += 1.0;
        else if (owner === b) db += 1.0;
      } else {
        const defender = plan.defenses.find((d) => d.id === beat.target)?.competitor;
        // movement quality: every attack attempt signals activity
        if (beat.attacker === a) da += 0.5;
        else if (beat.attacker === b) db += 0.5;
        if (beat.outcome === 'breached') {
          if (beat.attacker === a) da += 2.0;
          else if (beat.attacker === b) db += 2.0;
          if (defender === a) da -= 0.5;
          else if (defender === b) db -= 0.5;
        } else if (beat.outcome === 'blocked') {
          if (defender === a) da += 1.5;
          else if (defender === b) db += 1.5;
        } else if (beat.outcome === 'partial') {
          if (beat.attacker === a) da += 1.0;
          else if (beat.attacker === b) db += 1.0;
        }
      }
      ma += da * progress;
      mb += db * progress;
    }
    const diff = ma - mb;
    const raw = 1 / (1 + Math.exp(-WIN_K * diff));
    const probA = Math.min(WIN_CEIL, Math.max(WIN_FLOOR, raw));
    return { probA, probB: 1 - probA };
  })();

  const activeAttack = plan.beats.find((b) => {
    if (b.type !== 'attack') return false;
    const { start, end } = attackFor(b);
    return frame >= start && frame < end;
  });

  const targetPosition = (attack: ArenaBeat) => {
    const target = plan.defenses.find((d) => d.id === attack.target);
    if (!target) return { x: CENTER_X, y: midY };
    const side = attack.attacker === a ? 'right' : 'left';
    const tIndex = defensesFor(target.competitor).findIndex((d) => d.id === target.id);
    return { x: slotX(Math.max(tIndex, 0), side), y: midY };
  };

  /* ---- avatar (logo badge, with legacy shape fallback) ---- */
  const renderAvatar = (name: string, home: { x: number; y: number }, accent: string) => {
    const spec = logoOf(name);
    const ringColor = spec?.accent ?? accent;
    let dx = 0;
    let dy = 0;
    if (activeAttack && activeAttack.attacker === name) {
      const { x: tx, y: ty } = targetPosition(activeAttack);
      const { start } = attackFor(activeAttack);
      // ease-in lunge toward the target, then recoil after impact
      const lunge = interpolate(frame, [start, start + 22, start + 44], [0, 1, 0.12], {
        extrapolateLeft: 'clamp',
        extrapolateRight: 'clamp',
        easing: Easing.inOut(Easing.cubic),
      });
      dx = (tx - home.x) * 0.55 * lunge;
      dy = (ty - home.y) * lunge;
    }
    const bob = Math.sin(frame * 0.1) * 4;
    const pulse = 0.72 + 0.28 * Math.sin(frame * 0.16);
    const logoSpec = logoOf(name);
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
        {logoSpec ? (
          <div
            style={{
              width: 104,
              height: 104,
              borderRadius: '50%',
              background: `radial-gradient(circle at 35% 28%, ${ringColor}2e, rgba(3,7,17,0.92) 72%)`,
              border: `2px solid ${ringColor}`,
              boxShadow: `0 0 ${26 * pulse}px ${ringColor}66`,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              padding: 16,
              boxSizing: 'border-box',
            }}
          >
            <Img
              src={logoFile(logoSpec)}
              style={{
                width: '100%',
                height: '100%',
                objectFit: 'contain',
                filter: logoSpec.raster ? 'brightness(0) invert(1)' : undefined,
              }}
            />
          </div>
        ) : (
          <div style={avatarShapeStyle(avatarBy(name)?.shape ?? 'circle', ringColor)} />
        )}
        <div
          style={{
            position: 'absolute',
            bottom: -34,
            color: ringColor,
            fontFamily: MONO,
            fontSize: 16,
            letterSpacing: 2,
            fontWeight: 700,
            textShadow: `0 0 12px ${ringColor}66`,
          }}
        >
          {avatarBy(name)?.label ?? name}
        </div>
      </div>
    );
  };

  /* ---- defense artifact ---- */
  const renderDefense = (d: ArenaDefense, side: 'left' | 'right', index: number) => {
    const x = slotX(index, side);
    const size = SHAPE_BASE * fitScale;
    const built = buildTime(d);
    const p = spring({ frame: frame - built, fps, config: { damping: 13, mass: 0.7, stiffness: 120 } });
    const isTarget = activeAttack?.target === d.id;
    // decaying shake: full amplitude on impact, easing out to zero
    let shake = 0;
    let flash = d.color ?? '#4d9cff';
    if (isTarget && activeAttack) {
      const { end } = attackFor(activeAttack);
      const progress = interpolate(frame, [end - 6, end], [0, 1], {
        extrapolateLeft: 'clamp',
        extrapolateRight: 'clamp',
      });
      const decay = 1 - Easing.out(Easing.quad)(progress);
      const intensity = activeAttack.outcome === 'breached' ? 1 : 0.6;
      shake = Math.sin(frame * 0.9) * 8 * intensity * decay;
      flash = activeAttack.outcome === 'breached' ? RED : CYAN;
    }
    const color = isTarget ? flash : (d.color ?? '#4d9cff');
    return (
      <div
        key={d.id}
        style={{
          position: 'absolute',
          left: x - size / 2,
          top: midY - size / 2,
          opacity: p,
          transform: `scale(${p}) translateX(${shake}px)`,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
        }}
      >
        <div style={{ ...shapeStyle(d.shape, color, size), boxShadow: isTarget ? `0 0 26px ${flash}` : undefined }} />
        <div style={{ color: DIM, fontFamily: MONO, fontSize: 13, marginTop: 10, letterSpacing: 1 }}>{d.label}</div>
      </div>
    );
  };

  /* ---- attack projectile, trail and impact burst ---- */
  const renderAttack = () => {
    if (!activeAttack) return null;
    const { x: tx, y: ty } = targetPosition(activeAttack);
    const home = activeAttack.attacker === a ? HOME.left : HOME.right;
    const { start, dur, end } = attackFor(activeAttack);
    const p = interpolate(frame, [start, end], [0, 1], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
      easing: Easing.inOut(Easing.cubic),
    });
    const cx = home.x + (tx - home.x) * p;
    const cy = home.y + (ty - home.y) * p;
    const color = activeAttack.outcome === 'breached' ? RED : CYAN;
    const impactColor = activeAttack.outcome === 'blocked' ? CYAN : RED;
    const impactT = frame - end;

    // projectile trail: a few fading ghosts behind the head
    const trail = [0, 1, 2, 3, 4].map((i) => {
      const tp = Math.max(0, p - i * 0.07);
      return {
        x: home.x + (tx - home.x) * tp,
        y: home.y + (ty - home.y) * tp,
        r: 7 - i,
        opacity: 0.5 - i * 0.1,
      };
    });

    // impact shockwave ring + particle burst (breach = red, block = cyan)
    const ringP = Easing.out(Easing.cubic)(interpolate(impactT, [0, 22], [0, 1], { extrapolateRight: 'clamp' }));
    const particles =
      impactT >= 0 && impactT < 30
        ? Array.from({ length: PARTICLE_COUNT }, (_, i) => {
            const angle = i * GOLDEN_ANGLE;
            const dist = Easing.out(Easing.cubic)(interpolate(impactT, [0, 30], [0, 74], { extrapolateRight: 'clamp' }));
            return {
              x: tx + Math.cos(angle) * dist,
              y: ty + Math.sin(angle) * dist,
              r: 2 + (i % 3),
              opacity: interpolate(impactT, [0, 30], [1, 0], { extrapolateRight: 'clamp' }),
              color: i % 4 === 0 ? INK : impactColor,
            };
          })
        : [];

    return (
      <svg width="1920" height={H} style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
        <line
          x1={home.x}
          y1={home.y}
          x2={tx}
          y2={ty}
          stroke={color}
          strokeOpacity={0.28}
          strokeDasharray="6 8"
          strokeWidth={2}
        />
        {trail.map((t, i) => (
          <circle key={`trail-${i}`} cx={t.x} cy={t.y} r={t.r} fill={color} opacity={Math.max(0, t.opacity)} />
        ))}
        <circle cx={cx} cy={cy} r={7} fill={color} style={{ filter: `drop-shadow(0 0 8px ${color})` }} />
        {impactT >= 0 && impactT < 24 && (
          <circle
            cx={tx}
            cy={ty}
            r={10 + ringP * 46}
            fill="none"
            stroke={impactColor}
            strokeWidth={2.5}
            opacity={1 - ringP}
          />
        )}
        {particles.map((pt, i) => (
          <circle key={`p-${i}`} cx={pt.x} cy={pt.y} r={pt.r} fill={pt.color} opacity={pt.opacity} />
        ))}
      </svg>
    );
  };

  return (
    <AbsoluteFill
      style={{
        position: 'absolute',
        inset: 0,
        height: H,
        background: `linear-gradient(180deg, ${NAVY}, rgba(5,11,24,0.2))`,
        borderBottom: '1px solid rgba(77,156,255,0.2)',
        overflow: 'hidden',
      }}
    >
      {/* Arena content scales down uniformly (origin = canvas centre) when
          either side's defense row outgrows its span; the win bar below
          stays unscaled. */}
      <div
        style={{
          position: 'absolute',
          inset: 0,
          transform: `scale(${fitScale})`,
          transformOrigin: '50% 50%',
        }}
      >
        <svg width="1920" height={H} style={{ position: 'absolute', inset: 0 }}>
          <line x1={960} y1={20} x2={960} y2={H - 20} stroke="rgba(77,156,255,0.18)" strokeDasharray="3 6" />
        </svg>
        <div style={{ position: 'absolute', top: 10, left: 20, color: DIM, fontFamily: MONO, fontSize: 12, letterSpacing: 3 }}>
          ARENA · {a.toUpperCase()} <span style={{ color: accentA }}>◈</span> vs <span style={{ color: accentB }}>◈</span> {b.toUpperCase()}
        </div>
        {renderAvatar(a, HOME.left, accentA)}
        {renderAvatar(b, HOME.right, accentB)}
        {defensesFor(a).map((d, i) => renderDefense(d, 'left', i))}
        {defensesFor(b).map((d, i) => renderDefense(d, 'right', i))}
        {renderAttack()}
      </div>
      {/* ---- win-probability bar (starts 50/50, tracks momentum) ---- */}
      <div
        style={{
          position: 'absolute',
          left: 0,
          right: 0,
          bottom: 0,
          height: WIN_BAR_HEIGHT,
          boxSizing: 'border-box',
          padding: '6px 16px 7px',
          background: 'rgba(3,7,17,0.78)',
          borderTop: '1px solid rgba(77,156,255,0.22)',
        }}
      >
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'baseline',
            fontFamily: MONO,
            marginBottom: 5,
          }}
        >
          <span style={{ color: INK, fontSize: 12, letterSpacing: 2 }}>
            {avatarBy(a)?.label ?? a}{' '}
            <span style={{ color: accentA, fontWeight: 700 }}>{Math.round(winProb.probA * 100)}%</span>
          </span>
          <span style={{ color: DIM, fontSize: 10, letterSpacing: 4 }}>WIN PROBABILITY</span>
          <span style={{ color: INK, fontSize: 12, letterSpacing: 2 }}>
            <span style={{ color: accentB, fontWeight: 700 }}>{Math.round(winProb.probB * 100)}%</span>{' '}
            {avatarBy(b)?.label ?? b}
          </span>
        </div>
        <div
          style={{
            position: 'relative',
            height: 14,
            borderRadius: 3,
            background: 'rgba(255,255,255,0.05)',
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              position: 'absolute',
              left: 0,
              top: 0,
              bottom: 0,
              width: `${winProb.probA * 100}%`,
              background: `linear-gradient(90deg, ${accentA}aa, ${accentA})`,
              boxShadow: `0 0 12px ${accentA}88`,
            }}
          />
          <div
            style={{
              position: 'absolute',
              right: 0,
              top: 0,
              bottom: 0,
              width: `${winProb.probB * 100}%`,
              background: `linear-gradient(270deg, ${accentB}aa, ${accentB})`,
              boxShadow: `0 0 12px ${accentB}88`,
            }}
          />
          {/* 50/50 reference tick */}
          <div
            style={{
              position: 'absolute',
              left: '50%',
              top: 0,
              bottom: 0,
              width: 1,
              background: 'rgba(240,238,231,0.4)',
            }}
          />
          {/* current boundary */}
          <div
            style={{
              position: 'absolute',
              left: `${winProb.probA * 100}%`,
              top: -2,
              bottom: -2,
              width: 2,
              marginLeft: -1,
              background: INK,
              boxShadow: '0 0 8px rgba(240,238,231,0.9)',
            }}
          />
        </div>
      </div>
      {activeAttack && (
        <div
          style={{
            position: 'absolute',
            bottom: WIN_BAR_HEIGHT + 8,
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
