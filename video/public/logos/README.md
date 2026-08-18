# Competitor brand logos

Official brand marks used to represent the models in the arena animation.
Rendered through `src/logos.ts` (identity → logo resolution) and loaded by the
Remotion composition via `staticFile()`.

| File | Brand | Source |
|---|---|---|
| `poolside.svg` | Poolside (Laguna) | lobe-icons `@lobehub/icons-static-svg` (`poolside-color`) |
| `meta.svg` | Meta (Muse Spark) | lobe-icons (`meta-color`) |
| `openai.svg` | OpenAI (Codex / Luna 5.6) | lobe-icons (`openai`), recoloured white |
| `gemini.svg` | Google Gemini | lobe-icons (`gemini-color`) |
| `deepseek.svg` | DeepSeek | lobe-icons (`deepseek-color`) |
| `qwen.svg` | Qwen (Alibaba) | lobe-icons (`qwen-color`) |
| `zhipu.svg` | Z.ai / Zhipu (GLM) | lobe-icons (`zhipu-color`) |
| `stepfun.svg` | StepFun (Step) | lobe-icons (`stepfun-color`) |
| `xiaomimimo.svg` | Xiaomi MiMo | lobe-icons (`xiaomimimo`), recoloured orange |
| `fishaudio.svg` | Fish Audio (TTS voices) | lobe-icons (`fishaudio`), recoloured cyan |
| `thinkingmachines.png` | Thinking Machines Lab (Inkling) | official `thinkingmachines.ai` apple-touch-icon |

The `currentColor` marks from lobe-icons (OpenAI, Xiaomi MiMo, Fish Audio) are
baked to a fixed colour here so they stay visible on the dark navy arena
without CSS inheritance.

These are third-party trademarks used nominally to identify the competitor
models; they are not affiliated with or endorsed by Sandboxer. Replace or drop
any mark on request.
