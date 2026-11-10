// Explicit opt-in: npm ci --ignore-scripts --prefix tools/cc_node_runtime
// node --test tests/test_cc_node_timeout.cjs
'use strict';

const assert = require('node:assert/strict');
const {execFile} = require('node:child_process');
const http = require('node:http');
const path = require('node:path');
const {promisify} = require('node:util');
const {test} = require('node:test');
const run = promisify(execFile);
const runtime = path.resolve(__dirname, '../tools/cc_node_runtime');
const timers = new Set();

function fixture() {
  const server = http.createServer((req, res) => {
    if (req.url === '/body') {
      res.writeHead(200, {'Content-Type': 'text/plain'});
      res.write('first');
    }
    const timer = setTimeout(() => {
      timers.delete(timer);
      if (!res.destroyed) res.end('complete');
    }, 1400);
    timers.add(timer);
    res.on('close', () => { clearTimeout(timer); timers.delete(timer); });
  });
  return new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server)));
}

const program = `
const {Agent, setGlobalDispatcher} = require(${JSON.stringify(runtime + '/node_modules/undici')});
setGlobalDispatcher(new Agent({headersTimeout: 100, bodyTimeout: 100}));
if (process.env.PROBE_ADAPTER === '1') require(${JSON.stringify(runtime + '/timeout.cjs')});
(async () => {
  try {
    const response = await fetch(process.env.PROBE_URL, {
      signal: process.env.PROBE_ABORT === '1' ? AbortSignal.timeout(80) : undefined
    });
    console.log(JSON.stringify({ok: true, text: await response.text()}));
  } catch (error) {
    console.log(JSON.stringify({ok: false, name: error.name, code: error.cause?.code}));
  }
})();
`;

test('the scoped adapter permits slow headers and bodies while preserving cancellation and other origins', async t => {
  const configured = await fixture();
  const other = await fixture();
  t.after(() => {
    configured.closeAllConnections(); other.closeAllConnections();
    configured.close(); other.close();
    for (const timer of timers) clearTimeout(timer);
  });
  const origin = `http://127.0.0.1:${configured.address().port}`;
  async function probe(route, adapter, options = {}) {
    const {stdout} = await run(process.execPath, ['-e', program], {
      env: {...process.env, API_TIMEOUT_MS: '5000', ANTHROPIC_BASE_URL: origin,
        PROBE_URL: origin + route, PROBE_ADAPTER: adapter ? '1' : '0', ...options}, timeout: 10000
    });
    return JSON.parse(stdout.trim());
  }
  for (const [route, errorCode, text] of [
    ['/headers', 'UND_ERR_HEADERS_TIMEOUT', 'complete'],
    ['/body', 'UND_ERR_BODY_TIMEOUT', 'firstcomplete']
  ]) {
    const before = await probe(route, false);
    assert.equal(before.ok, false);
    assert.equal(before.code, errorCode);
    assert.deepEqual(await probe(route, true), {ok: true, text});
  }
  const cancelled = await probe('/headers', true, {PROBE_ABORT: '1'});
  assert.equal(cancelled.ok, false);
  assert.equal(cancelled.name, 'TimeoutError');
  const unrelated = await probe('/headers', true, {
    PROBE_URL: `http://127.0.0.1:${other.address().port}/headers`
  });
  assert.equal(unrelated.ok, false);
  assert.equal(unrelated.code, 'UND_ERR_HEADERS_TIMEOUT');
});
