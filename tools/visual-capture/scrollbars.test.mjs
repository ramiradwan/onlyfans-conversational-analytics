import assert from 'node:assert/strict';
import { once } from 'node:events';
import { createRequire } from 'node:module';
import { after, before, test } from 'node:test';
import { chromium } from 'playwright';
import { FIXED_NOW, startHarness } from './capture.mjs';
import { renderSurfaceState, SURFACE_STATES } from '../../extension/qualification/surface-fixtures.mjs';
import { payload } from '../../extension/test-fixtures/browser-task.mjs';

const require = createRequire(new URL('../../frontend/package.json', import.meta.url));
const Color = require('colorjs.io').default;
let browser, harness;
before(async () => { browser = await chromium.launch(); harness = await startHarness(); });
after(async () => {
  await browser?.close();
  if (harness?.vite.exitCode === null) {
    const exited = once(harness.vite, 'exit');
    harness.vite.kill();
    await exited;
  }
});

const brain = ['/api', '/openapi.json', '/ws'];
async function context(options) {
  const context = await browser.newContext({ ...options, reducedMotion: 'reduce', serviceWorkers: 'block' });
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    // Never contact a running Brain or any other host.
    if (brain.some(prefix => url.pathname.startsWith(prefix))) return route.abort();
    if (['http:', 'https:'].includes(url.protocol) && url.origin !== new URL(harness.base).origin && url.hostname !== 'extension-ui.test') return route.abort();
    return route.continue();
  });
  await context.routeWebSocket('**/*', socket => {
    const url = new URL(socket.url());
    if (url.host === new URL(harness.base).host && !brain.some(prefix => url.pathname.startsWith(prefix))) return socket.connectToServer();
    return socket.close();
  });
  return context;
}

const workspace = name => async (page, mode) => {
  await page.goto(`${harness.base}?workspace=${name}&state=populated&mode=${mode}`);
  await page.locator('h1').first().waitFor();
};
const rendered = (path, view, render, state) => async page => {
  await page.goto(new URL(path, harness.base).href, { waitUntil: 'load' });
  await page.evaluate(async ({ view, render, state }) => (await import(view))[render](document, state), { view, render, state });
};
const desktopSetup = rendered('/onboarding/index.html', '/onboarding/view.js', 'renderSetup', { connected: true, pending: null, error: null,
  snapshot: { next_task: 'browser-access', facts: {}, pending_operations: [], allowed_actions: ['browser.access'] } });
const browserTask = rendered('/browser-tasks/index.html', '/browser-tasks/view.js', 'renderBrowserTask', (() => {
  const task = payload(Date.parse(FIXED_NOW));
  task.task.purpose = 'browser.pairing-confirmation';
  task.creator_account_id = 'synthetic-creator';
  return { state: 'pairing_checking', payload: task };
})());
async function setupCode(page) {
  const journey = '11111111-1111-4111-8111-111111111111';
  const session = { csrf_token: 'A'.repeat(43), expires_at: Math.floor(Date.parse(FIXED_NOW) / 1000) + 600,
    hosted_url: 'https://setup.example.test/public/onboarding/setup', journey_id: journey, workspace_journey_id: '22222222-2222-4222-8222-222222222222' };
  await page.route('**/api/v1/**', route => {
    const { pathname } = new URL(route.request().url());
    if (route.request().method() !== 'GET') return route.abort();
    const body = pathname === '/api/v1/setup-code/session' ? session : pathname === '/api/v1/provisioning/setup-transfer/context' ? { state: 'none' } : null;
    return route.fulfill({ status: body ? 200 : 404, contentType: 'application/json', body: JSON.stringify(body) });
  });
  await page.goto(new URL(`/setup-code/index.html#journey=${journey}`, harness.base).href, { waitUntil: 'load' });
  await page.locator('#receiving-form:not([hidden])').waitFor();
}
const extension = (state, ready) => async page => {
  await renderSurfaceState(page, state);
  if (ready) await page.locator(ready).first().waitFor();
};

const app = { width: 1180, height: 640 }, desktop = { width: 1280, height: 520 };
const SURFACES = [
  ['Home', app, workspace('home')],
  ['Analytics', app, workspace('analytics')],
  ['Inbox', app, workspace('inbox')],
  ['Settings', app, workspace('settings')],
  ['desktop setup', desktop, desktopSetup],
  ['browser task', desktop, browserTask],
  ['setup code', desktop, setupCode],
  ['extension popup', { width: 390, height: 600 }, extension(SURFACE_STATES.preview)],
  ['extension setup', { width: 1280, height: 820 }, extension(SURFACE_STATES.full_review, '#full-disclosure')],
  ['extension options', { width: 640, height: 480 }, extension(SURFACE_STATES.connection)],
];

// Reads the page and every rendered scroll container, with the theme thumb and system button text colors.
function readScrollbars() {
  const clear = color => color === 'transparent' || /^rgba\(.*,\s*0\)$/.test(color) || /\/\s*0\)$/.test(color);
  const backdrop = node => {
    for (let item = node; item; item = item.parentElement) {
      const color = getComputedStyle(item).backgroundColor;
      if (!clear(color)) return color;
    }
    const color = getComputedStyle(document.body).backgroundColor;
    return clear(color) ? 'rgb(255, 255, 255)' : color;
  };
  const probe = document.createElement('span');
  document.body.append(probe);
  probe.style.color = 'var(--dipsy-intent-surface-scrollbar-thumb, var(--bridge-palette-surface-scrollbarThumb))';
  const token = getComputedStyle(probe).color;
  probe.style.color = 'ButtonText';
  const system = getComputedStyle(probe).color;
  probe.remove();
  const root = document.documentElement;
  const scrollers = [root, document.body, ...document.body.querySelectorAll('*')].filter(node => node === root
    || node.getClientRects().length && /auto|scroll/.test(getComputedStyle(node).overflowX + getComputedStyle(node).overflowY));
  return { token, system, scrollers: scrollers.map(node => {
    const style = getComputedStyle(node);
    const scrolls = node === root ? root.scrollHeight > innerHeight || root.scrollWidth > innerWidth
      : node.scrollHeight > node.clientHeight || node.scrollWidth > node.clientWidth;
    return { name: node === root ? 'page' : [node.localName, node.id && `#${node.id}`, node.getAttribute('data-reserved-region')].filter(Boolean).join(' '),
      active: scrolls || style.scrollbarGutter.includes('stable'), width: style.scrollbarWidth, color: style.scrollbarColor,
      thumb: getComputedStyle(node, '::-webkit-scrollbar-thumb').backgroundColor, backdrop: backdrop(node) };
  }) };
}
const colors = value => value.match(/[a-z-]+\((?:[^()]|\([^()]*\))*\)|#[0-9a-f]+|[a-z]+/gi) ?? [];

for (const mode of ['light', 'dark']) test(`every scrollbar is thin with the theme thumb in ${mode}`, async () => {
  const thumbs = new Set();
  for (const [surface, viewport, open] of SURFACES) {
    const owner = await context({ viewport, colorScheme: mode });
    const page = await owner.newPage();
    try {
      await page.clock.setFixedTime(FIXED_NOW);
      await open(page, mode);
      await page.evaluate(() => document.fonts.ready);
      const regular = await page.evaluate(readScrollbars);
      assert(regular.scrollers.some(scroller => scroller.active), `${surface} shows no scrollbar to check`);
      for (const scroller of regular.scrollers) {
        const where = `${surface} ${scroller.name} in ${mode}`;
        assert.equal(scroller.width, 'thin', `${where} uses the default scrollbar width`);
        const [thumb, track] = colors(scroller.color);
        assert(thumb && track, `${where} uses the default scrollbar colors`);
        assert(new Color(thumb).deltaE2000(new Color(regular.token)) < 1, `${where} thumb ${thumb} is not the theme thumb ${regular.token}`);
        assert.equal(Number(new Color(track).alpha), 0, `${where} track is not clear`);
        assert(new Color(thumb).contrastWCAG21(new Color(scroller.backdrop)) >= 3, `${where} thumb has insufficient contrast with ${scroller.backdrop}`);
      }
      thumbs.add(new Color(regular.token).to('srgb').toString({ precision: 3 }));
      await page.emulateMedia({ forcedColors: 'active' });
      const forced = await page.evaluate(readScrollbars);
      for (const scroller of forced.scrollers) {
        const where = `${surface} ${scroller.name} with forced colors in ${mode}`;
        assert.equal(scroller.width, 'auto', where);
        assert.equal(scroller.color, 'auto', where);
        assert.equal(scroller.thumb, forced.system, `${where} thumb is not the system button text color`);
      }
    } finally { await owner.close(); }
  }
  assert.equal(thumbs.size, 1, `Surfaces use different scrollbar thumbs in ${mode}: ${[...thumbs].join(', ')}`);
});
