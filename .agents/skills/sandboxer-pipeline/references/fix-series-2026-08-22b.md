# 2026-08-22 second fix series — custom intro, provider-error classifier

Follow-up to `pipeline-fixes.md`. Commits on `agent/sandboxer-v0-backup`,
pushed (`c944d17..50165f6`).

## Custom intro replaces cold_open

- User authored an 8s 1280x720@24 h264+aac intro (Google Flow share link;
  downloadable via `https://labs.google/fx/api/og-video/shared/<id>` —
  the page HTML itself does not embed the mp4 URL). Stored at
  `video/public/assets/custom-intro.mp4`.
- `cold_open` scene REMOVED everywhere. `scenes[0]` is now
  `{"type":"custom_intro","duration_frames":round(8*fps)}` rendered by
  `CustomIntroScene` (OffthreadVideo + Chrome overlay). No commentary
  budget for it; schedule authority drops lines targeting unknown scenes.
- `INTRO_SCENES` narrowed to `frozenset({"model_cards_and_rules"})`;
  greeting fallback in commentary.py anchors there too.
- BUG FOUND AND FIXED during test alignment:
  `scene_starts["model_cards_and_rules"]` pointed at the scene's DURATION
  instead of its absolute offset, landing every intro line minutes late.
  Must be `scenes[0].duration_frames` (the intro length).
- When renaming/removing scenes: grep tests for the old scene string —
  commentary/schedule-authority/tts fixtures anchor by scene name and
  offsets must be recomputed against the new scene layout.

## Provider error classifier (safety-signal hygiene)

Incident: laguna turn died after 8 consecutive `api_retry`; the result
error text contained generic tokens ("server") and got classified as
`COMMAND_CODE_TOOL_BOUNDARY_FAILURE`, polluting a safety-relevant signal.

Fix (`eec4d4ea`):
1. `_result` classification: transport-failure tokens
   (overloaded/unavailable/timeout/timed out/connection/network/5xx/bad
   gateway/service error/**server error**) map to CAPACITY_UNAVAILABLE
   BEFORE the (mcp/tool/server) boundary branch. "server error" needed as
   its own token — "server error 500" matches none of the others.
2. `run_command_code_match.py`: `MAX_PROVIDER_RETRIES = 8`; `_frame_monitor`
   counts consecutive api_retry per model (reset on any other event), emits
   `provider_capacity_unavailable` telemetry, raises
   `COMMAND_CODE_CAPACITY_UNAVAILABLE` directly on breach.
3. `TOOL_BOUNDARY_FAILURE` keeps its string but is now emitted from exactly
   one site and only for genuine boundary messages ("mcp server
   disconnected" still classifies correctly).

Tests: transport-token parametrized classification, genuine-boundary case,
retry-threshold abort, sub-threshold no-abort, per-model counter independence.
Suite state after series: 622 passed, 1 known pre-existing env failure
(`test_setup_credentials.py::test_orca_rejects_missing_key`).
