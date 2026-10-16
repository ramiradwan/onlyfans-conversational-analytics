import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { installWatcher, readWatcher, auditScrolling, nativeBoundary, settleNativeWatcher } from './shift-watcher.mjs';
import { SURFACE_STATES, surfaceDocument, surfaceBundle } from '../../extension/qualification/surface-fixtures.mjs';
import { installSurfaceFixture } from '../../extension/qualification/surface-runtime-fixture.mjs';
import { driveWorkspaceDetails } from './workspace-transitions.mjs';
import { runCaptureJobs } from './capture-jobs.mjs';
import { assertSetupTaskGeometry } from './task-copy.mjs';
import { inspectSettingsFrames, settingsFixturePolicy, settingsFailureSummary, settingsUnassignedShiftSummary } from './settings-stability.mjs';
import { failureSummary } from './failure-summary.mjs';

import { DYNAMIC_VIEWS, dynamicCases, dynamicSequence } from './dynamic-matrix.mjs';
export { DYNAMIC_VIEWS } from './dynamic-matrix.mjs';
export const REQUIRED_REGIONS = {
  home: ['freshness-status', 'issue-band', 'dashboard-notice', 'dashboard-overview', 'dashboard-basis', 'dashboard-recent', 'dashboard-setup'],
  analytics: ['freshness-status', 'issue-band', 'analytics-content'],
  inbox: ['freshness-status', 'issue-band', 'inbox-notice', 'inbox-content'],
  settings: ['freshness-status', 'issue-band', 'settings-browser', 'settings-history', 'settings-activation', 'settings-vault'],
  graph: ['freshness-status', 'issue-band', 'graph-notice'],
  passkey: ['passkey-card', 'passkey-feedback'],
  'production-boot': ['passkey-card', 'passkey-feedback'],
  popup: ['popup-content', 'popup-status', 'surface-feedback'],
  setup: ['extension-stage', 'surface-feedback'],
  options: ['options-notice', 'options-capture', 'options-connection', 'options-data', 'surface-feedback'],
};
export const REQUIRED_STATES = {
  home: ['loading', 'snapshot:fresh', 'snapshot:syncing', 'snapshot:populated', 'read-model:resyncing', 'read-model:realtime', 'agent:config-mismatch', 'counts:null', 'coverage:blocked'],
  analytics: ['loading', 'loading:model', 'model:error', 'tone:Table', 'tone:Chart', 'dates:invalid'],
  inbox: ['loading', 'message:pending', 'message:error', 'message:empty:success', 'message:populated:error', 'message:populated:prepend', 'message:populated:long'],
  settings: ['loading', 'control:pending', 'control:9999', 'control:10000', 'control:authoritative', 'disconnect:cancel', 'archive:invalid', 'delete:error', 'redemption:success:readiness', 'redemption:network:readiness', 'export:success:response', 'pairing:expired', 'pairing:confirmed:result'],
  graph: ['loading', 'pending:current', 'current:pending', 'current:unavailable', 'current:degraded'],
  passkey: ['loading', 'Sign in with passkey:NotAllowedError:pending', 'Set up a passkey:NotAllowedError:pending'],
  popup: ['unknown:phase', 'unknown:pairing', 'unknown:commercial', 'preview-counts:null'],
  setup: ['unknown:phase', 'unknown:pairing', 'unknown:commercial'],
  options: ['unknown:phase', 'unknown:pairing', 'unknown:commercial'],
  'production-boot': ['delayed-renderer-and-fonts', 'request-error:Sign in with passkey', 'request-error:Create passkey', 'session-status-unavailable'],
};
const TRANSITION_COUNTS = { home: { 390: 75, 1440: 73 }, analytics: 18, inbox: { 390: 56, 820: 56, 1440: 55 }, settings: 268, passkey: 17, graph: 9, popup: 22, setup: 68, options: 16, 'production-boot': 4 };
export function validateInventory(reports, expected = DYNAMIC_VIEWS) {
  for (const report of reports) {
    assert(DYNAMIC_VIEWS.includes(report.view), `Unknown captured view: ${report.view}`);
    for (const state of REQUIRED_STATES[report.view]) assert(report.states?.includes(state), `Missing captured state: ${report.view}:${state}`);
    const count = TRANSITION_COUNTS[report.view];
    assert.equal(report.transitions, typeof count === 'number' ? count : count[report.width], `Transition count mismatch: ${report.view}`);
    assert.deepEqual(report.states, dynamicSequence(report.view, report.width), `Transition sequence mismatch: ${report.view}`);
  }
  for (const view of expected) assert(reports.some((report) => report.view === view && report.transitions > 0), `Missing transition driver: ${view}`);
}
const seed = randomUUID();
const unseen = seed + 'W'.repeat(4096) + ' 界文字 '.repeat(32);
const permutations = (values) => values.length ? values.flatMap((value) => permutations(values.filter((item) => item !== value)).map((rest) => [value, ...rest])) : [[]];
const stable = (page) => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));

async function prepare(page, { fontScale, mode, view }) {
  await installWatcher(page, { requiredRegions: REQUIRED_REGIONS[view], taskTransitions: view === 'popup' ? 'popup' : view === 'setup', nativeTimeline: view === 'settings' });
  await page.addInitScript(({ fontScale, mode }) => {
    localStorage.setItem('mui-mode', mode);
    new MutationObserver(() => {
      if (document.documentElement && !document.documentElement.style.fontSize) document.documentElement.style.fontSize = `${16 * fontScale}px`;
    }).observe(document, { childList: true, subtree: true });
  }, { fontScale, mode });
}

async function driveWorkspace(page, view, base, step, mode) {
  await page.goto(`${base}?workspace=${view}&state=loading&transitions=1&mode=${mode}`);
  await page.waitForFunction(() => window.__workspaceFixture);
  await page.clock.install({ time: new Date('2026-06-30T12:05:00Z') });
  const push = (method, ...args) => page.evaluate(({ method, args }) => window.__workspaceFixture[method](...args), { method, args });
  await step('loading', () => page.clock.fastForward(450));
  if (view === 'passkey') {
    for (const action of ['Sign in with passkey', 'Set up a passkey']) for (const outcome of ['NotAllowedError', 'AbortError', 'Error', 'success']) {
      const key = action.startsWith('Set') ? 'passkey.enroll' : 'passkey.login';
      await step(`${action}:${outcome}:pending`, () => page.getByRole('button', { name: action, exact: true }).click());
      await step(`${action}:${outcome}`, async () => {
        if (outcome !== 'success') await push('reject', key, outcome);
        else {
          await push('resolve', key, undefined);
          await stable(page);
          if (key === 'passkey.enroll') assert(!(await push('pending')).includes('passkey.login'), 'Enrollment cannot require a second sign-in ceremony');
        }
      });
    }
    return;
  }
  if (view === 'analytics') {
    for (const prior of ['loading', 'model']) for (const state of ['loading', 'building', 'unavailable', 'baseline', 'model', 'error']) {
      await push('analytics', prior);
      await step(`${prior}:${state}`, () => push('analytics', state));
    }
    await driveWorkspaceDetails(page, view, step, unseen);
    return;
  }
  if (view === 'settings') await step.fixture('fixture:initial-connection', () => push('connection', 'connected'));
  else await push('connection', 'connected');
  if (view === 'graph') {
    for (const prior of ['pending', 'current']) for (const state of ['current', 'pending', 'unavailable', 'degraded']) {
      await push('projection', prior);
      await step(`${prior}:${state}`, () => push('projection', state, unseen));
    }
    return;
  }
  for (const state of ['fresh', 'syncing', 'populated', 'fresh', 'populated']) {
    await step(`snapshot:${state}`, () => push('snapshot', state));
  }
  for (const key of await page.evaluate(() => window.__workspaceFixture.pending())) {
    await step(`response:${key}`, () => push('release', key, 'populated'));
  }
  for (const connection of ['reconnecting', 'disconnected', 'connected', 'error', 'connected']) {
    await step(`bridge:${connection}`, () => push('connection', connection));
    await step(`grace:${connection}:2999`, () => page.clock.fastForward(2999));
    await step(`grace:${connection}:3000`, () => page.clock.runFor(1));
  }
  if (view === 'inbox') {
    const conversation = page.getByRole('button', { name: /^Conversation with/ }).first();
    await step('message:pending', () => conversation.click());
    await step('message:error', () => push('reject', 'message.getPage', 'Error'));
  }
  if (view === 'settings') {
    const keys = ['pairing.pins', 'history.get', 'onboarding.readiness', 'vault.get'];
    for (const order of permutations(keys)) {
      await step.fixture('fixture:response-order-reset', async () => { await push('refresh'); await stable(page); });
      for (const key of order) await step(`fixture-api:response-order:${order.join(',')}:${key}`, () => push('release', key, 'populated'));
    }
    for (const key of keys) {
      await step.fixture('fixture:response-error-reset', async () => { await push('refresh'); await stable(page); });
      await step(`fixture-api:response-error:${key}`, () => push('reject', key, 'Error'));
      await step.fixture('fixture:response-error-release', async () => {
        for (const other of keys.filter((item) => item !== key)) await push('release', other, 'populated');
      });
    }
    await step.fixture('fixture:response-final-reset', async () => {
      await push('refresh');
      await stable(page);
      for (const key of keys) await push('release', key, 'populated');
      await page.clock.fastForward(450);
      await stable(page);
    });
    for (const capture of ['active', 'paused', 'off']) for (const site_access of ['granted', 'needs_approval', 'reload_required']) for (const history_permission of ['granted', 'missing']) for (const legal_review_required of [false, true]) {
      await step(`browser:${capture}:${site_access}:${history_permission}:${legal_review_required}`, () => push('browser', { capture, site_access, history_permission, legal_review_required }));
    }
    await step.fixture('fixture:before-browser-controls', () => push('browser', { capture: 'active' }));
    await step('control:pending', () => page.getByRole('button', { name: 'Pause collecting', exact: true }).click());
    await step('control:delivered', () => push('resolve', 'browser.setCapture', 'delivered'));
    await step('control:9999', () => page.clock.fastForward(9999));
    await step('control:10000', () => page.clock.runFor(1));
    await step('control:authoritative', () => push('browser', { capture: 'paused' }));
    await step('control:bridge-lost', () => push('connection', 'reconnecting'));
    await step('control:bridge-lost-grace', () => page.clock.fastForward(3001));
    await step('control:bridge-return', () => push('connection', 'connected'));
    const disconnect = page.getByRole('button', { name: 'Disconnect browser extension 1' });
    await step('disconnect:open', () => disconnect.click());
    await step('disconnect:cancel', () => page.getByRole('button', { name: 'Cancel', exact: true }).click());
  }
  await driveWorkspaceDetails(page, view, step, unseen);
}


async function driveSurface(page, surface, step) {
  const cases = Object.entries(SURFACE_STATES).filter(([, state]) => state.surface === surface);
  const initial = cases[0][1];
  const html = await surfaceDocument(initial), script = await surfaceBundle(surface);
  const css = Object.fromEntries(await Promise.all(['popup', 'setup'].map(async (name) => [name, await readFile(new URL(`../../extension/${name}.css`, import.meta.url), 'utf8')])));
  await page.addInitScript(installSurfaceFixture, initial);
  await page.route('**/*', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('.css')) return route.fulfill({ contentType: 'text/css', body: css[path.slice(1, -4)] });
    if (path === `/${surface}.js`) return route.fulfill({ contentType: 'text/javascript', body: script });
    if (path === `/${surface}.html`) return route.fulfill({ contentType: 'text/html', body: html });
    return route.fulfill({ status: 404 });
  });
  const initialHash = initial.hash ?? (surface === 'setup' ? 'journey=11111111-1111-4111-8111-111111111111' : '');
  await page.goto(`http://surface-fixture.localhost/${surface}.html${initialHash ? '#' + initialHash : ''}`);
  for (const prior of [cases[0], cases.at(-1)]) for (const [name, state] of cases) {
    const change = (value) => page.evaluate((value) => {
      window.__surfaceFixture.change(value, true);
      location.hash = value.hash ?? (value.surface === 'setup' ? 'journey=11111111-1111-4111-8111-111111111111' : '');
    }, value);
    await change(prior[1]);
    await page.waitForTimeout(250);
    await step(`${prior[0]}:${name}`, () => change(state));
    await page.waitForTimeout(250);
    if (state.dialog) {
      await step(`${name}:dialog`, () => page.locator('#delete-local-data').click());
      await page.keyboard.press('Escape');
    }
  }
  for (const field of ['phase', 'pairing', 'commercial']) await step(`unknown:${field}`, () => page.evaluate(({ field, value, initial }) => window.__surfaceFixture.change({ ...initial, [field]: value }, true), { field, value: unseen, initial }));
  for (const value of [null, 0, Number.MAX_SAFE_INTEGER]) await step(`preview-counts:${value ?? 'null'}`, () => page.evaluate(({ value, initial }) => window.__surfaceFixture.change({ ...initial, preview: Object.fromEntries(['message_observations', 'chat_observations', 'inbound_observations', 'outbound_observations'].map((key) => [key, value])) }, true), { value, initial }));
}

async function driveProduction(page, step) {
  const root = new URL('../../app/static/dist/', import.meta.url);
  let enrolled = true, stateAvailable = true;
  const requests = [], segments = [];
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'credentials', { value: { get: async () => { throw new DOMException('Synthetic cancellation', 'NotAllowedError'); }, create: async () => { throw new DOMException('Synthetic cancellation', 'NotAllowedError'); } } });
  });
  await page.route('**/*', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/webauthn/session-state') return route.fulfill({ status: stateAvailable ? 200 : 503, contentType: 'application/json', body: JSON.stringify({ enrolled, authenticated: false, csrf_token: null }) });
    if (route.request().method() === 'POST') requests.push(path);
    if (path.startsWith('/api/')) return route.fulfill({ status: 503, contentType: 'application/json', body: '{}' });
    const assetPath = path.replace(/^\/static\/dist\//u, '/');
    if (!/^\/(assets\/[\w.-]+|index.html)?$/.test(assetPath)) return route.abort();
    if (/\.(js|woff2)$/.test(assetPath)) await new Promise((resolve) => setTimeout(resolve, 750));
    const body = await readFile(new URL(assetPath === '/' ? 'index.html' : assetPath.slice(1), root));
    await route.fulfill({ body, contentType: path.endsWith('.js') ? 'text/javascript' : path.endsWith('.css') ? 'text/css' : path.endsWith('.woff2') ? 'font/woff2' : 'text/html' });
  });
  await page.goto('http://production-fixture.localhost/');
  await page.getByRole('heading', { name: 'Sign in on this computer' }).waitFor();
  await step('delayed-renderer-and-fonts', () => stable(page));
  await step('request-error:Sign in with passkey', async () => {
    await page.getByRole('button', { name: 'Sign in with passkey', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: 'Sign-in could not start. Try again.' }).waitFor();
  });
  segments.push({ state: 'enrolled', ...await readWatcher(page) });
  assert.deepEqual(segments.at(-1).failures, []);
  await step('request-error:Create passkey', async () => {
    enrolled = false;
    await page.reload();
    await page.getByRole('heading', { name: 'Create a passkey on this computer' }).waitFor();
    await page.getByRole('button', { name: 'Create passkey', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: 'Passkey setup could not be confirmed.' }).waitFor();
  });
  segments.push({ state: 'not-enrolled', ...await readWatcher(page) });
  assert.deepEqual(segments.at(-1).failures, []);
  await step('session-status-unavailable', async () => {
    stateAvailable = false;
    await page.reload();
    await page.getByRole('heading', { name: 'Passkey setup could not be checked' }).waitFor();
    await page.getByRole('button', { name: 'Try again', exact: true }).waitFor({ state: 'visible', timeout: 15_000 });
    assert(await page.getByRole('button', { name: 'Try again', exact: true }).isVisible());
    assert.equal(await page.getByRole('button', { name: /Sign in with passkey|Create passkey/ }).count(), 0);
    assert.equal(requests.length, 2, 'Each known profile gets one ceremony attempt; unavailable status cannot start one');
  });
  return segments;
}

export async function captureDynamicTransitions(browser, base, outDir, only = DYNAMIC_VIEWS, focused = false, recorder = null) {
  const directory = join(outDir, 'transitions');
  await mkdir(directory, { recursive: true });
  const reports = [];
  const cases = dynamicCases(only, focused);
  await runCaptureJobs(cases, async ({ view, width, mode, fontScale, motion }) => {
      const viewport = { width, height: view === 'popup' || width === 1366 ? 600 : width === 480 ? 760 : width === 390 ? 844 : 900 };
      const name = [view, width, mode, fontScale, motion].join('-');
      const finish = recorder?.begin(`dynamic-${name}`, { view, width, mode, fontScale, motion });
      const page = await browser.newPage({ viewport, colorScheme: mode, reducedMotion: motion });
      const transitions = [], auxiliary = [], errors = [];
      let scrolling = [], segments = [];
      await prepare(page, { fontScale, mode, view });
      let observing = false;
      const observe = async (id, action, destination) => {
        if (observing) throw new Error('Overlapping driver observation: ' + id);
        observing = true;
        try {
          const boundary = () => view === 'settings' ? nativeBoundary(page)
            : page.evaluate(() => ({ at: performance.now(), frame: window.__regionWatcher.frames.length, boxes: window.__regionWatcher.frames.at(-1)?.regions }));
          const before = await boundary();
          await action();
          if (view === 'settings') await settleNativeWatcher(page); else await stable(page);
          const taskGeometry = view === 'setup' ? await assertSetupTaskGeometry(page) : null;
          destination.push({ id, before, after: await boundary(), taskGeometry });
        } finally { observing = false; }
      };
      const step = (id, action) => observe(id, action, transitions);
      step.fixture = (id, action) => { settingsFixturePolicy(id); return observe(id, action, auxiliary); };
      try {
        if (['popup', 'setup', 'options'].includes(view)) await driveSurface(page, view, step);
        else if (view === 'production-boot') segments = await driveProduction(page, step);
        else await driveWorkspace(page, view, base, step, mode);
        if (!transitions.length) errors.push('No transitions captured');
        if (view === 'settings') await step.fixture('fixture:scroll-audit', async () => { scrolling = await auditScrolling(page); });
        else scrolling = await auditScrolling(page);
        if (scrolling.some((entry) => !entry.positions.at(-1).endReached || entry.positions.some((position) => position.horizontalEscape))) errors.push('Reading coverage failed');
      } catch (error) { errors.push(error.message); }
      const watcher = await readWatcher(page).catch((error) => ({ failures: [error.message] }));
      if (view === 'settings' && watcher.frames) {
        const geometry = inspectSettingsFrames(watcher.frames, transitions, REQUIRED_REGIONS.settings, { ...watcher, auxiliary });
        watcher.failures = geometry.failures;
        watcher.transitionGeometry = geometry.policies;
        watcher.shiftAttribution = geometry.shiftAttribution;
        watcher.auxiliaryGeometry = geometry.auxiliaryPolicies;
      }
      const failures = [...errors, ...watcher.failures];
      if (failures.length) await page.screenshot({ path: join(directory, name + '-failure.png') });
      await writeFile(join(directory, name + '.json'), JSON.stringify({ revision: process.env.VISUAL_CAPTURE_REVISION ?? null, view, viewport, mode, fontScale, motion, seed, transitions, auxiliary, scrolling, segments, ...watcher, failures }) + '\n');
      reports.push({ view, width, file: `transitions/${name}.json`, transitions: transitions.length, states: transitions.map(({ id }) => id), failures });
      finish?.({ outcome: failures.length ? 'failed' : 'passed', observations: transitions.map(({ id }) => id), files: [`transitions/${name}.json`] });
      await page.close();
      console.log(`transitions ${name}: ${transitions.length}, failures=${failures.length}`);
      if (failures.length) console.log(`transition-failure-counts ${name}: ${JSON.stringify(failureSummary(errors, watcher.failures))}`);
      if (view === 'settings' && watcher.failures.length) console.log(`settings-failure-stages ${name}: ${JSON.stringify(settingsFailureSummary(watcher.failures))}`);
      if (view === 'settings' && watcher.failures.length) console.log(`settings-shift-gaps ${name}: ${JSON.stringify(settingsUnassignedShiftSummary(watcher.shiftAttribution, transitions, auxiliary))}`);
  });
  assert.equal(reports.length, cases.length, 'Missing capture configuration');
  reports.sort((first, second) => first.file.localeCompare(second.file));
  await writeFile(join(directory, 'manifest.json'), JSON.stringify(reports, null, 2) + '\n');
  validateInventory(reports, only);
  return reports;
}
