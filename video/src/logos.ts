import { staticFile } from 'remotion';

/* ------------------------------------------------------------------ */
/* Model logo registry.                                               */
/*                                                                     */
/* Every competitor identity (human-readable name or provider slug)   */
/* resolves to a brand logo stored under public/logos/, plus an accent */
/* colour used for the badge ring, glow and label.  Logos are the     */
/* official brand marks: vector SVG where available (Poolside, Meta,   */
/* OpenAI, Gemini, DeepSeek, Qwen, Zhipu/GLM, StepFun, Xiaomi MiMo,    */
/* Fish Audio) and the official apple-touch mark for Thinking Machines */
/* (PNG, transparent).                                                 */
/* ------------------------------------------------------------------ */

export type LogoSpec = {
  file: string;
  accent: string;
  /** true for raster logos that need a lightening filter on dark canvas */
  raster?: boolean;
};

export const LOGOS: Record<string, LogoSpec> = {
  poolside: { file: 'logos/poolside.svg', accent: '#4137ff' },
  meta: { file: 'logos/meta.svg', accent: '#4f8ef7' },
  openai: { file: 'logos/openai.svg', accent: '#10a37f' },
  gemini: { file: 'logos/gemini.svg', accent: '#3186ff' },
  deepseek: { file: 'logos/deepseek.svg', accent: '#4d6bfe' },
  qwen: { file: 'logos/qwen.svg', accent: '#7c6cf0' },
  zhipu: { file: 'logos/zhipu.svg', accent: '#4f7dff' },
  stepfun: { file: 'logos/stepfun.svg', accent: '#3396ff' },
  xiaomimimo: { file: 'logos/xiaomimimo.svg', accent: '#ff6900' },
  fishaudio: { file: 'logos/fishaudio.svg', accent: '#38e1c8' },
  thinkingmachines: { file: 'logos/thinkingmachines.png', accent: '#e8e8e8', raster: true },
};

/* Identity → brand keyword, checked in order (most specific first). */
const BRAND_MATCHERS: Array<[RegExp, string]> = [
  [/thinking|inkling|tml|t-machines/i, 'thinkingmachines'],
  [/xiaomi|mimo/i, 'xiaomimimo'],
  [/glm|zhipu|zai\b|z\.ai|chatglm/i, 'zhipu'],
  [/laguna|poolside/i, 'poolside'],
  [/muse|meta\b/i, 'meta'],
  [/deepseek|deep-seek/i, 'deepseek'],
  [/stepfun|step[ _-]?3|step[ _-]?2/i, 'stepfun'],
  [/qwen/i, 'qwen'],
  [/gemini/i, 'gemini'],
  [/fish|fishaudio/i, 'fishaudio'],
  [/luna|codex|gpt|openai|chatgpt/i, 'openai'],
];

/** Resolve a competitor identity to a brand key (or null when unknown). */
export const brandOf = (identity: string): string | null => {
  const key = identity.trim().toLowerCase();
  if (!key) return null;
  for (const [pattern, brand] of BRAND_MATCHERS) {
    if (pattern.test(key)) return brand;
  }
  return null;
};

/** Return the LogoSpec for an identity, or null when there is no logo. */
export const logoOf = (identity: string): LogoSpec | null => {
  const brand = brandOf(identity);
  return brand ? LOGOS[brand] ?? null : null;
};

/**
 * Brand accent colour for a competitor identity, taken straight from the
 * LOGOS registry so every scene (cards, bars, captions, arena) shares one
 * source of truth.  Unknown identities fall back to the caller's colour.
 */
export const accentOf = (identity: string | undefined, fallback: string): string =>
  (identity ? logoOf(identity)?.accent : null) ?? fallback;

/** Resolve a static-file path for a logo (safe for the Remotion bundler). */
export const logoFile = (spec: LogoSpec): string => staticFile(spec.file);
