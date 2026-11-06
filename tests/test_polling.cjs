'use strict';
// Exercise the actual request/poll functions with a deliberately hung endpoint.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');
const fleet = fs.readFileSync('static/fleet.js', 'utf8');
const section = (text, start, end) => text.slice(text.indexOf(start), text.indexOf(end, text.indexOf(start)));
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, no) => { resolve = ok; reject = no; });
  return { promise, resolve, reject };
}
async function requests() {
  const context = vm.createContext({ state: { instanceId: 'a' }, csrf: 'test', AbortController, DOMException,
    FormData, setTimeout, clearTimeout, location: { assign() {} }, fetch: async (path, options) =>
      new Promise((resolve, reject) => options.signal.addEventListener('abort', () => reject(options.signal.reason))) });
  vm.runInContext(section(source, 'async function api(', '\nfunction setView('), context);
  await assert.rejects(context.api('/read', { timeoutMs: 5 }), error => error.name === 'TimeoutError' && error.message.includes('上次成功'));
  await assert.rejects(context.api('/write', { method: 'POST', body: { value: 1 }, timeoutMs: 5 }), error => error.message.includes('尚未确认'));
  const cancel = new AbortController();
  const pending = context.api('/cancel', { signal: cancel.signal });
  cancel.abort();
  await assert.rejects(pending, error => error.name === 'AbortError');
  let sends = 0;
  const options = { method: 'POST', body: { value: 1 } };
  context.fetch = async () => { sends++; return { ok: true, status: 200, text: async () => '{}' }; };
  await context.api('/write', options);
  assert.equal(sends, 1);
  assert.equal(typeof options.body, 'object');
}
async function polling() {
  const pending = deferred(), calls = [], applied = [];
  const context = vm.createContext({ AbortController, Date, Promise, Map, queueMicrotask,
    state: { instanceId: 'a', selectedTask: null }, fleetEnabled: true,
    location: { hash: '#overview' }, $: () => ({ value: '', open: false }),
    listQuery: () => '', refreshExecution: async () => {}, updateTaskDetail: async () => {},
    applyRefreshData: data => applied.push(data),
    api: (path, options) => {
      calls.push([path, context.state.instanceId]);
      if (path === '/api/status' && context.state.instanceId === 'a') {
        // Simulate a transport that ignores cancellation; the ticket must still fence its result.
        return pending.promise;
      }
      return Promise.resolve({ instance: context.state.instanceId });
    } });
  vm.runInContext('const refreshedAt={}; const refreshInFlight=new Map(); const refreshFailures={}; let refreshFailureInstance;\n' +
    section(source, 'async function refresh({', "\ndocument.addEventListener('viewchange'"), context);
  const first = context.refresh();
  await tick();
  assert.ok(applied.some(data => data.overview), 'fast overview must apply while status is hung');
  await context.refresh({ poll: true });
  assert.equal(calls.filter(([path]) => path === '/api/status').length, 1, 'no duplicate same endpoint');
  context.state.instanceId = 'b';
  await context.refresh();
  pending.resolve({ instance: 'a' });
  await first;
  assert.ok(applied.some(data => data.status?.instance === 'b'));
  assert.ok(!applied.some(data => data.status?.instance === 'a'), 'late old instance response must be ignored');
}
function applying() {
  const elements = new Map();
  const context = vm.createContext({ JSON,
    state: { media: [], overview: { media: {}, recent_media: [] } }, pages: { media: {}, tasks: {} },
    $: selector => { if (!elements.has(selector)) elements.set(selector, {}); return elements.get(selector); },
    renderMedia() {}, renderTasks() {}, renderClash() {}, renderDownloadControl() {}, renderStatus() {}, renderPagination() {} });
  vm.runInContext(section(source, 'function applyRefreshData(', '\nasync function refresh({'), context);
  context.applyRefreshData({ status: { database: 'mysql', workers: [], concurrency: { local_health: { progress_cache: { enabled: true } } } } });
  assert.ok(elements.get('#database-backend').textContent.includes('MySQL'));
  assert.ok(elements.get('#database-backend').textContent.includes('缓存已启用'));
  vm.runInContext(section(source, 'function liveTransferSpeed(', '\nfunction progressFreshness(') +
    section(fleet, 'function instanceCurrentTasks(', '\nfunction instanceLive('), context);
  context.Date = Date;
  context.escapeHTML = String;
  context.progressLabel = () => 'progress';
  context.bytes = String;
  const html = context.instanceCurrentTasks({ current_tasks: [{ progress: { stage: 'downloading', speed: 123, _reported_at: Date.now()/1000-30 } }] });
  assert.ok(!html.includes('123/s'), 'stale speed must also disappear from fleet cards');
}
async function fleetUpdates() {
  const elements = new Map();
  const $ = selector => {
    if (!elements.has(selector)) elements.set(selector, { innerHTML: '', value: '', textContent: '' });
    return elements.get(selector);
  };
  let cards = 0, calls = 0, blocked;
  const item = { id: 'a', role: 'worker', online: true, name: 'fixture', worker_count: 2 };
  const context = vm.createContext({ Date, Promise, Set, state: { instanceId: 'a', config: null }, fleetEnabled: true,
    location: { hash: '#instances' }, document: { activeElement: null }, $, $$: () => [],
    lastStartedJobId: null, settingsDirty: false,
    api: async () => { calls++; if (blocked) await blocked.promise; return { items: [item], instance_id: 'a', global_limit: 4 }; },
    renderFleetJob() {}, renderDownloadControl() {}, renderInstanceScope() {}, renderConcurrency() {}, syncInstanceControls() {},
    instanceState: () => 'online', instanceLive: () => 'progress', bytes: String, escapeHTML: String,
    instanceCard: () => { cards++; return 'card'; }, emptyState: () => '' });
  vm.runInContext(section(fleet, 'let fleetLoading', '\nasync function initializeFleet'), context);
  await context.loadFleet();
  item.running = 2;
  await context.loadFleet();
  assert.equal(cards, 1, 'progress updates must preserve the configuration DOM');
  context.state.fleetFormDirty = true;
  item.name = 'renamed';
  await context.loadFleet();
  assert.equal(cards, 1, 'in-progress form editing must be preserved');
  context.state.fleetFormDirty = false;
  await context.loadFleet();
  assert.equal(cards, 2);
  context.location.hash = '#overview';
  await context.loadFleet();
  assert.equal(cards, 2, 'hidden instance page must not render cards');
  blocked = deferred();
  const before = calls;
  const first = context.loadFleet();
  await context.loadFleet();
  assert.equal(calls, before+1, 'no overlapping fleet requests');
  blocked.resolve();
  await first;
}
(async () => { await requests(); applying(); await polling(); await fleetUpdates(); console.log('PASS: request timeout/cancel, independent polling, stale response fencing, status rendering, stable fleet DOM'); })()
  .catch(error => { console.error(error); process.exitCode = 1; });
