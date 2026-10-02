import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { installWatcher, readWatcher, auditScrolling } from './shift-watcher.mjs';
import { installProvisioningFixture, openProvisioningFixture } from './provisioning-driver.mjs';
import { SURFACE_STATES, surfaceDocument, surfaceBundle } from '../../extension/qualification/surface-fixtures.mjs';
import { installSurfaceFixture } from '../../extension/qualification/surface-runtime-fixture.mjs';
import { driveWorkspaceDetails } from './workspace-transitions.mjs';
import { runCaptureJobs } from './capture-jobs.mjs';

export const DYNAMIC_VIEWS = ['home', 'analytics', 'inbox', 'settings', 'passkey', 'graph', 'popup', 'setup', 'options', 'provisioning', 'production-boot'];
export const REQUIRED_REGIONS = {
  home: ['freshness-status', 'issue-band', 'dashboard-notice', 'dashboard-setup-status', 'dashboard-overview', 'dashboard-basis', 'dashboard-recent', 'dashboard-setup'],
  analytics: ['freshness-status', 'issue-band', 'analytics-content'],
  inbox: ['freshness-status', 'issue-band', 'inbox-notice', 'inbox-content'],
  settings: ['freshness-status', 'issue-band', 'settings-browser', 'settings-history', 'settings-activation', 'settings-vault'],
  graph: ['freshness-status', 'issue-band', 'graph-notice'],
  passkey: ['passkey-card', 'passkey-feedback'],
  'production-boot': ['passkey-card', 'passkey-feedback'],
  popup: ['popup-content', 'popup-status', 'surface-feedback'],
  setup: ['setup-rail', 'extension-stage', 'surface-feedback'],
  options: ['options-notice', 'options-capture', 'options-connection', 'options-data', 'surface-feedback'],
  provisioning: ['provisioning-rail', 'provisioning-stage', 'provisioning-feedback', 'provisioning-actions'],
};
export const REQUIRED_STATES = {
  home: ['loading', 'snapshot:fresh', 'snapshot:syncing', 'snapshot:populated', 'read-model:resyncing', 'read-model:realtime', 'agent:config-mismatch', 'counts:null', 'coverage:blocked'],
  analytics: ['loading', 'loading:model', 'model:error', 'tone:Table', 'tone:Chart', 'dates:invalid'],
  inbox: ['loading', 'message:pending', 'message:error', 'message:empty:success', 'message:populated:error', 'message:populated:prepend', 'message:populated:long'],
  settings: ['loading', 'control:pending', 'control:9999', 'control:10000', 'control:authoritative', 'disconnect:cancel', 'archive:invalid', 'delete:error', 'redemption:success:readiness', 'redemption:network:readiness', 'export:success:response', 'pairing:300000', 'pairing:confirmed:result'],
  graph: ['loading', 'pending:current', 'current:pending', 'current:unavailable', 'current:degraded'],
  passkey: ['loading', 'Sign in with passkey:NotAllowedError:pending', 'Set up a passkey:NotAllowedError:pending'],
  popup: ['unknown:phase', 'unknown:pairing', 'unknown:commercial', 'preview-counts:null'],
  setup: ['unknown:phase', 'unknown:pairing', 'unknown:commercial'],
  options: ['unknown:phase', 'unknown:pairing', 'unknown:commercial'],
  provisioning: ['invalid-code', 'valid-code', 'claim:pending', 'claim:confirmed', 'account:confirm', 'controller:malformed', 'controller:expired', 'extension:missing', 'approval:pending', 'finalization:pending', 'finalization:response-lost-reconciled'],
  'production-boot': ['delayed-renderer-and-fonts', 'request-error:Sign in with passkey', 'request-error:Set up a passkey'],
};
const TRANSITION_COUNTS = { home: { 390: 75, 1440: 73 }, analytics: 18, inbox: { 390: 56, 820: 56, 1440: 55 }, settings: 270, passkey: 17, graph: 9, popup: 22, setup: 56, options: 16, provisioning: 47, 'production-boot': 3 };
export function validateInventory(reports, expected = DYNAMIC_VIEWS) {
  for (const report of reports) {
    assert(DYNAMIC_VIEWS.includes(report.view), `Unknown captured view: ${report.view}`);
    for (const state of REQUIRED_STATES[report.view]) assert(report.states?.includes(state), `Missing captured state: ${report.view}:${state}`);
    const count = TRANSITION_COUNTS[report.view];
    assert.equal(report.transitions, typeof count === 'number' ? count : count[report.width], `Transition count mismatch: ${report.view}`);
  }
  for (const view of expected) assert(reports.some((report) => report.view === view && report.transitions > 0), `Missing transition driver: ${view}`);
}
const seed = randomUUID();
const unseen = seed + 'W'.repeat(4096) + ' 界文字 '.repeat(32);
const permutations = (values) => values.length ? values.flatMap((value) => permutations(values.filter((item) => item !== value)).map((rest) => [value, ...rest])) : [[]];
const stable = (page) => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));

async function prepare(page, { fontScale, mode, view }) {
  await installWatcher(page, { requiredRegions: REQUIRED_REGIONS[view] });
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
          if (key === 'passkey.enroll') await push('resolve', 'passkey.login', undefined);
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
  await push('connection', 'connected');
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
    const keys = ['pairing.pins', 'history.get', 'activation.readiness', 'vault.get'];
    for (const order of permutations(keys)) {
      await push('refresh');
      await stable(page);
      for (const key of order) await step(`response-order:${order.join(',')}:${key}`, () => push('release', key, 'populated'));
    }
    for (const key of keys) {
      await push('refresh');
      await stable(page);
      await step(`response-error:${key}`, () => push('reject', key, 'Error'));
      for (const other of keys.filter((item) => item !== key)) await push('release', other, 'populated');
    }
    await push('refresh');
    await stable(page);
    for (const key of keys) await push('release', key, 'populated');
    for (const capture of ['active', 'paused', 'off']) for (const site_access of ['granted', 'needs_approval', 'reload_required']) for (const history_permission of ['granted', 'missing']) for (const legal_review_required of [false, true]) {
      await step(`browser:${capture}:${site_access}:${history_permission}:${legal_review_required}`, () => push('browser', { capture, site_access, history_permission, legal_review_required }));
    }
    await push('browser', { capture: 'active' });
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

async function driveProvisioning(page, step) {
  await installProvisioningFixture(page);
  await openProvisioningFixture(page);
  const run = (method) => page.evaluate((method) => window.__provisioningController[method](), method);
  for (const flag of ['malformed', 'expired', 'unavailable', 'identityUnavailable']) {
    await step('controller:' + flag, async () => {
      await page.evaluate((flag) => { window.__provisioningFixture[flag] = true; }, flag);
      await run(flag === 'identityUnavailable' ? 'refreshIdentity' : 'checkStatus');
    });
    await step('controller:' + flag + ':recovery', async () => {
      await page.evaluate((flag) => { window.__provisioningFixture[flag] = false; }, flag);
      await run('checkStatus'); await run('refreshIdentity');
    });
  }
  for (const stage of ['needs_terms', 'needs_full', 'needs_site_access', 'needs_account', 'ready_to_pair', 'paired']) await step('extension:' + stage, () => page.evaluate((stage) => window.__provisioningFixture.push(stage), stage));
  await step('extension:missing', () => page.evaluate(() => { window.__provisioningFixture.portMissing = true; window.__provisioningFixture.disconnect(); }));
  await step('invalid-code', () => page.locator('#claim-package').fill('invalid code'));
  await step('valid-code', () => page.locator('#claim-package').fill('abcdefgh'));
  await step('claim:pending', async () => {
    await page.evaluate(() => window.__provisioningFixture.hold.push('claim'));
    await page.locator('#claim-submit').click();
  });
  await step('claim:confirmed', () => page.evaluate(() => window.__provisioningFixture.release('claim')));
  for (const identity of [null, 'changed-fixture-account', 'fixture-account']) await step(`identity:${identity === null ? 'absent' : identity.startsWith('changed') ? 'changed' : 'present'}`, async () => {
    await page.evaluate((identity) => { window.__provisioningFixture.identity = identity; }, identity);
    await run('refreshIdentity');
  });
  await step('account:confirm', () => page.locator('#confirm-identity').click());
  await step('recovery:open', () => page.locator('#recovery-open').click());
  await step('recovery:close', () => page.locator('#recovery-close').click());
  for (const reason of ['size', 'encoding', 'profile', 'schema', 'device', 'consumed', 'binding_acquisition_unavailable', 'hosted_origin_unavailable', 'hosted_unavailable', 'installation_key_unavailable', 'membership_reference_unavailable', 'candidate_resolution_conflict', 'grant_verification_refused', 'claim_already_consumed', 'claim_refused', 'incomplete_grant_set', 'membership_refresh_unavailable', unseen]) {
    await page.evaluate((reason) => { window.__provisioningFixture.refusal = reason; }, reason);
    await step(`approval:refusal:${reason === unseen ? 'unknown' : reason}`, () => run('acquireAssociation'));
  }
  await step('approval:pending', async () => {
    await page.evaluate(() => { window.__provisioningFixture.refusal = null; window.__provisioningFixture.loseFinalize = true; window.__provisioningFixture.hold.push('acquire', 'finalize'); window.dispatchEvent(new Event('focus')); });
  });
  await step('finalization:pending', () => page.evaluate(() => window.__provisioningFixture.release('acquire')));
  await step('finalization:completed', () => page.evaluate(() => window.__provisioningFixture.release('finalize')));
  await step('finalization:response-lost-reconciled', () => run('finalizeProvisioning'));
  assert.equal(await page.evaluate(() => window.__provisioningFixture.calls.filter((operation) => operation === 'finalize').length), 1);
  assert(await page.getByRole('heading', { name: 'Setup finished' }).isVisible());
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
  await page.goto(`http://surface-fixture.localhost/${surface}.html`);
  await page.clock.install();
  for (const prior of [cases[0], cases.at(-1)]) for (const [name, state] of cases) {
    const change = (value) => page.evaluate((value) => { window.__surfaceFixture.change(value, true); location.hash = value.hash ?? ''; }, value);
    await change(prior[1]);
    await page.clock.fastForward(250);
    await step(`${prior[0]}:${name}`, () => change(state));
    await page.clock.fastForward(250);
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
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'credentials', { value: { get: async () => { throw new DOMException('Synthetic cancellation', 'NotAllowedError'); }, create: async () => { throw new DOMException('Synthetic cancellation', 'NotAllowedError'); } } });
  });
  await page.route('**/*', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.startsWith('/api/')) return route.fulfill({ status: 503, contentType: 'application/json', body: '{}' });
    if (!/^\/(assets\/[\w.-]+|index.html)?$/.test(path)) return route.abort();
    if (/\.(js|woff2)$/.test(path)) await new Promise((resolve) => setTimeout(resolve, 750));
    const body = await readFile(new URL(path === '/' ? 'index.html' : path.slice(1), root));
    await route.fulfill({ body, contentType: path.endsWith('.js') ? 'text/javascript' : path.endsWith('.css') ? 'text/css' : path.endsWith('.woff2') ? 'font/woff2' : 'text/html' });
  });
  await page.goto('http://production-fixture.localhost/');
  await page.getByRole('heading', { name: 'Protect access to your messages' }).waitFor();
  await step('delayed-renderer-and-fonts', () => stable(page));
  for (const action of ['Sign in with passkey', 'Set up a passkey']) await step(`request-error:${action}`, () => page.getByRole('button', { name: action, exact: true }).click());
}

export async function captureDynamicTransitions(browser, base, outDir, only = DYNAMIC_VIEWS, focused = false) {
  const directory = join(outDir, 'transitions');
  await mkdir(directory, { recursive: true });
  const reports = [];
  const cases = [];
  for (const view of only) {
    assert(DYNAMIC_VIEWS.includes(view), `Unknown transition view: ${view}`);
    const widths = view === 'popup' ? [320, 390] : view === 'inbox' ? [390, 820, 1440] : ['passkey', 'production-boot'].includes(view) ? [390, 1366, 1440] : ['setup', 'provisioning'].includes(view) ? [390, 480, 1440] : [390, 1440];
    for (const width of widths) for (const mode of ['light', 'dark']) for (const fontScale of [1, 1.25]) for (const motion of ['reduce', 'no-preference']) {
      if (focused && (width !== widths[0] || mode !== 'light' || fontScale !== 1 || motion !== 'reduce')) continue;
      cases.push({ view, width, mode, fontScale, motion });
    }
  }
  await runCaptureJobs(cases, async ({ view, width, mode, fontScale, motion }) => {
      const viewport = { width, height: view === 'popup' || width === 1366 ? 600 : width === 480 ? 760 : width === 390 ? 844 : 900 };
      const name = [view, width, mode, fontScale, motion].join('-');
      const page = await browser.newPage({ viewport, colorScheme: mode, reducedMotion: motion });
      const transitions = [], errors = [];
      let scrolling = [];
      await prepare(page, { fontScale, mode, view });
      const step = async (id, action) => {
        const before = await page.evaluate(() => ({ at: performance.now(), frame: window.__regionWatcher.frames.length, boxes: window.__regionWatcher.frames.at(-1)?.regions }));
        await action();
        await stable(page);
        transitions.push({ id, before, after: await page.evaluate(() => ({ at: performance.now(), frame: window.__regionWatcher.frames.length, boxes: window.__regionWatcher.frames.at(-1)?.regions })) });
      };
      try {
        if (view === 'provisioning') await driveProvisioning(page, step);
        else if (['popup', 'setup', 'options'].includes(view)) await driveSurface(page, view, step);
        else if (view === 'production-boot') await driveProduction(page, step);
        else await driveWorkspace(page, view, base, step, mode);
        if (!transitions.length) errors.push('No transitions captured');
        scrolling = await auditScrolling(page);
        if (scrolling.some((entry) => !entry.positions.at(-1).endReached || entry.positions.some((position) => position.horizontalEscape))) errors.push('Reading coverage failed');
      } catch (error) { errors.push(error.message); }
      const watcher = await readWatcher(page).catch((error) => ({ failures: [error.message] }));
      const failures = [...errors, ...watcher.failures];
      if (failures.length) await page.screenshot({ path: join(directory, name + '-failure.png') });
      await writeFile(join(directory, name + '.json'), JSON.stringify({ revision: process.env.VISUAL_CAPTURE_REVISION ?? null, view, viewport, mode, fontScale, motion, seed, transitions, scrolling, ...watcher, failures }) + '\n');
      reports.push({ view, width, file: `transitions/${name}.json`, transitions: transitions.length, states: transitions.map(({ id }) => id), failures });
      await page.close();
      console.log(`transitions ${name}: ${transitions.length}, failures=${failures.length}`);
  });
  assert.equal(reports.length, cases.length, 'Missing capture configuration');
  reports.sort((first, second) => first.file.localeCompare(second.file));
  await writeFile(join(directory, 'manifest.json'), JSON.stringify(reports, null, 2) + '\n');
  validateInventory(reports, only);
  return reports;
}
