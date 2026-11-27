'use strict';
const http = require('node:http');

const methods = new Map([
  ['/mcp', new Set(['GET', 'POST', 'DELETE'])],
  ['/.well-known/oauth-protected-resource', new Set(['GET'])],
  ['/.well-known/oauth-protected-resource/mcp', new Set(['GET'])],
]);
const requestHeaders = new Set(['accept', 'authorization', 'content-type', 'last-event-id', 'origin']);
const responseHeaders = new Set(['www-authenticate', 'content-type', 'retry-after', 'allow']);

function headersFor(message, allowed) {
  const hop = new Set((message.headers.connection || '').toLowerCase().split(',').map(s => s.trim()));
  return Object.fromEntries(Object.entries(message.headers).filter(([key]) =>
    !hop.has(key) && (allowed.has(key) || key.startsWith('mcp-'))));
}

function gateway(host, active, connect = http.request) {
  return http.createServer((req, res) => {
    res.setHeader('cache-control', 'no-store');
    if (!active || !host || req.headers.host !== host || !methods.get(req.url)?.has(req.method)) {
      res.writeHead(404).end();
      return;
    }
    // Use a DNS name so App Service can select the Hybrid Connection. No input selects the target.
    const upstream = connect({
      hostname: 'knownfaces-mcp.invalid', port: 8094, method: req.method, path: req.url,
      headers: { ...headersFor(req, requestHeaders), host }, agent: false,
    }, reply => {
      res.writeHead(reply.statusCode, headersFor(reply, responseHeaders));
      res.flushHeaders();
      reply.on('error', () => res.destroy());
      reply.pipe(res);
    });
    upstream.on('error', () => {
      if (res.headersSent) res.destroy();
      else res.writeHead(503).end();
    });
    // Bound a silent connection. Active SSE traffic resets this socket inactivity timer.
    upstream.setTimeout(60000, () => upstream.destroy());
    req.on('error', () => upstream.destroy());
    res.on('close', () => upstream.destroy());
    req.pipe(upstream);
  });
}

module.exports = { gateway };
if (require.main === module) {
  gateway(process.env.WEBSITE_HOSTNAME, process.env.KF_EDGE_STATE === 'synthetic')
    .listen(Number(process.env.PORT || 8080), '0.0.0.0');
}
