// Loopback origins preserve secure-context browser APIs and the approved target list.
import http from 'node:http';
const routes = [[4200, 'next-to-you-ui', 80], [8100, 'next-to-you', 8100], [8025, 'mailhog', 8025]];
const hop = new Set(['connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade']);
for (const [port, host, upstreamPort] of routes) {
  http.createServer((req, res) => {
    const headers = Object.fromEntries(Object.entries(req.headers).filter(([name]) => !hop.has(name)));
    // Keep the browser-facing Host so redirects and cookies use the loopback origin.
    const upstream = http.request({ hostname: host, port: upstreamPort, method: req.method, path: req.url, headers }, response => {
      const responseHeaders = Object.fromEntries(Object.entries(response.headers).filter(([name]) => !hop.has(name)));
      res.writeHead(response.statusCode, responseHeaders);
      response.pipe(res);
    });
    upstream.setTimeout(30_000, () => upstream.destroy());
    upstream.on('error', () => { if (!res.headersSent) res.writeHead(502); res.end(); });
    req.on('error', () => upstream.destroy());
    req.pipe(upstream);
  }).listen(port, '127.0.0.1');
}
