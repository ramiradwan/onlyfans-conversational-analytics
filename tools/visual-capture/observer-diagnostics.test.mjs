import assert from 'node:assert/strict';
import { test } from 'node:test';
import { OBSERVER_REGIONS, collectObserverDiagnostics, validateObserverDiagnostics } from './observer-diagnostics.mjs';
import { observeRegions, readWatcher } from './shift-watcher.mjs';
import { failureSummary } from './failure-summary.mjs';

const missing = { code: 'required_region_missing_at_first_surface', phase: 'first_surface', region: 'popup-status' };
const read = { code: 'observer_read_failed', phase: 'observer_read', region: null };
const config = { view: 'popup' };
const canary = 'PRIVATE-cookie-account=https://private.example.test/';

test('the actual first-surface assertion produces only registered diagnostic regions', t => {
  let nextFrame;
  const main = { parentElement: null, matches: () => false, getClientRects: () => [{}] };
  const globals = {
    window: globalThis, requestAnimationFrame: callback => { nextFrame = callback; return 1; }, cancelAnimationFrame: () => {},
    document: { querySelectorAll: selector => selector === '[data-reserved-region]' ? [] : [main] },
    getComputedStyle: () => ({ position: 'static', width: '10px', height: '10px', visibility: 'visible', opacity: '1' }),
    PerformanceObserver: class { static supportedEntryTypes = ['paint', 'layout-shift']; observe() {} },
  };
  for (const [name, value] of Object.entries(globals)) {
    const original = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, { value, writable: true, configurable: true });
    t.after(() => original ? Object.defineProperty(globalThis, name, original) : delete globalThis[name]);
  }
  t.after(() => { delete globalThis.__regionWatcher; delete globalThis.__regionWatchControl; });
  observeRegions({ requiredRegions: OBSERVER_REGIONS.popup });
  nextFrame();
  const failures = window.__regionWatcher.errors;
  assert.equal(failures.length, 3);
  const diagnostics = collectObserverDiagnostics('popup', failures);
  assert.deepEqual(diagnostics.map(row => row.region), OBSERVER_REGIONS.popup);
  assert(diagnostics.every(row => row.code === missing.code && row.phase === missing.phase));
  assert.equal(failureSummary([], failures).other, 3);
});

test('observer read rejection is distinct from initial-region absence without error text', async () => {
  const page = { evaluate: async () => { throw new Error(canary); } };
  let readFailed = false;
  const watcher = await readWatcher(page).catch(error => { readFailed = true; return { failures: [error.message] }; });
  assert.deepEqual(collectObserverDiagnostics('popup', watcher.failures, readFailed), [read]);
  assert.deepEqual(collectObserverDiagnostics('popup', ['popup-status: missing on first surface frame']), [missing]);
  assert.equal(failureSummary([], watcher.failures).other, 1);
  assert(!JSON.stringify(collectObserverDiagnostics('popup', watcher.failures, readFailed)).includes(canary));
});

test('unknown values stay classified failures without exposing arbitrary prefixes or objects', () => {
  const failures = [canary, `${canary}: missing on first surface frame`, { private: canary }, null];
  assert.deepEqual(collectObserverDiagnostics('popup', failures), [
    { code: 'observer_unclassified', phase: 'observer_inspection', region: null },
  ]);
  assert.equal(failureSummary([], failures).other, 4);
  assert.deepEqual(collectObserverDiagnostics('popup', []), []);
  assert.throws(() => collectObserverDiagnostics(canary, failures), /invalid_observer_diagnostics/);
});

test('the retained boundary rejects unknown, duplicate, oversized and cross-view diagnostics', () => {
  const mutations = [
    null, {}, [null], [{ ...missing, code: canary }], [{ ...missing, phase: 'observer_read' }],
    [{ ...missing, region: 'dashboard-overview' }], [{ ...missing, private: canary }],
    [{ ...read, region: canary }], [missing, missing], Array(6).fill(missing),
    [{ ...missing, code: { toString: () => missing.code, private: canary } }],
  ];
  for (const value of mutations) assert.throws(() => validateObserverDiagnostics(value, config, 'failed'), /invalid_observer_diagnostics/);
  assert.throws(() => validateObserverDiagnostics([missing], { view: 'home' }, 'failed'));
  assert.throws(() => validateObserverDiagnostics([missing], config, 'passed'));
  assert.deepEqual(validateObserverDiagnostics([missing, read], config, 'failed'), [missing, read]);
});
