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
    return env.ASSETS.fetch(new Request(packagedUrl, request));
  }
};

export default worker;
