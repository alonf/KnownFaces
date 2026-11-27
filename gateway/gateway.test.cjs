const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { once } = require('node:events');
const { gateway } = require('./gateway.cjs');
const host = 'pilot.example.invalid';
async function listen(t, server) {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => { server.closeAllConnections(); server.close(); });
  return server.address().port;
}
function request(port, path, method = 'GET', headers = {}, body) {
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname: '127.0.0.1', port, path, method, headers: {host, ...headers} }, resolve);
    req.on('error', reject);
    req.end(body);
  });
}
test('blocked or disallowed requests never connect', async t => {
  for (const active of [false, true]) {
    const port = await listen(t, gateway(host, active, () => assert.fail('unexpected upstream')));
    const denied = [['GET','/'],['POST','/v1/snapshots'],['GET','/v1/samples/x/crop'],['GET','/login'],
      ['GET','/v1/snapshots/x/image'],['GET','/mcp/'],['POST','/%6dcp'],['GET','/mcp?token=test'],
      ['POST','/.well-known/oauth-protected-resource/mcp'],['PUT','/mcp'],['GET','http://other.invalid/mcp']];
    if (!active) denied.push(['POST','/mcp'], ['GET','/.well-known/oauth-protected-resource']);
    for (const [method,path] of denied) assert.equal((await request(port,path,method)).statusCode,404);
    assert.equal((await request(port,'/mcp','POST',{host:'unapproved.invalid'})).statusCode,404);
  }
});
test('canonical headers, challenge, methods, incremental SSE and disconnect', {timeout:10000}, async t => {
  let release, disconnected;
  const observedClose = new Promise(resolve => { disconnected = resolve; });
  const backend = http.createServer((req,res) => {
    assert.equal(req.headers.host,host);
    assert.equal(req.headers['x-forwarded-host'],undefined);
    assert.equal(req.headers.cookie,undefined);
    if (req.method === 'POST') {
      assert.equal(req.headers.authorization,'Bearer synthetic');
      assert.equal(req.headers['mcp-protocol-version'],'2026-07-28');
      assert.equal(req.headers['mcp-session-id'],'fixture');
      assert.equal(req.headers['mcp-method'],'tools/call');
      req.setEncoding('utf8');
      let body = '';
      req.on('data', bytes => { body += bytes; });
      req.on('end', () => {
        assert.equal(body,'fixture body');
        res.writeHead(401,{'www-authenticate':'Bearer realm="fixture"','set-cookie':'private=x'}).end();
      });
      return;
    }
    if (req.url === '/mcp' && req.method === 'GET') {
      res.writeHead(200,{'content-type':'text/event-stream','mcp-session-id':'fixture'});
      res.write('data: first\n\n');
      release = () => res.write('data: second\n\n');
      res.on('close',disconnected);
    } else res.writeHead(200).end();
  });
  const port = await listen(t,backend);
  const edge = await listen(t,gateway(host,true,(options,callback) => {
    assert.equal(options.hostname,'knownfaces-mcp.invalid');
    assert.equal(options.port,8094);
    return http.request({...options, hostname:'127.0.0.1',port},callback);
  }));
  const denied = await request(edge,'/mcp','POST',{authorization:'Bearer synthetic',
    'mcp-protocol-version':'2026-07-28','mcp-session-id':'fixture','mcp-method':'tools/call',
    'x-forwarded-host':'wrong',cookie:'dummy'},'fixture body');
  assert.equal(denied.statusCode,401);
  assert.equal(denied.headers['www-authenticate'],'Bearer realm="fixture"');
  assert.equal(denied.headers['set-cookie'],undefined);
  for (const path of ['/.well-known/oauth-protected-resource','/.well-known/oauth-protected-resource/mcp'])
    assert.equal((await request(edge,path)).statusCode,200);
  assert.equal((await request(edge,'/mcp','DELETE')).statusCode,200);
  const stream = await request(edge,'/mcp');
  assert.equal(stream.headers['mcp-session-id'],'fixture');
  assert.equal(String((await once(stream,'data'))[0]),'data: first\n\n');
  const next = once(stream,'data');
  release();
  assert.equal(String((await next)[0]),'data: second\n\n');
  stream.destroy();
  await observedClose;
  backend.close();
  assert.equal((await request(edge,'/mcp')).statusCode,503);
});
