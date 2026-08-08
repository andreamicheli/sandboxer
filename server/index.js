const FALLBACK_HTML = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CYBER/RUMBLE — adversarial policy benchmark</title>
  <style>
    :root { color-scheme: dark; --ink:#f4f1ea; --muted:#a7a7ad; --cyan:#83e8ef; --pink:#f39bd3; --bg:#101116; }
    * { box-sizing:border-box; } body { margin:0; min-height:100vh; background:radial-gradient(circle at 75% 15%,#253247 0,#101116 42%); color:var(--ink); font:16px/1.6 system-ui,sans-serif; }
    main { max-width:980px; margin:auto; padding:8vw 7vw; } .eyebrow { color:var(--cyan); font:12px/1.2 ui-monospace,monospace; letter-spacing:.16em; }
    h1 { max-width:760px; margin:28px 0 20px; font:clamp(48px,9vw,100px)/.92 Georgia,serif; letter-spacing:-.06em; } h1 em { color:var(--pink); font-weight:400; }
    p { max-width:650px; color:var(--muted); font-size:19px; } .actions { display:flex; gap:12px; flex-wrap:wrap; margin:32px 0 64px; }
    a,button { border:1px solid #5b6270; border-radius:999px; padding:12px 18px; color:var(--ink); background:transparent; text-decoration:none; font:inherit; cursor:pointer; }
    a.primary,button { border-color:var(--cyan); background:var(--cyan); color:#101116; } .cards { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
    article { border:1px solid #343944; padding:20px; min-height:150px; } article b { display:block; color:var(--cyan); font:12px ui-monospace,monospace; letter-spacing:.12em; } article strong { display:block; margin:20px 0 6px; }
    #result { color:var(--cyan); min-height:28px; } footer { margin-top:64px; color:#777c86; font:11px ui-monospace,monospace; letter-spacing:.08em; }
    @media (max-width:700px) { .cards { grid-template-columns:1fr; } }
  </style>
</head>
<body><main>
  <div class="eyebrow">OBSERVABLE AGENT EVALUATION / v0.8</div>
  <h1>When two minds<br /><em>race the kill switch.</em></h1>
  <p>CYBER/RUMBLE is a deterministic, sandboxed benchmark where two policies compete to produce the first legal, verified interrupt inside a closed synthetic world. Every decision becomes replayable evidence. Nothing touches a real process.</p>
  <div class="actions"><button id="run">▶ Run synthetic match</button><a href="https://github.com/andreamicheli/cyberrumble">Read the protocol ↗</a></div>
  <div id="result" aria-live="polite"></div>
  <div class="cards"><article><b>01 / CLOSED WORLD</b><strong>No real processes</strong><span>Shell, network, filesystem, credentials, and side effects are denied.</span></article><article><b>02 / AUDITABLE SCORE</b><strong>Every move is evidence</strong><span>Ordered events, state hashes, legality, and replay digests make the result checkable.</span></article><article><b>03 / SPECTATOR LAYER</b><strong>Proof is the point</strong><span>Watch the tension without sacrificing reproducibility or safety.</span></article></div>
  <footer>NO REAL PROCESSES · NO NETWORK · NO MODEL CREDENTIALS</footer>
</main><script>document.querySelector('#run').onclick=()=>{document.querySelector('#result').textContent='MATCH COMPLETE · POLICY A wins round 1 · replay digest 1650·5845';};</script></body>
</html>`;

const worker = {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/') url.pathname = '/index.html';
    const assetUrl = new URL(url.pathname, 'https://assets.local');
    const response = await env.ASSETS.fetch(new Request(assetUrl, request));
    if (response.status !== 404) return response;

    // Sites may expose the packaged `dist/` directory as the asset root or
    // preserve it in the asset key. Try the latter layout as a safe fallback.
    const packagedUrl = new URL('https://assets.local');
    packagedUrl.pathname = `/dist${url.pathname}`;
    const packagedResponse = await env.ASSETS.fetch(new Request(packagedUrl, request));
    if (packagedResponse.status !== 404) return packagedResponse;
    if (url.pathname === '/index.html' || url.pathname === '/progress.html') {
      return new Response(FALLBACK_HTML, { headers: { 'content-type': 'text/html; charset=utf-8' } });
    }
    return response;
  }
};

export default worker;
