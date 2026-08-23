// ============================================================================
// render-video.js - Remotion video rendering script
// ============================================================================
// DIAGNOSED AND FIXED ISSUES:
// 1. ES Module __dirname issue: Original used `import` (ESM) but `__dirname` is
//    CommonJS-only. Fixed by using `require()` syntax.
// 2. Composition ID mismatch: render-video.js looked for 'SandboxerSeries', which
//    matches the id="SandboxerSeries" in index.tsx line 674-675. COMPOSITION ID MATCHES ✓
// 3. Missing bundle step: getCompositions() and renderMedia() require a webpack
//    bundle or serve URL. Original script was missing the @remotion/bundler step.
//    Fixed by importing and calling bundle() first.
// 4. API mismatch - videoCodec vs codec: renderMedia expects `codec` parameter,
//    not `videoCodec`. Fixed to use `codec: 'h264'`.
// 5. Missing licenseKey: Remotion v5 requires `licenseKey: 'free-license'`.
// 6. Invalid parameters removed: `McCartney`, `forceRewrap`, `rootDir` are not
//    valid renderMedia parameters in v4.0.509.
// 7. Path resolution: `../../pilot/...` was incorrect; fixed to `../pilot/...`.
// 8. Server error EADDRINUSE: Used bundle() which handles port allocation internally.
// 9. Disk space in /tmp (490MB free): Set TMPDIR=/home/ubuntu for temp files.
// ============================================================================

process.env.TMPDIR = '/home/ubuntu';

const { renderMedia, getCompositions } = require('@remotion/renderer');
const { bundle } = require('@remotion/bundler');
const path = require('path');
const fs = require('fs');

const entryPoint = path.resolve(__dirname, 'src/index.tsx');
const outputFile = path.resolve(
  __dirname,
  '../pilot/artifacts/runs/match-full-pipeline-v7/video-only.mp4'
);
const propsPath = path.resolve(
  __dirname,
  '../pilot/artifacts/runs/match-full-pipeline-v7/remotion-props.json'
);

const props = JSON.parse(fs.readFileSync(propsPath, 'utf-8'));

async function main() {
  console.log('[render-script] entryPoint:', entryPoint);
  console.log('[render-script] outputFile:', outputFile);

  // Step 1: bundle the Remotion project with webpack
  console.log('[render-script] bundling...');
  const bundled = await bundle({ entryPoint });
  console.log('[render-script] bundle URL:', bundled);

  // Step 2: get compositions from the bundle
  console.log('[render-script] getting compositions...');
  const compositions = await getCompositions({
    serveUrl: bundled,
    inputProps: props,
  });
  console.log('[render-script] compositions:', compositions.map(c => c.id));

  // Find 'SandboxerSeries' (the composition.id in index.tsx line 675)
  // Composition exports: SeriesVideo (component), id="SandboxerSeries"
  const composition = compositions.find(c => c.id === 'SandboxerSeries')
    || compositions[0];

  if (!composition) {
    console.error('[render-script] No composition found');
    process.exit(1);
  }

  console.log('[render-script] rendering composition:', composition.id);
  console.log('[render-script] fps:', composition.fps, 'durationInFrames:', composition.durationInFrames);

  await renderMedia({
    serveUrl: bundled,
    composition,
    outputLocation: outputFile,
    inputProps: props,
    concurrency: 1,
    logLevel: 'info',
    codec: 'h264',
    audioCodec: 'aac',
    pixelFormat: 'yuv420p',
    imageFormat: 'jpeg',
    licenseKey: 'free-license',
    chromiumOptions: {
      args: [
        '--disable-dev-shm-usage',
        '--no-sandbox',
        '--disable-gpu',
      ],
    },
  });

  console.log('[render-script] DONE - video written to', outputFile);
}

main().catch(err => {
  console.error('[render-script] ERROR:', err.message);
  console.error('[render-script] stack:', err.stack);
  process.exit(1);
});