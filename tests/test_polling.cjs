'use strict';
// Exercise the actual request/poll functions with a deliberately hung endpoint.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');
const fleet = fs.readFileSync('static/fleet.js', 'utf8');
const section = (text, start, end) => {
  const from = text.indexOf(start);
  assert.notEqual(from, -1, `Missing source anchor: ${start}`);
  const to = text.indexOf(end, from + start.length);
  assert.notEqual(to, -1, `Missing source anchor after ${start}: ${end}`);
  return text.slice(from, to);
};
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, no) => { resolve = ok; reject = no; });
  return { promise, resolve, reject };
}
async function requests() {
  const context = vm.createContext({
    state: { instanceId: 'a', instanceEpoch: 1, instanceReady: true, configInstance: 'a' },
    fleetEnabled: true, csrf: 'test', AbortController, DOMException,
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
  context.state.instanceReady = false;
  await assert.rejects(context.api('/api/config', options), /所选实例尚未就绪/);
  assert.equal(sends, 1, 'unready instance must not receive a write');
  context.state.instanceReady = true;
  const late = deferred();
  context.fetch = () => late.promise;
  const oldRead = context.api('/api/config');
  context.state.instanceEpoch += 1;
  late.resolve({ ok: true, status: 200, text: async () => '{}' });
  await assert.rejects(oldRead, error => error.name === 'AbortError');
  assert.equal(context.state.instanceWrites || 0, 0);
}
async function polling() {
  const pending = deferred(), calls = [], applied = [];
  const context = vm.createContext({ AbortController, Date, Promise, Map, queueMicrotask, performance,
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
  const context = vm.createContext({ JSON, performance, fleetEnabled: true,
    state: { media: [], overview: { media: {}, recent_media: [] } }, pages: { media: {}, tasks: {} },
    $: selector => { if (!elements.has(selector)) elements.set(selector, {}); return elements.get(selector); },
    renderMedia() {}, renderTasks() {}, renderClash() {}, renderDownloadControl() {}, renderStatus() {}, renderPagination() {} });
  vm.runInContext(section(source, 'function applyRefreshData(', '\nasync function refresh({'), context);
  context.applyRefreshData({ status: { database: 'mysql', workers: [], concurrency: { local_health: { progress_cache: { enabled: true } } } } });
  assert.ok(elements.get('#database-backend').textContent.includes('MySQL'));
  assert.ok(elements.get('#database-backend').textContent.includes('缓存已启用'));
  vm.runInContext(section(source, 'function liveTransferSpeed(', '\nfunction progressFreshness(') +
    section(fleet, 'function instanceMode(', '\nfunction instanceState('), context);
  context.Date = Date;
  context.escapeHTML = String;
  context.progressLabel = () => 'progress';
  context.bytes = String;
  const task = { status: 'running', progress: { stage: 'downloading', speed: 123, _reported_at: Date.now()/1000-30 } };
  assert.equal(context.liveTransferSpeed(task), null, 'expired progress must not contribute live speed');
  task.progress._reported_at = Date.now()/1000;
  assert.equal(context.liveTransferSpeed(task), 123, 'fresh transfer speed must remain visible');
  const html = context.instanceLive({ online: false, speed: 123 });
  assert.ok(!html.includes('123/s'), 'offline fleet cards must hide their last reported speed');
}
async function fleetUpdates() {
  const elements = new Map();
  const $ = selector => {
    if (!elements.has(selector)) elements.set(selector, { innerHTML: '', value: '', textContent: '' });
    return elements.get(selector);
  };
  let cards = 0, calls = 0, blocked;
  const item = { id: 'a', role: 'worker', online: true, name: 'fixture', worker_count: 2 };
  const context = vm.createContext({ Date, Promise, Set, performance, state: { instanceId: 'a', config: null }, fleetEnabled: true,
    location: { hash: '#instances' }, document: { activeElement: null }, $, $$: () => [],
    lastStartedJobId: null, settingsDirty: false,
    api: async () => { calls++; if (blocked) await blocked.promise; return { items: [item], instance_id: 'a', global_limit: 4 }; },
    applyStartup() {}, instanceMode: item => item.throughput_mode || 'manual',
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
async function initialization() {
  const persisted = 'a'.repeat(32), linked = 'b'.repeat(32);
  for (const initialJobId of [null, 'invalid-link', linked]) {
    const reads = [];
    const context = vm.createContext({ initialJobId, selectedJobId: '', state: {}, fleetEnabled: true,
      location: { hash: '#overview' }, $: () => ({}), setInterval() {},
      renderMedia() {}, renderTasks() {}, setView() {}, clearJobLink() {},
      initializeFleet: async () => {},
      loadSelectedInstance: async () => { context.selectedJobId = persisted; },
      refreshSelectedJob: async () => { reads.push(context.selectedJobId); },
      loadDownloadJobs: async () => {}, pollStartup: async () => {}, refresh: async () => {},
      toast: message => { throw new Error(message); } });
    vm.runInContext(section(source, 'async function init() {', '\nrenderDownloadMethod();'), context);
    await context.init();
    assert.deepEqual(reads, [initialJobId === linked ? linked : persisted],
      'initialization must preserve the restored job unless a valid deep link overrides it');
  }
}
(async () => { await requests(); applying(); await polling(); await fleetUpdates(); await initialization(); console.log('PASS: request timeout/cancel, independent polling, stale response fencing, status rendering, stable fleet DOM, initial job restoration'); })()
  .catch(error => { console.error(error); process.exitCode = 1; });
