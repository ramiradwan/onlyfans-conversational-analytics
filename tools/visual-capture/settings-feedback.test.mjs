import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdir, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { after, before, test } from 'node:test';
import { chromium } from 'playwright';
import { startHarness } from './capture.mjs';
import { inspectFrames, nativeBoundary, observeRegions, readWatcher, settleNativeWatcher } from './shift-watcher.mjs';
import { drivePairingExpiry, openPairingFallback, waitForPairingOpenPresentation } from './workspace-transitions.mjs';
import { renderSurfaceState, SURFACE_STATES } from '../../extension/qualification/surface-fixtures.mjs';

const require = createRequire(new URL('../../frontend/package.json', import.meta.url));
const Color = require('colorjs.io').default;
const evidence = process.env.SETTINGS_FEEDBACK_EVIDENCE;
let browser, harness;
before(async () => {
  if (evidence) await mkdir(evidence);
  browser = await chromium.launch();
  harness = await startHarness();
});
after(async () => {
  await browser?.close();
  if (harness?.vite.exitCode === null) {
    const exited = once(harness.vite, 'exit');
    harness.vite.kill();
    await exited;
  }
});

const settle = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
const push = (page, method, ...args) => page.evaluate(({ method, args }) => window.__workspaceFixture[method](...args), { method, args });
async function context(options) {
  const context = await browser.newContext({ ...options, serviceWorkers: 'block' });
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    // Match Vite's prefix semantics and never contact a running Brain.
    if (['/api', '/openapi.json', '/ws'].some(prefix => url.pathname.startsWith(prefix))) return route.abort();
    if (['http:', 'https:'].includes(url.protocol) && url.origin !== new URL(harness.base).origin && url.hostname !== 'extension-ui.test') return route.abort();
    return route.continue();
  });
  await context.routeWebSocket('**/*', socket => {
    const url = new URL(socket.url());
    if (url.host === new URL(harness.base).host && !['/api', '/openapi.json', '/ws'].some(prefix => url.pathname.startsWith(prefix))) return socket.connectToServer();
    return socket.close();
  });
  return context;
}
async function settings(page, mode, scale = 1) {
  await page.goto(`${harness.base}?workspace=settings&state=loading&transitions=1&mode=${mode}`);
  await page.waitForFunction(() => window.__workspaceFixture);
  await push(page, 'snapshot', 'populated');
  await push(page, 'connection', 'connected');
  for (const key of await push(page, 'pending')) await push(page, 'release', key, 'populated');
  await push(page, 'browser', { capture: 'active' });
  await page.getByRole('heading', { name: 'Archive', exact: true }).waitFor();
  await page.evaluate(async scale => { await document.fonts.ready; document.documentElement.style.fontSize = `${16 * scale}px`; }, scale);
  await page.waitForTimeout(250);
  await settle(page);
}
async function record(page, name, report) {
  if (!evidence) return;
  await writeFile(join(evidence, `${name}.json`), JSON.stringify(report, null, 2) + '\n');
  await page.screenshot({ path: join(evidence, `${name}.png`), fullPage: true });
}
async function startPassive(page, requiredRegions) {
  // Initial fixture responses and the explicit click settle before this window.
  // The observer still records its complete buffered native shift history.
  await page.evaluate(observeRegions, { requiredRegions, nativeTimeline: true });
  await settle(page);
}
async function passiveReport(page) {
  const raw = await readWatcher(page);
  assert(raw.nativeTimeline.clockVerified && raw.nativeTimeline.completed);
  const start = raw.nativeTimeline.capturedAt;
  const bufferedBefore = raw.rawShifts.filter(shift => shift.at < start);
  const observed = raw.rawShifts.filter(shift => shift.at >= start);
  assert.equal(bufferedBefore.length + observed.length, raw.rawShifts.length);
  const frames = raw.frames.map(frame => ({ ...frame, shifts: frame.shifts.filter(shift => shift.at >= start) }));
  return { ...raw, passiveWindow: { start, end: raw.nativeTimeline.endAt, bufferedBefore, observed }, passiveFailures: inspectFrames(frames) };
}
async function textFits(locator) {
  return locator.evaluate(node => {
    const bounds = node.getBoundingClientRect();
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const text = walker.currentNode;
      const style = getComputedStyle(text.parentElement);
      if (!text.textContent.trim() || style.visibility !== 'visible' || style.display === 'none') continue;
      const range = document.createRange(); range.selectNodeContents(text);
      if ([...range.getClientRects()].some(rect => rect.left < bounds.left - 1 || rect.right > bounds.right + 1 || rect.top < bounds.top - 1 || rect.bottom > bounds.bottom + 1)) return false;
    }
    return node.scrollWidth <= node.clientWidth;
  });
}

test('pairing expiry driver holds its boundary through slow native observation, then expires explicitly', async () => {
  const owner = await context({ viewport: { width: 1440, height: 900 }, colorScheme: 'light' });
  const page = await owner.newPage();
  try {
    await settings(page, 'light', 1.25);
    await push(page, 'desktopSnapshot', { allowed_actions: ['activation.review'] });
    await push(page, 'refresh');
    await settle(page);
    for (const key of await push(page, 'pending')) await push(page, 'release', key, 'fresh');
    await page.getByRole('button', { name: 'Connection options', exact: true }).waitFor();
    await startPassive(page, ['settings-browser']);
    await page.clock.install({ time: new Date('2026-06-30T12:05:00Z') });
    const openedAt = await openPairingFallback(page);
    const pin = await push(page, 'apiSample', 'pairing');
    const stages = [];
    const step = async (id, action) => {
      await action();
      await settleNativeWatcher(page);
      if (id === 'pairing:before-expiry') {
        const before = await page.evaluate(() => ({ date: Date.now(), native: window.__regionWatchControl.boundary().nativeAt }));
        // Deliberately exceed the one-second expiry margin in real time.
        await new Promise(resolve => setTimeout(resolve, 1500));
        await settleNativeWatcher(page);
        const after = await page.evaluate(() => ({ date: Date.now(), native: window.__regionWatchControl.boundary().nativeAt }));
        assert(after.native - before.native >= 1500);
        assert.equal(after.date, before.date, 'Driver latency must not advance the expiry state');
        assert(await page.getByText('Waiting for the browser extension to connect.', { exact: true }).isVisible());
        assert.equal(await page.getByText('Time ran out before the connection finished. Try again.', { exact: true }).count(), 0);
      }
      stages.push(id);
    };
    await drivePairingExpiry(page, pin, (method, ...args) => push(page, method, ...args), step, openedAt);
    assert.deepEqual(stages, ['pairing:pending', 'pairing:before-expiry', 'pairing:expired']);
    assert(await page.getByText('Time ran out before the connection finished. Try again.', { exact: true }).isVisible());
    const before = await page.evaluate(() => Date.now());
    await new Promise(resolve => setTimeout(resolve, 50));
    assert((await page.evaluate(() => Date.now())) > before, 'Later driver actions require a resumed clock');
  } finally { await owner.close(); }
});

test('pairing expiry driver preserves the original open deadline when its response is delayed', async () => {
  const owner = await context({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark', reducedMotion: 'reduce' });
  const page = await owner.newPage();
  try {
    await settings(page, 'dark', 1.25);
    await push(page, 'desktopSnapshot', { allowed_actions: ['activation.review'] });
    await push(page, 'refresh');
    await settle(page);
    for (const key of await push(page, 'pending')) await push(page, 'release', key, 'fresh');
    await page.getByRole('button', { name: 'Connection options', exact: true }).waitFor();
    await startPassive(page, ['settings-browser']);
    await page.clock.install({ time: new Date('2026-06-30T12:05:00Z') });
    // Capture the actual open click before React starts its local deadline.
    await page.evaluate(() => {
      const recordOpen = event => {
        if (!(event.target instanceof Element)
          || event.target.closest('button')?.textContent.trim() !== 'Compare connection codes') return;
        window.__pairingExpiryOpenedAt = Date.now();
        document.removeEventListener('click', recordOpen, true);
      };
      document.addEventListener('click', recordOpen, true);
    });
    await openPairingFallback(page);
    const openedAt = await page.evaluate(() => window.__pairingExpiryOpenedAt);
    assert(Number.isFinite(openedAt), 'The real open action must establish the deadline anchor');
    // The component's five-minute cap already runs while the response is pending.
    await page.clock.fastForward(4000);
    assert((await page.evaluate(() => Date.now())) - openedAt >= 4000);
    const pin = await push(page, 'apiSample', 'pairing');
    page.setDefaultTimeout(3000);
    const stages = [];
    const step = async (id, action) => {
      await action();
      await settleNativeWatcher(page);
      if (id === 'pairing:before-expiry') {
        assert((await page.evaluate(() => Date.now())) - openedAt < 300_000,
          'The before-expiry observation must precede the original local deadline');
        assert(await page.getByText('Waiting for the browser extension to connect.', { exact: true }).isVisible());
        assert.equal(await page.getByText('Time ran out before the connection finished. Try again.', { exact: true }).count(), 0);
      }
      stages.push(id);
    };
    await drivePairingExpiry(page, pin, (method, ...args) => push(page, method, ...args), step, openedAt);
    assert.deepEqual(stages, ['pairing:pending', 'pairing:before-expiry', 'pairing:expired']);
    assert(await page.getByText('Time ran out before the connection finished. Try again.', { exact: true }).isVisible());
  } finally { await owner.close(); }
});

test('pairing driver completes the waiting presentation before a passive companion notice under delayed scheduling', async () => {
  const owner = await context({ viewport: { width: 390, height: 844 }, colorScheme: 'dark', reducedMotion: 'reduce' });
  const page = await owner.newPage();
  try {
    await page.addInitScript(() => {
      const Channel = MessageChannel, nativeTask = setTimeout.bind(window);
      window.MessageChannel = class extends Channel {
        constructor() {
          super();
          const send = this.port2.postMessage.bind(this.port2);
          this.port2.postMessage = (...args) => nativeTask(() => send(...args), 200);
        }
      };
    });
    await page.goto(`${harness.base}?workspace=settings&state=loading&transitions=1&mode=dark`);
    await page.waitForFunction(() => window.__workspaceFixture);
    await push(page, 'snapshot', 'fresh');
    await push(page, 'connection', 'connected');
    await page.waitForFunction(() => ['history.get', 'vault.get', 'pairing.pins'].every(key => window.__workspaceFixture.pending().includes(key)));
    for (const key of await push(page, 'pending')) await push(page, 'release', key, 'fresh');
    await push(page, 'desktopSnapshot', { allowed_actions: ['activation.review'] });
    await page.getByRole('button', { name: 'Connection options', exact: true }).waitFor();
    await page.evaluate(() => document.fonts.ready);
    await startPassive(page, ['settings-browser', 'browser-feedback']);
    await page.clock.install({ time: new Date('2026-06-30T12:05:00Z') });
    await openPairingFallback(page);
    const pin = await push(page, 'apiSample', 'pairing');
    const expires_at = await page.evaluate(() => new Date(Date.now() + 300_000).toISOString());
    await push(page, 'resolve', 'pairing.open', { ...pin, expires_at, state: 'open', version: 0, comparison_code: null, agent_identity_thumbprint: null });
    await waitForPairingOpenPresentation(page);
    await settleNativeWatcher(page);
    const before = await nativeBoundary(page);
    await push(page, 'companion', { creator_account_id: pin.creator_account_id, revision: 101, changed_at: expires_at });
    await page.waitForFunction(() => window.__workspaceFixture.pending().includes('pairing.get'));
    // Observe both the notice and delayed work after it without resolving get().
    await new Promise(resolve => setTimeout(resolve, 350));
    await settleNativeWatcher(page);
    const raw = await readWatcher(page);
    const observed = raw.rawShifts.filter(shift => shift.at >= before.nativeAt);
    const frames = raw.frames.slice(Math.max(0, before.frame - 1))
      .map(frame => ({ ...frame, shifts: frame.shifts.filter(shift => shift.at >= before.nativeAt) }));
    assert.equal(observed.length, 0, 'A passive notice cannot admit a delayed open-page layout change');
    assert.equal(inspectFrames(frames).length, 0, 'Passive companion-notice geometry must stay stable');
    assert((await push(page, 'pending')).includes('pairing.get'));
    assert(await page.getByText('Waiting for the browser extension to connect.', { exact: true }).isVisible());
    await record(page, 'pairing-delayed-scheduler', { ...raw, passiveStart: before, observed, passiveFailures: inspectFrames(frames) });
  } finally { await owner.close(); }
});

for (const [width, height, scale] of [[640, 480, 1], [390, 844, 1.25]])
for (const mode of ['light', 'dark']) test(`compact Settings keeps passive browser feedback stable at ${width}x${height}/${scale} in ${mode}`, async () => {
  const owner = await context({ viewport: { width, height }, colorScheme: mode, reducedMotion: 'reduce' });
  const page = await owner.newPage();
  try {
    await settings(page, mode, scale);
    const section = page.locator('#browser-extension');
    const baseline = await section.boundingBox();
    const full = await page.locator('#full-analytics').boundingBox();
    if (width === 640) assert(baseline.height < 330, `Browser section still reserves excessive empty space: ${baseline.height}`);
    assert.equal(await section.locator('[data-reserved-region="browser-feedback"]').count(), 1);
    assert.equal(await section.locator('[data-reserved-region="pairing-result"], [data-reserved-region="pairing-links-feedback"]').count(), 0);
    await startPassive(page, ['settings-browser', 'browser-feedback']);
    for (const [method, state] of [
      ['browser', { capture: 'paused' }], ['browser', null], ['browser', { capture: 'active', site_access: 'needs_approval' }],
      ['connection', 'reconnecting'], ['connection', 'connected'],
    ]) {
      await push(page, method, state);
      await settle(page);
      assert.deepEqual(await section.boundingBox(), baseline, 'A passive browser update resized the compact section');
      assert.deepEqual(await page.locator('#full-analytics').boundingBox(), full, 'A passive update moved Full activation');
    }
    await page.getByRole('button', { name: 'Pause collecting', exact: true }).click();
    await push(page, 'resolve', 'browser.setCapture', 'unreachable');
    await settle(page);
    await push(page, 'companion', { creator_account_id: 'story-only-account', revision: 7, changed_at: '2026-09-29T10:00:00Z' });
    await page.waitForFunction(() => window.__workspaceFixture.pending().includes('pairing.pins'));
    await push(page, 'reject', 'pairing.pins', 'FixtureFailure');
    const retry = page.getByRole('button', { name: 'Retry checking connected extensions' });
    await retry.waitFor();
    await settle(page);
    const feedback = section.locator('[data-reserved-region="browser-feedback"]');
    assert(await textFits(feedback), 'Combined feedback escapes its fixed region');
    assert.deepEqual(await section.boundingBox(), baseline, 'Combined failures resize the section');
    const raw = await passiveReport(page);
    await record(page, `passive-${width}-${scale}-${mode}`, { baseline, full, ...raw });
    assert.deepEqual(raw.passiveFailures, []);
    const details = feedback.getByRole('button', { name: 'Show details' });
    const hasDetails = await details.count() > 0;
    if (hasDetails) await details.click();
    const message = await (hasDetails ? page.getByRole('dialog') : feedback).innerText();
    assert(message.includes('The browser extension is not connected right now.'));
    assert(message.includes("Connected extensions couldn't be checked."));
  } finally { await owner.close(); }
});

for (const width of [320, 390]) test(`shared browser feedback retains every message at ${width} with doubled text`, async () => {
  const owner = await context({ viewport: { width, height: 1000 }, reducedMotion: 'reduce' });
  const page = await owner.newPage();
  try {
    await settings(page, 'light', 2);
    await page.getByRole('button', { name: 'Pause collecting', exact: true }).click();
    await push(page, 'resolve', 'browser.setCapture', 'unreachable');
    await push(page, 'companion', { creator_account_id: 'story-only-account', revision: 8, changed_at: '2026-09-29T10:00:00Z' });
    await page.waitForFunction(() => window.__workspaceFixture.pending().includes('pairing.pins'));
    await push(page, 'reject', 'pairing.pins', 'FixtureFailure');
    const retry = page.getByRole('button', { name: 'Retry checking connected extensions' });
    await retry.waitFor();
    const feedback = page.locator('[data-reserved-region="browser-feedback"]');
    await feedback.scrollIntoViewIfNeeded();
    await settle(page);
    const fits = await textFits(feedback);
    const geometry = await feedback.boundingBox();
    const overflow = await page.locator('#browser-extension').evaluate(section => {
      const bounds = section.getBoundingClientRect();
      return [...section.querySelectorAll('*')].filter(node => getComputedStyle(node).visibility === 'visible' && node.getBoundingClientRect().right > bounds.right + 1)
        .map(node => ({ tag: node.tagName, text: node.textContent.slice(0, 90), rect: node.getBoundingClientRect().toJSON() }));
    });
    await record(page, `narrow-${width}`, { fits, geometry, overflow });
    assert.deepEqual(overflow, [], 'Browser controls must not escape the section');
    assert(fits, 'Long simultaneous feedback must fit or expose its complete details');
    assert(await textFits(retry), 'Retry must remain readable');
    await feedback.getByRole('button', { name: 'Show details' }).click();
    const dialog = page.getByRole('dialog');
    const message = await dialog.getByRole('alert').innerText();
    assert.equal(message, 'The browser extension is not connected right now. Connected extensions couldn\'t be checked.');
    await dialog.getByRole('button', { name: 'Close', exact: true }).click();
    await retry.focus();
    await retry.press('Enter');
    assert.deepEqual(await push(page, 'pending'), ['pairing.pins']);
  } finally { await owner.close(); }
});

for (const [width, scale] of [[640, 1], [390, 2]]) test(`shared footer preserves allowed pairing actions and passive retry at ${width} and ${scale}x text`, async () => {
  const owner = await context({ viewport: { width, height: 1000 }, reducedMotion: 'reduce' });
  const page = await owner.newPage();
  try {
    await settings(page, 'light', scale);
    await push(page, 'desktopSnapshot', { allowed_actions: ['browser.pair'] });
    await push(page, 'desktopActionError', 'FixtureFailure');
    await page.getByRole('button', { name: 'Connect another extension', exact: true }).click();
    const section = page.locator('#browser-extension');
    await section.locator('[data-reserved-region="browser-feedback"]').getByRole('button', { name: 'Show details' }).click();
    const entryDetails = page.getByRole('dialog');
    assert.equal(await entryDetails.getByRole('alert').innerText(), 'The browser connection request could not be opened.');
    await entryDetails.getByRole('button', { name: 'Close', exact: true }).click();
    await section.locator('[data-browser-feedback]').scrollIntoViewIfNeeded();
    await settle(page);
    const baseline = await section.boundingBox();
    await startPassive(page, ['settings-browser', 'browser-feedback']);
    await push(page, 'companion', { creator_account_id: 'story-only-account', revision: 9, changed_at: '2026-09-29T10:00:00Z' });
    await page.waitForFunction(() => window.__workspaceFixture.pending().includes('pairing.pins'));
    await push(page, 'reject', 'pairing.pins', 'FixtureFailure');
    const retry = page.getByRole('button', { name: 'Retry checking connected extensions' });
    await retry.waitFor();
    await settle(page);
    const connect = page.getByRole('button', { name: 'Connect extension', exact: true });
    const options = page.getByRole('button', { name: 'Connection options', exact: true });
    for (const control of [connect, options, retry]) assert(await textFits(control), 'Every explicit recovery action remains readable');
    assert.equal(await options.evaluate(node => {
      const range = document.createRange(); range.selectNodeContents(node);
      return new Set([...range.getClientRects()].filter(rect => rect.width > 0 && rect.height > 0).map(rect => rect.top)).size;
    }), 1, 'Options must stay on one line, never split inside the word');
    const feedback = section.locator('[data-reserved-region="browser-feedback"]');
    assert(await textFits(feedback));
    assert.deepEqual(await section.boundingBox(), baseline, 'Adding the separately permitted options and scoped retry must not resize the footer');
    const raw = await passiveReport(page);
    const browserIds = new Set(['settings-browser', ...await section.locator('[data-reserved-region]').evaluateAll(nodes => nodes.map(node => node.dataset.reservedRegion))]);
    // Below the native 640px minimum this added case qualifies browser containment,
    // not unrelated page regions. Keep their full raw overflow evidence; all region
    // movement and every native shift in the observation window still fail.
    const browserFailures = inspectFrames(raw.frames.map(frame => ({ ...frame,
      shifts: frame.shifts.filter(shift => shift.at >= raw.passiveWindow.start),
      regions: frame.regions.map(region => width < 640 && !browserIds.has(region.id) ? { ...region, overflow: false } : region),
    })));
    const outsideBrowserOverflow = raw.frames.flatMap(frame => frame.regions.filter(region => region.overflow && !browserIds.has(region.id)));
    await record(page, `actions-${width}`, { baseline, ...raw, browserFailures, outsideBrowserOverflow,
      containmentScope: width < 640 ? 'browser only; full raw page overflow retained; all movement and native shifts checked' : 'full page' });
    assert.deepEqual(browserFailures, []);
    await feedback.getByRole('button', { name: 'Show details' }).click();
    const dialog = page.getByRole('dialog');
    assert((await dialog.getByRole('alert').innerText()).includes('The browser connection request could not be opened.'));
    assert((await dialog.getByRole('alert').innerText()).includes("Connected extensions couldn't be checked."));
    await dialog.getByRole('button', { name: 'Close', exact: true }).click();
    await options.click();
    assert(await page.getByRole('button', { name: 'Compare connection codes', exact: true }).isEnabled());
    assert.deepEqual(await push(page, 'desktopActions'), [{ action: 'browser.pair' }]);
  } finally { await owner.close(); }
});

for (const mode of ['light', 'dark']) test(`stored-message settings precede their stable feedback in ${mode}`, async () => {
  const owner = await context({ viewport: { width: 640, height: 480 }, colorScheme: mode, reducedMotion: 'reduce' });
  const page = await owner.newPage();
  try {
    await settings(page, mode);
    const section = page.locator('[data-reserved-region="settings-vault"]');
    const archive = section.getByRole('heading', { name: 'Archive', exact: true });
    await archive.scrollIntoViewIfNeeded();
    await settle(page);
    const summary = await section.getByText('Saved only on this computer.', { exact: true }).boundingBox();
    const row = await archive.boundingBox();
    assert(row.y - (summary.y + summary.height) <= 44, 'An empty feedback lane still precedes Archive');
    const feedback = section.locator('[data-reserved-region="vault-feedback"]');
    const deletion = await section.getByRole('heading', { name: 'Delete messages', exact: true }).boundingBox();
    assert((await feedback.boundingBox()).y >= deletion.y + deletion.height);
    await section.getByRole('button', { name: 'Download messages', exact: true }).click();
    const baseline = await section.boundingBox();
    await startPassive(page, ['settings-vault', 'vault-feedback']);
    await push(page, 'reject', 'vault.exportDocument', 'FixtureFailure');
    await settle(page);
    assert.deepEqual(await section.boundingBox(), baseline, 'A vault error must not resize its section');
    assert(await textFits(feedback), 'Vault recovery must fit or offer complete details');
    const raw = await passiveReport(page);
    await record(page, `vault-${mode}`, raw);
    assert.deepEqual(raw.passiveFailures, []);
  } finally { await owner.close(); }
});

for (const mode of ['light', 'dark']) test(`Options scrollbars use readable theme tokens and native forced colors in ${mode}`, async () => {
  const owner = await context({ viewport: { width: 640, height: 480 }, colorScheme: mode, reducedMotion: 'reduce' });
  const page = await owner.newPage();
  try {
    await renderSurfaceState(page, SURFACE_STATES.connection);
    const read = () => page.evaluate(() => [document.documentElement, document.querySelector('.options-frame')].map(node => {
      const style = getComputedStyle(node), body = getComputedStyle(document.body);
      return { width: style.scrollbarWidth, color: style.scrollbarColor, thumb: getComputedStyle(node, '::-webkit-scrollbar-thumb').backgroundColor,
        paper: body.getPropertyValue('--dipsy-color-paper').trim(), backdrop: body.backgroundColor };
    }));
    const regular = await read();
    for (const scrollbar of regular) {
      assert.equal(scrollbar.width, 'thin');
      assert.notEqual(scrollbar.color, 'auto');
      assert(new Color(scrollbar.thumb).contrastWCAG21(new Color(scrollbar.paper)) >= 3, 'Idle thumb has insufficient separation from paper');
      assert(new Color(scrollbar.thumb).contrastWCAG21(new Color(scrollbar.backdrop)) >= 3, 'Idle thumb has insufficient separation from page');
    }
    await page.emulateMedia({ forcedColors: 'active' });
    const forced = await read();
    const nativeText = await page.evaluate(() => {
      const node = document.createElement('span'); node.style.color = 'ButtonText'; document.body.append(node);
      const color = getComputedStyle(node).color; node.remove(); return color;
    });
    for (const scrollbar of forced) {
      assert.equal(scrollbar.width, 'auto');
      assert.equal(scrollbar.color, 'auto');
      assert.equal(scrollbar.thumb, nativeText);
    }
    await record(page, `options-${mode}`, { regular, forced });
  } finally { await owner.close(); }
});
