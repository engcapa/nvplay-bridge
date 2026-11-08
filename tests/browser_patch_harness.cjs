// Run with: Python pipes browser_upstream.PATCH_JS into node on stdin.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const patch = fs.readFileSync(0, 'utf8');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const encoder = new TextEncoder();
let controllers = [];
let bodies = [];
let aborted = [];

global.window = { fetch: async (input, init) => {
  const request = new Request(input, init);
  bodies.push(await request.text());
  let controller;
  const body = new ReadableStream({ start(value) { controller = value; } });
  controllers.push(controller);
  request.signal.addEventListener('abort', () => {
    aborted.push(bodies.length);
    try { controller.error(new Error('aborted')); } catch {}
  });
  return new Response(body, { headers: { 'content-type': 'text/event-stream' } });
}};

async function waitFor(predicate) {
  for (let i = 0; i < 100 && !predicate(); i++) await delay(5);
  assert.ok(predicate(), 'expected browser state transition');
}

(async () => {
  assert.equal(vm.runInThisContext(patch), 'installed');
  assert.equal(vm.runInThisContext(patch), 'already');
  window.__bridgeArm('old', '{"custom":true}', 4096);
  const oldFetch = window.fetch('https://test/v2/predict/models/test', { method: 'POST', body: 'x' });
  await oldFetch;
  const old = window.__bridge;
  const bytes = encoder.encode('data: {"text":"中文🙂"}\n\n');
  const split = bytes.indexOf(0xe4) + 1;
  controllers[0].enqueue(bytes.slice(0, split));
  await delay(10);
  const first = window.__bridgeDrain('old', 65536).data;
  controllers[0].enqueue(bytes.slice(split));
  await waitFor(() => old.pending > 0);
  const second = window.__bridgeDrain('old', 65536).data;
  assert.equal(first + second, new TextDecoder().decode(bytes));
  assert.equal(bodies[0], '{"custom":true}');
  assert.equal(old.body, null);
  assert.equal(await window.__bridgeAbort('old'), true);
  assert.ok(aborted.length);

  window.__bridgeArm('new', '{"new":true}', 4096);
  await window.fetch(new Request('https://test/v2/predict/models/test', { method: 'POST', body: 'x' }));
  controllers[1].enqueue(encoder.encode('new data'));
  controllers[1].close();
  await waitFor(() => window.__bridge.done);
  assert.equal(window.__bridgeDrain('new', 65536).data, 'new data');
  assert.equal(window.__bridge.error, null);
  assert.equal(old.pending, 0);
  assert.throws(() => window.__bridgeDrain('old', 65536), /mismatch/);

  window.__bridgeArm('overflow', '{}', 8);
  await window.fetch('https://test/v2/predict/models/test', { method: 'POST', body: 'x' });
  controllers[2].enqueue(encoder.encode('too much data for the buffer'));
  await waitFor(() => window.__bridge.done);
  assert.match(window.__bridge.error, /buffer exceeded/);
  assert.equal(window.__bridge.pending, 0);
  console.log('PASS browser patch: Unicode fragments, incremental drain, request isolation, abort and buffer limit');
})().catch(error => { console.error(error); process.exitCode = 1; });
