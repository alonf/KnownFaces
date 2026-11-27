// Runs inside a network-isolated Linux container. Start the exact compiled App Service command.
const { spawn } = require('node:child_process');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const resources = JSON.parse(fs.readFileSync('/fixture/main.json')).resources;
const configs = resources.filter(r => r.type === 'Microsoft.Web/sites/config');
assert.equal(configs.length, 1);
const settings = configs[0].properties;
assert.equal(settings.scmIpSecurityRestrictionsDefaultAction, 'Deny');
assert.equal(settings.httpLoggingEnabled, false);
assert.equal(settings.requestTracingEnabled, false);
const command = settings.appCommandLine;
async function main() {
  const base = { ...process.env, PORT:'8080', WEBSITE_HOSTNAME:'localhost:8080', KF_EDGE_STATE:'blocked' };
  const tampered = spawn('sh',['-c',command],{env:{...base,KF_GATEWAY_SHA:'0'.repeat(64)}});
  assert.notEqual(await new Promise(resolve => tampered.once('exit',resolve)),0);
  const child = spawn('sh',['-c',command],{env:base});
  try {
    for (let n=0; ; n++) {
      try { await fetch('http://localhost:8080/mcp'); break; }
      catch (e) { if (n === 40) throw e; await new Promise(r => setTimeout(r,100)); }
    }
    for (const path of ['/mcp','/.well-known/oauth-protected-resource/mcp','/v1/snapshots'])
      assert.equal((await fetch('http://localhost:8080'+path)).status,404);
    console.log('Verified blocked package startup and digest enforcement');
  } finally { child.kill(); }
}
main().catch(() => { console.error('Gateway package check failed'); process.exitCode=1; });
