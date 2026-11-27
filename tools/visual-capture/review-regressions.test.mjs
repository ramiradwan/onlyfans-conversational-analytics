import assert from 'node:assert/strict';
import { assertNumericTypography } from './appearance-contracts.mjs';
import { randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { after, before, test } from 'node:test';
import { chromium } from 'playwright';
import { FIXED_NOW, startHarness } from './capture.mjs';
import { auditScrolling, installWatcher, readWatcher } from './shift-watcher.mjs';
import { renderSurfaceState, SURFACE_STATES } from '../../extension/qualification/surface-fixtures.mjs';
import { openActivationReview, publishActivation } from './desktop-decision-fixtures.mjs';
import { measureDashboardMismatch, measureFindingsStates, measureOfferPrice } from './review-contracts.mjs';
import { prepareFindings } from './screen-matrix.mjs';

let browser, harness;
before(async () => { browser = await chromium.launch(); harness = await startHarness(); });
after(async () => { await browser?.close(); harness?.vite.kill(); });
const settle = (page) => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));

test('parallel visual harnesses use separate Vite dependency caches', async () => {
  const second = await startHarness();
  try {
    const dependencyPath = async ({ base }) => {
      const response = await fetch(new URL('/src/story-only/VisualHarness.tsx', base));
      assert.equal(response.status, 200);
      const match = (await response.text()).match(/"([^"]+\/deps\/react-dom_client\.js)\?/u);
      assert(match, 'The harness entry must import the optimized React dependency');
      return new URL(match[1], base).pathname;
    };
    const [firstPath, secondPath] = await Promise.all([dependencyPath(harness), dependencyPath(second)]);
    assert.notEqual(firstPath, secondPath, 'Concurrent Vite servers share an optimizer cache');
  } finally {
    second.vite.kill();
  }
});

for (const mode of ['light', 'dark']) for (const reducedMotion of ['reduce', 'no-preference']) {
  for (const [width, height] of [[1440, 900], [390, 844], [320, 900]]) {
    test(`first and seen findings copy fits at ${width} in ${mode} with motion ${reducedMotion}`, async () => {
      const viewport = { width, height };
      const page = await browser.newPage({ viewport, colorScheme: mode, reducedMotion, locale: 'en-US', timezoneId: 'UTC' });
      try {
        await prepareFindings(page, true);
        await measureFindingsStates(page, harness.base, mode, viewport);
      } finally { await page.close(); }
    });
  }
}

for (const mode of ['light', 'dark']) for (const scale of [1, 1.5]) {
  test(`native window controls and compact Settings fit 640x480 in ${mode} at ${scale}x text`, async () => {
    const context = await browser.newContext({ viewport: { width: 640, height: 480 }, colorScheme: mode,
      reducedMotion: 'reduce', deviceScaleFactor: scale });
    try {
      const frame = await readFile(new URL('../../desktop/window-frame.js', import.meta.url), 'utf8');
      await context.addInitScript({ content: 'window.__TAURI__ = {core: {invoke: async () => false}};\n' + frame });
      // Intercept the exact native origin without starting or using a local Brain.
      await context.route('http://bridge.localhost:17871/**', async route => {
        const url = new URL(route.request().url());
        const path = url.pathname === '/' ? '/visual-harness.html' : url.pathname;
        await route.fulfill({ response: await route.fetch({ url: new URL(path + url.search, harness.base).href }) });
      });
      const page = await context.newPage();
      await page.goto(`http://bridge.localhost:17871/?workspace=settings&state=populated&mode=${mode}`);
      await page.waitForFunction(() => window.__workspaceFixture);
      await page.evaluate(async factor => { await document.fonts.ready; document.documentElement.style.fontSize = `${16 * factor}px`; }, scale);
      await page.getByRole('heading', { name: 'Stored messages', exact: true }).waitFor();
      await page.waitForTimeout(250);
      const controls = await page.locator('desktop-window-controls button').evaluateAll(nodes => nodes.map(node => {
        const bounds = node.getBoundingClientRect();
        return { label: node.getAttribute('aria-label'), x: bounds.x, y: bounds.y, right: bounds.right, bottom: bounds.bottom };
      }));
      assert.deepEqual(controls.map(control => control.label), ['Minimize', 'Maximize', 'Close window']);
      assert(controls.every(control => control.x >= 0 && control.y >= 0 && control.right <= 640 && control.bottom <= 480));
      const measure = () => page.locator('#full-analytics').evaluate(node => node.getBoundingClientRect().y);
      const baseline = await measure();
      await page.evaluate(() => window.__workspaceFixture.browser({ capture: 'paused' }));
      await settle(page);
      assert.equal(await measure(), baseline, 'A capture-status push must not move the next section');
      await page.evaluate(() => window.__workspaceFixture.disconnected());
      await settle(page);
      assert.equal(await measure(), baseline, 'A connection-status push must not move the next section');
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    } finally { await context.close(); }
  });
}

for (const painted of [false, true]) test(`the region watcher ${painted ? 'rejects a painted' : 'ignores an unpainted'} intermediate layout`, async () => {
  const page = await browser.newPage({ viewport: { width: 390, height: 900 } });
  try {
    await installWatcher(page, { requiredRegions: ['task'] });
    await page.goto('data:text/html,<main data-reserved-region="task" style="width:280px"><div data-region-content>Setup</div></main>');
    await settle(page);
    await page.evaluate(async painted => {
      const task = document.querySelector('main');
      task.style.marginTop = '20px';
      task.firstElementChild.style.width = '600px';
      if (painted) await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      task.style.marginTop = '';
      task.firstElementChild.style.width = '';
    }, painted);
    await settle(page);
    const { failures } = await readWatcher(page);
    if (painted) {
      assert(failures.includes('task: moved or resized'), 'Movement visible for a browser frame must be caught');
      assert(failures.includes('task: overflow'), 'Overflow visible for a browser frame must be caught');
    } else assert.deepEqual(failures, []);
  } finally { await page.close(); }
});

for (const width of [320, 390, 1440]) for (const fontScale of [1, 2]) for (const group of [
  { requirement: 'activation', states: ['activation_checking', 'activation_required', 'activation_unavailable', 'activation_checking'] },
  { requirement: 'pairing', states: ['pairing_waiting', 'pairing_failed', 'pairing_waiting'] },
  { requirement: 'desktop-connection', states: ['desktop_app_needed', 'desktop_app_unavailable', 'full_disconnected', 'desktop_app_needed'] },
]) test(`passive ${group.requirement} updates preserve setup at ${width} and ${fontScale}x text`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce' });
  try {
    await installWatcher(page, { requiredRegions: ['extension-stage', 'surface-feedback'], taskTransitions: true });
    await page.addInitScript(fontScale => { new MutationObserver(() => {
      if (document.documentElement && !document.documentElement.style.fontSize) document.documentElement.style.fontSize = `${16 * fontScale}px`;
    }).observe(document, { childList: true, subtree: true }); }, fontScale);
    await renderSurfaceState(page, SURFACE_STATES[group.states[0]]); await settle(page);
    if (group.requirement === 'desktop-connection') await page.evaluate(() => {
      const send = window.chrome.runtime.sendMessage;
      window.chrome.runtime.sendMessage = async message => {
        const response = await send(message);
        if (message.type === 'ofca.ui.status' && window.__surfaceFixture.state.transport === 'disconnected') response.status.delivery.transport_state = 'disconnected';
        return response;
      };
    });
    await page.locator('#journey-title').focus();
    const focus = await page.evaluate(() => document.activeElement.id);
    for (const name of group.states.slice(1)) {
      await page.evaluate(state => window.__surfaceFixture.change(state, true), name === 'full_disconnected'
        ? { ...SURFACE_STATES.full_unavailable, transport: 'disconnected' } : SURFACE_STATES[name]);
      await settle(page);
      assert.equal(await page.locator('main').getAttribute('data-requirement'), 'desktop-handoff');
      assert.equal(await page.evaluate(() => document.activeElement.id), focus, 'A passive status change cannot steal focus');
    }
    assert.deepEqual((await readWatcher(page)).failures, []);
  } finally { await page.close(); }
});

for (const width of [320, 1280]) for (const [state, review] of [
  [{ ...SURFACE_STATES.software_activation, fullIntent: true, browserTask: true }, 'initial review'],
  [SURFACE_STATES.full_review, 'Preview upgrade'],
]) test(`approved copy sections sit under the ${review} title at ${width}`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 900 } });
  try {
    await renderSurfaceState(page, state);
    const levels = await page.evaluate(() => {
      const size = node => parseFloat(getComputedStyle(node).fontSize);
      const top = node => node.getBoundingClientRect().top, bottom = node => node.getBoundingClientRect().bottom;
      const heading = document.getElementById('activation-title');
      const sections = [...heading.closest('section').querySelectorAll('.points > h2')]
        .filter(node => node !== heading && node.getClientRects().length);
      const copy = sections[0].nextElementSibling;
      return { title: size(heading), labels: sections.map(node => node.textContent), sizes: sections.map(size),
        paragraph: top(copy.nextElementSibling) - bottom(copy),
        below: sections.map(node => top(node.nextElementSibling) - bottom(node)),
        above: sections.slice(1).map((node, index) => top(node) - bottom(sections[index].parentElement.lastElementChild)) };
    });
    assert.deepEqual(levels.labels, ['Full', 'Separate choices']);
    assert(levels.sizes.every(size => size < levels.title), 'Approved copy headings must sit under the title');
    assert(levels.below.every(gap => gap < levels.paragraph), 'A section heading must sit closer to its copy than paragraphs do');
    assert(levels.above.length && levels.above.every(gap => gap > levels.paragraph), 'A new section must start further away than the next paragraph');
  } finally { await page.close(); }
});

const popupBoxes = (page, selectors) => page.evaluate((selectors) => selectors.map((selector) => {
  const { top, bottom } = document.querySelector(selector).getBoundingClientRect();
  return `${selector} ${top + scrollY}-${bottom + scrollY}`;
}), selectors);

for (const width of [320, 390]) for (const fontScale of [1, 2]) test(`popup sections stack in one page scroll at ${width} and ${fontScale}x text`, async () => {
  for (const [name, state] of Object.entries(SURFACE_STATES).filter(([, state]) => state.surface === 'popup')) {
    const page = await browser.newPage({ viewport: { width, height: 600 }, reducedMotion: 'reduce' });
    try {
      await page.addInitScript((scale) => { new MutationObserver(() => { if (document.documentElement && !document.documentElement.style.fontSize) document.documentElement.style.fontSize = (16 * scale) + 'px'; }).observe(document, { childList: true, subtree: true }); }, fontScale);
      await renderSurfaceState(page, state);
      await settle(page);
      const layout = await page.evaluate(() => {
        const nested = [...document.querySelectorAll('body *')].filter((node) => {
          if (!/(auto|scroll)/.test(getComputedStyle(node).overflowY)) return false;
          const start = node.scrollTop;
          node.scrollTop = start + 40;
          const moved = node.scrollTop !== start;
          node.scrollTop = start;
          return moved;
        }).map((node) => node.id || node.getAttribute('class') || node.localName);
        const sections = [...document.querySelectorAll('.popup-content > [data-region-content] > *, nav.menu, footer')]
          .filter((node) => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden' && !['absolute', 'fixed'].includes(getComputedStyle(node).position))
          .map((node) => ({ name: node.id || node.dataset.reservedRegion || node.localName, top: node.getBoundingClientRect().top, bottom: node.getBoundingClientRect().bottom }));
        const overlaps = sections.slice(1).flatMap((section, index) => section.top < sections[index].bottom - 0.5 ? [`${sections[index].name} and ${section.name}`] : []);
        const action = document.querySelector('#journey-primary');
        return { nested, overlaps, spill: action.getBoundingClientRect().bottom - action.parentElement.getBoundingClientRect().bottom };
      });
      assert.deepEqual(layout.nested, [], `The ${name} popup scrolls inside the page`);
      assert.deepEqual(layout.overlaps, [], `The ${name} popup overlaps its sections`);
      assert(layout.spill <= 0.5, `The ${name} popup's status action extends ${layout.spill}px past its slot`);
    } finally { await page.close(); }
  }
});

const popupLayout = (page, selectors) => page.evaluate((selectors) => {
  const edges = (node) => { const { top, bottom } = node.getBoundingClientRect(); return [top + scrollY, bottom + scrollY]; };
  const action = document.querySelector('#journey-primary');
  return { action: action.getBoundingClientRect().height, below: edges(action)[1], boxes: selectors.map((selector) => edges(document.querySelector(selector))) };
}, selectors);
// A status label that wraps differently moves what follows it by its own change in height, and nothing else moves.
const assertPopupHeld = (current, initial, message) => {
  const round = (value) => Math.round(value * 100) / 100;
  const change = current.action - initial.action;
  assert.deepEqual(current.boxes.map((box) => box.map(round)),
    initial.boxes.map((box) => box.map((edge) => round(edge >= initial.below - 0.5 ? edge + change : edge))), message);
};

for (const width of [320, 390]) for (const fontScale of [1, 2]) test(`pausing and resuming Preview keep the popup in place at ${width} and ${fontScale}x text`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 600 }, reducedMotion: 'reduce' });
  try {
    await page.addInitScript((scale) => { new MutationObserver(() => { if (document.documentElement && !document.documentElement.style.fontSize) document.documentElement.style.fontSize = (16 * scale) + 'px'; }).observe(document, { childList: true, subtree: true }); }, fontScale);
    await renderSurfaceState(page, SURFACE_STATES.preview);
    await settle(page);
    const selectors = ['#journey-card', '#preview-metrics', '[data-reserved-region="surface-feedback"]', 'nav.menu', 'footer'];
    const initial = await popupLayout(page, selectors);
    await page.locator('#pause').focus();
    await page.keyboard.press('Enter');
    await page.locator('main[aria-busy="false"]').waitFor({ state: 'attached' });
    await page.locator('#pause').waitFor({ state: 'hidden' });
    await settle(page);
    assertPopupHeld(await popupLayout(page, selectors), initial, 'Pausing moves the popup');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'journey-title', 'Pausing must move focus to the status heading');
    await page.locator('#journey-primary').click();
    await page.locator('main[aria-busy="false"]').waitFor({ state: 'attached' });
    await page.locator('#pause').waitFor();
    await settle(page);
    assertPopupHeld(await popupLayout(page, selectors), initial, 'Resuming moves the popup');
  } finally { await page.close(); }
});

for (const width of [320, 390]) test(`the desktop control note takes the Pause slot at ${width}`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 600 }, reducedMotion: 'reduce' });
  try {
    await renderSurfaceState(page, SURFACE_STATES.full_ready);
    await settle(page);
    const selectors = ['#journey-card', '#ready-details', '[data-reserved-region="surface-feedback"]', 'nav.menu', 'footer'];
    const initial = await popupBoxes(page, selectors);
    await page.evaluate((state) => window.__surfaceFixture.change(state, true), SURFACE_STATES.desktop_controlled);
    await page.locator('#desktop-control-note').waitFor();
    await settle(page);
    assert.deepEqual(await popupBoxes(page, selectors), initial, 'Desktop control moves the popup');
    await page.evaluate((state) => window.__surfaceFixture.change(state, true), SURFACE_STATES.full_ready);
    await page.locator('#pause').waitFor();
    await settle(page);
    assert.deepEqual(await popupBoxes(page, selectors), initial, 'Releasing desktop control moves the popup');
  } finally { await page.close(); }
});

for (const width of [390, 1440]) {
  for (const content of ['unseen', 'wide']) test(`feedback contains ${content} text at ${width}`, async () => {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    try {
      await installWatcher(page);
      await page.goto(`${harness.base}?stability=notice&mode=light`);
      const text = content === 'wide' ? '界'.repeat(width === 390 ? 60 : 160) : `${randomUUID()}${'x'.repeat(4096)} 界文字 `.repeat(2);
      await page.evaluate((detail) => window.dispatchEvent(new CustomEvent('notice-fixture', { detail })), text);
      await settle(page);
      assert.deepEqual((await readWatcher(page)).failures, []);
      assert.equal(await page.getByRole('button', { name: 'Show details', exact: true }).count(), 2);
      await page.getByRole('button', { name: 'Show details', exact: true }).first().click();
      assert((await page.getByRole('dialog').textContent()).includes(text));
    } finally { await page.close(); }
  });
}

test('watcher ignores transparent decorative text but detects the same clipping when visible', async () => {
  const page = await browser.newPage();
  try {
    await installWatcher(page, { requiredRegions: ['slot'] });
    await page.route('**/*', route => route.fulfill({ contentType: 'text/html', body: '<div data-reserved-region="slot" data-region-role="scroll" style="height:100px;width:200px;overflow:auto"><span data-region-content style="display:block;width:12px;overflow:hidden;white-space:nowrap;opacity:0">Decorative label</span></div>' }));
    await page.goto('http://watcher-fixture.localhost/');
    await settle(page);
    assert.deepEqual((await readWatcher(page)).failures, []);
    await page.evaluate(() => { document.querySelector('span').style.opacity = '1'; });
    await settle(page);
    assert((await readWatcher(page)).failures.some(failure => failure.includes('overflow')));
  } finally { await page.close(); }
});

for (const attack of ['horizontal reading overflow', 'clipped reading text', 'unreachable reading action', 'overlapping reading actions', 'late reservation', 'unmarked neighbor']) test(`watcher rejects ${attack}`, async () => {
  const page = await browser.newPage();
  try {
    await installWatcher(page, { requiredRegions: ['slot'] });
    await page.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: `<main><div id="slot" ${attack === 'late reservation' ? '' : 'data-reserved-region="slot"'} data-region-role="scroll" style="height:100px;width:200px;overflow:auto"><span data-region-content>Short text</span></div><button>Neighbor</button></main>` }));
    await page.goto('http://watcher-fixture.localhost/');
    await settle(page);
    await page.evaluate((attack) => {
      if (attack === 'horizontal reading overflow') document.querySelector('span').style.cssText = 'display:block;width:2000px';
      if (attack === 'clipped reading text') document.querySelector('span').style.cssText = 'display:block;width:12px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis';
      if (attack === 'unreachable reading action') { const button = document.createElement('button'); button.textContent = 'Action'; button.style.cssText = 'position:relative;top:-200px'; document.querySelector('#slot').append(button); }
      if (attack === 'overlapping reading actions') {
        const slot = document.querySelector('#slot'); slot.style.position = 'relative';
        for (let index = 0; index < 2; index++) { const button = document.createElement('button'); button.textContent = 'Action'; button.style.cssText = 'position:absolute;top:30px;left:0'; slot.append(button); }
      }
      if (attack === 'late reservation') document.querySelector('#slot').dataset.reservedRegion = 'slot';
      if (attack === 'unmarked neighbor') document.querySelector('button').style.transform = 'translateX(0.5px)';
    }, attack);
    await settle(page);
    if (attack === 'unmarked neighbor') { await page.evaluate(() => { document.querySelector('button').style.transform = ''; }); await settle(page); }
    assert((await readWatcher(page)).failures.length, 'Invalid geometry was accepted');
  } finally { await page.close(); }
});

for (const layer of ['menu', 'plain']) test(`watcher ${layer === 'menu' ? 'treats a closed menu as an overlay' : 'flags a removed neighbor layer'}`, async () => {
  const page = await browser.newPage();
  try {
    await installWatcher(page, { requiredRegions: ['slot'] });
    await page.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: '<main><div data-reserved-region="slot" style="height:100px;width:200px"><span data-region-content>Short text</span></div></main>' }));
    await page.goto('http://watcher-fixture.localhost/');
    await settle(page);
    await page.evaluate((layer) => {
      const node = document.createElement('div');
      node.id = 'layer';
      node.style.cssText = 'position:fixed;inset:0';
      node.innerHTML = layer === 'menu' ? '<ul role="menu"><li role="menuitem">Disconnect</li></ul>' : '<p>Layer</p>';
      document.body.append(node);
    }, layer);
    await settle(page);
    await page.evaluate(() => document.querySelector('#layer').remove());
    await settle(page);
    const { failures } = await readWatcher(page);
    if (layer === 'menu') assert.deepEqual(failures, []);
    else assert(failures.some((failure) => failure.endsWith(': removed')), 'A removed neighbor must be flagged: ' + JSON.stringify(failures));
  } finally { await page.close(); }
});

test('reading audit follows the document scroll owner', async () => {
  const page = await browser.newPage({ viewport: { width: 390, height: 600 } });
  try {
    await page.setContent('<!doctype html><style>body{max-height:600px;overflow-y:auto;margin:0}main{height:800px}</style><main>Reading content</main>');
    const reports = await auditScrolling(page);
    assert(reports.length);
    assert(reports.every((entry) => entry.positions.at(-1).endReached));
  } finally { await page.close(); }
});

for (const state of ['loading', 'fresh', 'syncing', 'populated']) test('numeric baselines follow inline glyphs during ' + state, async () => {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  try {
    await page.goto(harness.base + '?workspace=home&state=' + state);
    if (state === 'fresh') {
      await page.getByRole('heading', { name: 'Set up your browser to see your message counts', exact: true }).waitFor();
      assert.equal(await page.locator('[data-visual="conversation-total"]').count(), 0);
      await page.evaluate(() => window.__workspaceFixture.snapshot('populated'));
    }
    await page.locator('[data-visual="conversation-total"]').waitFor();
    await page.evaluate(() => document.fonts.ready);
    await assertNumericTypography(page, { width: 1440 });
    await page.locator('[data-visual="message-total"]').evaluate((element) => { element.style.transform = 'translateY(2px)'; });
    await assert.rejects(() => assertNumericTypography(page, { width: 1440 }), /baselines diverged/);
  } finally { await page.close(); }
});

for (const width of [390, 1440]) for (const fontScale of [1, 1.25]) test(`activation status retains its geometry on a showing page at ${width} with scale ${fontScale}`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce' });
  try {
    await page.addInitScript((scale) => { new MutationObserver(() => { if (document.documentElement && !document.documentElement.style.fontSize) document.documentElement.style.fontSize = (16 * scale) + 'px'; }).observe(document, { childList: true, subtree: true }); }, fontScale);
    await page.goto(harness.base + '?workspace=settings&state=loading&transitions=1');
    await page.waitForFunction(() => window.__workspaceFixture);
    const push = (method, ...args) => page.evaluate(({ method, args }) => window.__workspaceFixture[method](...args), { method, args });
    await push('snapshot', 'populated');
    await push('connection', 'connected');
    for (const key of await push('pending')) await push('release', key, 'populated');
    const section = page.locator('[data-reserved-region="settings-activation"]');
    const chip = section.locator('[aria-live="polite"] .MuiChip-outlined');
    await chip.waitFor();
    await page.evaluate(() => document.fonts.ready);
    await settle(page);
    let initial, initialChip;
    for (const [commercial_authority, analysis_admission, label] of [
      ['active', 'admitted', 'On'], ['active', 'blocked', 'Needs attention'], ['required', 'blocked', 'Off'],
      ['unavailable', 'blocked', null], ['active', 'admitted', 'On'],
    ]) {
      await push('refresh');
      await settle(page);
      for (const key of await push('pending')) await push('release', key, 'populated', key === 'onboarding.readiness'
        ? { schema: 'ofca-analysis-readiness/v1', commercial_authority, analysis_admission } : undefined);
      await settle(page);
      const geometry = await section.getByRole('heading', { name: 'Full analytics', exact: true }).boundingBox();
      initial ??= geometry;
      assert.deepEqual(geometry, initial, 'Status changes move the persistent heading');
      const bounds = await section.boundingBox();
      const content = await section.getByRole('status').or(section.getByRole('alert')).or(section.getByRole('button')).all();
      assert(content.length > 0, 'The section needs visible status or an available action');
      for (const row of content) {
        const summary = await row.boundingBox();
        assert(summary.y >= bounds.y && summary.y + summary.height <= bounds.y + bounds.height, 'Content escapes the section');
      }
      if (label) {
        assert.equal(await chip.innerText(), label);
        const { x, y, width, height } = await chip.boundingBox();
        assert(x >= bounds.x && x + width <= bounds.x + bounds.width, 'The pill escapes the section');
        assert(Math.abs(bounds.x + bounds.width - x - width - 24) < 1, 'The pill must align to the card inset');
        assert(y < geometry.y + geometry.height && y + height > geometry.y, 'The pill must share the heading row');
        initialChip ??= { y, height };
        assert.deepEqual({ y, height }, initialChip, 'Status pills change vertical geometry');
      } else assert.equal(await chip.count(), 0);
    }
  } finally { await page.close(); }
});

for (const width of [320, 390, 1440]) for (const fontScale of [1, 2]) test(`recent conversations show four rows without scrolling at ${width} and ${fontScale}x text`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce' });
  try {
    await page.addInitScript((scale) => { new MutationObserver(() => { if (document.documentElement && !document.documentElement.style.fontSize) document.documentElement.style.fontSize = (16 * scale) + 'px'; }).observe(document, { childList: true, subtree: true }); }, fontScale);
    await page.clock.setFixedTime(FIXED_NOW);
    await page.goto(harness.base + '?workspace=home&state=populated');
    const region = page.locator('[data-reserved-region="dashboard-recent"]');
    await region.locator('li').nth(3).waitFor();
    await page.evaluate(() => document.fonts.ready);
    await settle(page);
    assert.equal(await region.locator('li').count(), 4);
    const { content, client } = await region.evaluate((node) => ({ content: node.scrollHeight, client: node.clientHeight }));
    assert(content <= client, `Recent conversations scroll by ${content - client}px`);
  } finally { await page.close(); }
});

for (const [times, now] of Object.entries({ today: FIXED_NOW, 'same-year': '2026-07-01T12:05:00.000Z', 'earlier-year': '2027-01-15T12:05:00.000Z' })) for (const width of [320, 390]) for (const fontScale of [1, 1.25, 2]) test(`recent conversation rows keep names, times and previews apart at ${width} and ${fontScale}x text with ${times} times`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce', timezoneId: 'UTC' });
  try {
    await page.addInitScript((scale) => { new MutationObserver(() => { if (document.documentElement && !document.documentElement.style.fontSize) document.documentElement.style.fontSize = (16 * scale) + 'px'; }).observe(document, { childList: true, subtree: true }); }, fontScale);
    await page.clock.setFixedTime(now);
    await page.goto(harness.base + '?workspace=home&state=populated');
    const region = page.locator('[data-reserved-region="dashboard-recent"]');
    await region.locator('li').nth(3).waitFor();
    await page.evaluate(() => document.fonts.ready);
    await settle(page);
    assert.equal(await region.locator('li').count(), 4);
    const failures = await region.evaluate((node) => {
      const rects = (target) => { const range = document.createRange(); range.selectNodeContents(target); return [...range.getClientRects()].filter((rect) => rect.width > 0 && rect.height > 0); };
      const card = node.querySelector('section').getBoundingClientRect();
      const inside = (rect) => rect.left >= card.left - 0.5 && rect.right <= card.right + 0.5;
      const hit = (a, b) => a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5;
      const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
      const failures = [];
      const link = [...node.querySelectorAll('a')].find((element) => element.textContent.trim() === 'Open inbox');
      const label = [...link.childNodes].find((child) => child.nodeType === Node.TEXT_NODE && child.textContent.trim());
      const lines = new Set(rects(label).map((rect) => Math.round(rect.top))).size;
      if (lines !== 1) failures.push(`Open inbox takes ${lines} lines`);
      if (!inside(link.getBoundingClientRect())) failures.push('Open inbox leaves the card');
      for (const item of node.querySelectorAll('li')) {
        const [name, preview] = item.querySelectorAll('[data-region-content]');
        const badge = item.querySelector('span[aria-hidden="true"]')?.parentElement;
        const parts = Object.entries({ name: rects(name), time: rects(item.querySelector('time')), preview: rects(preview), badge: badge ? [badge.getBoundingClientRect()] : [] });
        parts.forEach(([first, a], index) => parts.slice(index + 1).forEach(([second, b]) => {
          if (a.some((p) => b.some((q) => hit(p, q)))) failures.push(`${name.textContent}: ${first} overlaps ${second}`);
        }));
        if (parts.some(([, list]) => list.some((rect) => !inside(rect)))) failures.push(`${name.textContent}: text leaves the card`);
        const column = name.parentElement.getBoundingClientRect();
        const overrun = Math.max(...rects(name).map((rect) => rect.right)) - column.right;
        if (overrun > 0.5) failures.push(`${name.textContent}: name runs ${Math.round(overrun)}px past its column`);
        const space = Math.min(8 * rem, item.getBoundingClientRect().right - parseFloat(getComputedStyle(item).paddingRight) - column.left);
        if (column.width < space - 0.5) failures.push(`${name.textContent}: name keeps ${Math.round(column.width)}px of ${Math.round(space)}px`);
      }
      return failures;
    });
    assert.deepEqual(failures, []);
  } finally { await page.close(); }
});

for (const fontScale of [1, 2]) test(`inbox pane headers share one height and title line at ${fontScale}x text`, async () => {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' });
  try {
    await page.addInitScript((scale) => { new MutationObserver(() => { if (document.documentElement && !document.documentElement.style.fontSize) document.documentElement.style.fontSize = (16 * scale) + 'px'; }).observe(document, { childList: true, subtree: true }); }, fontScale);
    await page.clock.setFixedTime(FIXED_NOW);
    await page.goto(harness.base + '?workspace=inbox&state=populated');
    await page.locator('#message-stream-title').waitFor();
    await page.evaluate(() => document.fonts.ready);
    await settle(page);
    const [list, stream] = await page.evaluate(() => ['conversations-title', 'message-stream-title'].map((id) => {
      const title = document.getElementById(id);
      const header = title.closest('[data-reserved-region]').firstElementChild.getBoundingClientRect();
      const text = document.createRange();
      text.selectNodeContents(title);
      return { height: header.height, bottom: header.bottom, line: text.getClientRects()[0].top };
    }));
    assert(Math.abs(list.height - stream.height) < 0.5, `Pane headers are ${list.height}px and ${stream.height}px tall`);
    assert(Math.abs(list.bottom - stream.bottom) < 0.5, 'Pane header dividers do not line up');
    assert(Math.abs(list.line - stream.line) < 0.5, `Pane titles sit ${stream.line - list.line}px apart`);
  } finally { await page.close(); }
});

for (const [times, now] of Object.entries({ today: FIXED_NOW, 'earlier-year': '2027-01-15T12:05:00.000Z' })) for (const width of [320, 390]) for (const fontScale of [1.25, 2]) test(`inbox rows keep names, times and previews inside narrow panes at ${width} and ${fontScale}x text with ${times} times`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce', timezoneId: 'UTC' });
  try {
    await page.addInitScript((scale) => { new MutationObserver(() => { if (document.documentElement && !document.documentElement.style.fontSize) document.documentElement.style.fontSize = (16 * scale) + 'px'; }).observe(document, { childList: true, subtree: true }); }, fontScale);
    await page.clock.setFixedTime(now);
    await page.goto(harness.base + '?workspace=inbox&state=populated');
    const pane = page.locator('[data-reserved-region="chat-list"]');
    await pane.locator('li').nth(4).waitFor();
    await page.evaluate(() => document.fonts.ready);
    await settle(page);
    const failures = await pane.evaluate((node) => {
      const rects = (target) => { const range = document.createRange(); range.selectNodeContents(target); return [...range.getClientRects()].filter((rect) => rect.width > 0 && rect.height > 0); };
      const bounds = node.getBoundingClientRect();
      const inside = (rect) => rect.left >= bounds.left - 0.5 && rect.right <= bounds.right + 0.5;
      const hit = (a, b) => a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5;
      const failures = [];
      if (bounds.left < -0.5 || bounds.right > innerWidth + 0.5) failures.push('pane leaves the viewport');
      for (const item of node.querySelectorAll('li')) {
        const [name, preview] = item.querySelectorAll('[data-region-content]');
        const time = item.querySelector('time');
        if (!name?.textContent?.trim() || !preview?.textContent?.trim() || !time?.textContent?.trim()) failures.push('row text is missing');
        const parts = Object.entries({ name: rects(name), time: rects(time), preview: rects(preview) });
        for (const [label, list] of parts) if (list.some((rect) => !inside(rect))) failures.push(`${label} leaves the pane`);
        parts.forEach(([first, a], index) => parts.slice(index + 1).forEach(([second, b]) => {
          if (a.some((p) => b.some((q) => hit(p, q)))) failures.push(`${first} overlaps ${second}`);
        }));
      }
      return failures;
    });
    assert.deepEqual(failures, []);
    const stamps = await pane.locator('time').allTextContents();
    assert(stamps.every((stamp) => times === 'today' ? /^\d{2}:\d{2}$/.test(stamp) : stamp.includes('2026')));
  } finally { await page.close(); }
});

for (const width of [320, 600, 900, 1440]) for (const fontScale of [1, 2]) test(`analytics results fit their region without scrolling at ${width} and ${fontScale}x text`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce' });
  try {
    await page.addInitScript((scale) => { new MutationObserver(() => { if (document.documentElement && !document.documentElement.style.fontSize) document.documentElement.style.fontSize = (16 * scale) + 'px'; }).observe(document, { childList: true, subtree: true }); }, fontScale);
    await page.clock.setFixedTime(FIXED_NOW);
    await page.goto(harness.base + '?workspace=analytics&state=model');
    const region = page.locator('[data-reserved-region="analytics-content"]');
    await region.locator('[data-reserved-region="topics-list"]').waitFor();
    await page.evaluate(() => document.fonts.ready);
    await settle(page);
    const { content, client } = await region.evaluate((node) => ({ content: node.scrollHeight, client: node.clientHeight }));
    assert(content <= client, `Analytics results scroll by ${content - client}px`);
  } finally { await page.close(); }
});

for (const width of [390, 600, 900, 1440]) for (const fontScale of [1, 1.1, 1.25, 1.5, 2]) test(`a scrolling topics list shows part of its next row at ${width} and ${fontScale}x text`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce' });
  try {
    await page.addInitScript((scale) => { new MutationObserver(() => { if (document.documentElement && !document.documentElement.style.fontSize) document.documentElement.style.fontSize = (16 * scale) + 'px'; }).observe(document, { childList: true, subtree: true }); }, fontScale);
    await page.clock.setFixedTime(FIXED_NOW);
    await page.goto(harness.base + '?workspace=analytics&state=model');
    const region = page.locator('[data-reserved-region="topics-list"]');
    await region.waitFor();
    await page.evaluate(() => document.fonts.ready);
    await settle(page);
    const cut = await region.evaluate((node) => {
      const bottom = node.getBoundingClientRect().top + node.clientHeight;
      const row = [...node.querySelectorAll('li, tbody tr')].map((item) => item.getBoundingClientRect()).find((rect) => rect.height > 0 && rect.bottom > bottom + 0.5);
      return row ? (bottom - row.top) / row.height : null;
    });
    if (cut !== null) assert(cut >= 0.2 && cut <= 0.8, `The topics list cuts a row at ${Math.round(cut * 100)}% of its height`);
  } finally { await page.close(); }
});

for (const width of [320, 600, 700, 900, 1440]) for (const fontScale of [1, 1.25, 2]) test(`topics fit their panel without scrolling sideways at ${width} and ${fontScale}x text`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce' });
  try {
    await page.addInitScript((scale) => { new MutationObserver(() => { if (document.documentElement && !document.documentElement.style.fontSize) document.documentElement.style.fontSize = (16 * scale) + 'px'; }).observe(document, { childList: true, subtree: true }); }, fontScale);
    await page.clock.setFixedTime(FIXED_NOW);
    await page.goto(harness.base + '?workspace=analytics&state=model');
    const region = page.locator('[data-reserved-region="topics-list"]');
    await region.waitFor();
    await page.evaluate(() => document.fonts.ready);
    await settle(page);
    const { shown, sideways, spills } = await region.evaluate((node) => {
      const layouts = [...node.querySelectorAll(':scope > [data-region-content]')].filter((item) => getComputedStyle(item).display !== 'none');
      const sideways = [node, ...layouts, ...layouts.flatMap((item) => [...item.querySelectorAll('table')])]
        .filter((item) => item.scrollWidth > item.clientWidth).map((item) => `${item.tagName.toLowerCase()} by ${item.scrollWidth - item.clientWidth}px`);
      const spills = new Set();
      for (const layout of layouts) {
        const walker = document.createTreeWalker(layout, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
          const text = walker.currentNode;
          const cell = text.parentElement.closest('th, td, li');
          if (!text.textContent.trim() || !cell) continue;
          const box = cell.getBoundingClientRect(), style = getComputedStyle(cell);
          const left = box.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft);
          const right = box.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight);
          const range = document.createRange();
          range.selectNodeContents(text);
          if ([...range.getClientRects()].some((rect) => rect.left < left - 0.5 || rect.right > right + 0.5)) spills.add(text.textContent.trim());
        }
      }
      return { shown: layouts.length, sideways, spills: [...spills] };
    });
    assert.equal(shown, 1, `The topics region shows ${shown} layouts`);
    assert.deepEqual(sideways, [], 'Topics scroll sideways');
    assert.deepEqual(spills, [], 'Topic text spills out of its cell');
  } finally { await page.close(); }
});

for (const width of [320, 390]) test(`combined browser recovery follows its controls at ${width} with doubled text`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 1000 }, reducedMotion: 'reduce' });
  try {
    await page.goto(harness.base + '?workspace=settings&state=loading&transitions=1&mode=light');
    await page.waitForFunction(() => window.__workspaceFixture);
    const push = (method, ...args) => page.evaluate(({ method, args }) => window.__workspaceFixture[method](...args), { method, args });
    await push('snapshot', 'populated');
    await push('connection', 'connected');
    for (const key of await push('pending')) await push('release', key, 'populated');
    await push('browser', { capture: 'active', site_access: 'needs_approval', history_permission: 'missing' });
    await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
    await page.getByRole('switch', { name: 'Collect new messages', exact: true }).click();
    await push('resolve', 'browser.setCapture', 'unreachable');
    const feedback = page.locator('[data-reserved-region="browser-feedback"]');
    await feedback.locator('[data-region-content]').waitFor({ state: 'visible' });
    await page.evaluate(() => document.fonts.ready);
    await settle(page);
    await feedback.scrollIntoViewIfNeeded();
    assert(await page.getByRole('button', { name: 'Allow in extension', exact: true }).first().isVisible());
    // The connection facts sit in the section header as its status, beside its options menu.
    const header = page.locator('#browser-extension [data-settings-section-header] [data-settings-status]').first();
    assert(await header.isVisible(), 'The connection status must stay visible');
    assert((await header.innerText()).trim(), 'The connection status must name the connection');
    const geometry = await feedback.evaluate((node) => {
      const bounds = node.getBoundingClientRect();
      const section = node.closest('[data-journey-state="desktop.extension_pairing"]');
      const facts = section.querySelector('[data-reserved-region="browser-facts"]');
      const textBoxes = (element) => {
        const boxes = [];
        const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
          const text = walker.currentNode;
          if (!text.textContent.trim() || getComputedStyle(text.parentElement).visibility !== 'visible') continue;
          const range = document.createRange(); range.selectNodeContents(text);
          boxes.push(...range.getClientRects());
        }
        return boxes;
      };
      const contains = (outer, inner) => inner.left >= outer.left - 1 && inner.right <= outer.right + 1 && inner.top >= outer.top - 1 && inner.bottom <= outer.bottom + 1;
      const controls = facts.querySelector('[data-browser-controls="available"]');
      const rows = [...controls.children].slice(0, 2);
      return {
        instructionsBefore: rows.every((element) => textBoxes(element).every((box) => box.bottom <= bounds.top + 1)),
        rowsFit: rows.every((row) => textBoxes(row).every((box) => contains(row.getBoundingClientRect(), box))),
        guidanceBottom: rows.at(-1).getBoundingClientRect().bottom,
        feedback: bounds.toJSON(),
        feedbackFits: textBoxes(node).every((box) => contains(bounds, box)),
        sectionBottom: section.getBoundingClientRect().bottom,
        horizontalOverflow: section.scrollWidth > section.clientWidth,
      };
    });
    assert(geometry.instructionsBefore && geometry.rowsFit, 'Browser instructions and control labels must fit their rows and finish before recovery');
    assert(geometry.feedbackFits && !geometry.horizontalOverflow, 'All recovery text must remain visible without horizontal scrolling: ' + JSON.stringify(geometry));
    assert(geometry.guidanceBottom <= geometry.feedback.top, 'Browser-location guidance must finish before recovery');
    assert(geometry.feedback.bottom <= geometry.sectionBottom + 1, 'The combined browser section must contain recovery');
    const previous = await header.boundingBox();
    assert(previous && previous.y + previous.height <= geometry.feedback.top, 'Connection facts must finish before shared feedback');
    const next = await page.locator('#full-analytics').boundingBox();
    assert(next && next.y >= geometry.feedback.bottom, 'The next section must follow recovery');
    const details = feedback.getByRole('button', { name: 'Browser alert' });
    if (await details.count()) {
      await details.click();
      assert.equal(await page.getByRole('dialog').getByRole('alert').innerText(), 'The browser extension is not connected right now.');
      await page.getByRole('dialog').getByRole('button', { name: 'Close', exact: true }).click();
    } else assert.equal(await feedback.getByRole('alert').innerText(), 'The browser extension is not connected right now.');
  } finally { await page.close(); }
});

for (const width of [320, 390, 600]) test(`essential activation content stays separated and can grow at ${width} with doubled text`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 1000 }, reducedMotion: 'reduce' });
  try {
    await page.goto(harness.base + '?workspace=settings&state=loading&transitions=1&mode=light');
    await page.waitForFunction(() => window.__workspaceFixture);
    const push = (method, ...args) => page.evaluate(({ method, args }) => window.__workspaceFixture[method](...args), { method, args });
    await push('snapshot', 'populated');
    await push('connection', 'connected');
    for (const key of await push('pending')) await push('release', key, 'populated', key === 'onboarding.readiness'
      ? { schema: 'ofca-analysis-readiness/v1', commercial_authority: 'active', analysis_admission: 'blocked' } : undefined);
    await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
    const section = page.locator('[data-reserved-region="settings-activation"]');
    const notice = section.getByRole('status');
    await notice.waitFor();
    await page.evaluate(() => document.fonts.ready);
    await settle(page);
    assert.equal(await notice.innerText(), "New messages aren't being analyzed");
    assert.equal(await section.getByRole('button').count(), 0, 'A confirmed analysis block must not offer a status retry that cannot resolve it');
    const geometry = await section.evaluate((node) => {
      const bounds = node.getBoundingClientRect();
      const content = node.querySelector('[data-region-content]').getBoundingClientRect();
      // Sections an installation doesn't offer stay mounted but unrendered, so the next one is the next rendered one.
      const sections = [...document.querySelectorAll('[data-settings-section]')].filter((section) => section.getClientRects().length > 0);
      const next = sections[sections.indexOf(node.closest('[data-settings-section]')) + 1].getBoundingClientRect();
      const notice = node.querySelector('[role="status"]').getBoundingClientRect();
      const title = node.querySelector('h2');
      const header = title.closest('[data-settings-section-header]');
      const status = header.querySelector('[data-settings-status]').getBoundingClientRect();
      const range = document.createRange(); range.selectNodeContents(header);
      const titleRange = document.createRange(); titleRange.selectNodeContents(title);
      return { minimum: parseFloat(getComputedStyle(node).minBlockSize), height: bounds.height, bottom: bounds.bottom,
        contentBottom: content.bottom, nextTop: next.top, horizontalOverflow: node.scrollWidth > node.clientWidth,
        headerBeforeNotice: [...range.getClientRects()].every((box) => box.bottom <= notice.top) && status.bottom <= notice.top,
        statusFits: status.left >= bounds.left && status.right <= bounds.right,
        titleStatusSeparate: [...titleRange.getClientRects()].every((box) => box.bottom <= status.top || box.top >= status.bottom || box.right <= status.left || box.left >= status.right) };
    });
    assert(geometry.height >= geometry.minimum, 'Essential content must preserve its minimum and remain free to grow');
    assert(geometry.contentBottom <= geometry.bottom + 1 && geometry.nextTop >= geometry.contentBottom,
      'The full notice must fit before the next Settings section');
    assert(geometry.headerBeforeNotice, 'The heading and summary must finish before the warning');
    assert(geometry.statusFits && geometry.titleStatusSeparate, 'The status must fit the section and remain separate from its title');
    assert(!geometry.horizontalOverflow, 'Expanded essential copy must not require horizontal scrolling');
    assert.equal(await notice.getByRole('button', { name: 'Show details' }).count(), 0);
  } finally { await page.close(); }
});

for (const mode of ['light', 'dark']) for (const [width, scale] of [[1440, 1], [390, 1], [320, 2]]) {
  test(`Dashboard mismatch V03 at ${width} with ${scale}x text in ${mode}`, async t => {
    const page = await browser.newPage({ colorScheme: mode, reducedMotion: 'reduce' });
    try {
      await page.addInitScript(value => {
        const setFontSize = () => {
          const root = document.documentElement;
          if (!root) return;
          root.style.fontSize = `${value * 100}%`;
          observer.disconnect();
        };
        const observer = new MutationObserver(setFontSize);
        observer.observe(document, { childList: true });
        setFontSize();
      }, scale);
      const open = async workspace => {
        await page.goto(harness.base + `?workspace=${workspace}&mode=${mode}`);
        await page.waitForFunction(() => window.__workspaceFixture);
        await page.evaluate(() => document.fonts.ready);
        await settle(page);
      };
      t.diagnostic(JSON.stringify(await measureDashboardMismatch(page, open, width, scale)));
    } finally { await page.close(); }
  });
}

test('activation Dashboard prompt uses Guided task geometry at 1440', async () => {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' });
  try {
    await page.goto(harness.base + '?workspace=dashboard-continuation&state=activating-full-analytics');
    await page.waitForFunction(() => window.__workspaceFixture);
    await publishActivation(page, 'review');
    const prompt = page.getByRole('region', { name: 'Turn on Full analytics' });
    await prompt.waitFor();
    await page.evaluate(() => document.fonts.ready);
    await settle(page);
    const geometry = await prompt.evaluate(node => {
      const title = node.querySelector('h2');
      const body = node.querySelector('p');
      const action = node.querySelector('.onboarding-action');
      const style = getComputedStyle(title);
      return {
        width: node.getBoundingClientRect().width,
        padding: getComputedStyle(node).padding,
        fontSize: style.fontSize,
        titleLines: title.getClientRects().length,
        titleHeight: parseFloat(style.height),
        lineHeight: parseFloat(style.lineHeight),
        actionGap: action.getBoundingClientRect().top - body.getBoundingClientRect().bottom,
      };
    });
    assert.equal(geometry.width, 760);
    assert.equal(geometry.padding, '32px');
    assert.equal(geometry.fontSize, '22px');
    assert.equal(geometry.titleLines, 1);
    assert.equal(geometry.titleHeight, geometry.lineHeight);
    assert.equal(geometry.actionGap, 24);
  } finally { await page.close(); }
});

for (const mode of ['light', 'dark']) for (const width of [320, 390, 1440]) {
  test(`Dashboard collection continuation uses Guided task geometry at ${width} in ${mode}`, async () => {
    const page = await browser.newPage({ viewport: { width, height: 900 }, colorScheme: mode, reducedMotion: 'reduce' });
    try {
      for (const [state, title] of [
        ['allow-message-collection', 'Allow OnlyFans activity collection'],
        ['allow-onlyfans-access', 'Allow site access'],
        ['message-collection-off', 'OnlyFans activity collection is off'],
      ]) {
        await page.goto(harness.base + `?workspace=dashboard-continuation&state=${state}&mode=${mode}`);
        if (width === 320) await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
        const task = page.getByRole('region', { name: title, exact: true });
        await task.waitFor();
        await page.evaluate(() => document.fonts.ready);
        const geometry = await task.evaluate(node => {
          const title = node.querySelector('h2');
          const headingStyle = getComputedStyle(title);
          const bounds = node.getBoundingClientRect();
          const spot = node.querySelector('.onboarding-spot')?.getBoundingClientRect();
          const region = node.closest('[data-reserved-region="dashboard-setup"]').getBoundingClientRect();
          return { width: bounds.width, left: bounds.left, regionLeft: region.left, spot: spot?.width,
            padding: getComputedStyle(node).padding, fontSize: headingStyle.fontSize,
            titleHeight: title.getBoundingClientRect().height, lineHeight: parseFloat(headingStyle.lineHeight),
            fits: node.scrollWidth <= node.clientWidth && bounds.right <= innerWidth && document.documentElement.scrollWidth <= innerWidth };
        });
        assert.equal(geometry.spot, 40);
        assert.equal(geometry.left, geometry.regionLeft);
        assert(geometry.fits);
        assert.equal(await task.getByRole('button', { name: 'Review in browser', exact: true }).count(), 1);
        if (width === 1440) {
          assert.equal(geometry.width, 760);
          assert.equal(geometry.padding, '32px');
          assert.equal(geometry.fontSize, '22px');
          assert.equal(geometry.titleHeight, geometry.lineHeight);
        }
      }
    } finally { await page.close(); }
  });
}

for (const mode of ['light', 'dark']) for (const width of [320, 390, 1440]) test(`activation offer scroll and styling at ${width} in ${mode}`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 640 }, reducedMotion: 'reduce', colorScheme: mode });
  try {
    await page.goto(harness.base + `?workspace=settings&state=fresh&mode=${mode}`);
    await page.waitForFunction(() => window.__workspaceFixture);
    if (width === 320) await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
    await openActivationReview(page);
    const offer = page.getByRole('dialog', { name: 'Activate Full analytics' });
    await page.evaluate(() => document.fonts.ready);
    await settle(page);
    const geometry = await offer.evaluate(node => {
      const content = node.querySelector('.MuiDialogContent-root');
      const rows = [...node.querySelectorAll('dl > div')];
      const price = rows[1].querySelector('dd');
      const caption = price.lastElementChild;
      const quiet = [...node.querySelectorAll('.MuiButton-text')];
      const style = getComputedStyle(node);
      return {
        clipped: content.scrollHeight > content.clientHeight,
        shadow: style.boxShadow,
        spot: node.querySelector('.onboarding-spot')?.getBoundingClientRect().width,
        rowCount: rows.length,
        valueAlignments: rows.map(row => getComputedStyle(row.querySelector('dd')).textAlign),
        valuesShareLeftEdge: rows.every(row => row.querySelector('dt').getBoundingClientRect().left
          === row.querySelector('dd').getBoundingClientRect().left),
        valueStyle: [getComputedStyle(price).fontWeight, getComputedStyle(price).fontVariantNumeric, getComputedStyle(price).textAlign],
        captionStyle: [getComputedStyle(caption).color, getComputedStyle(caption).display, getComputedStyle(caption).fontWeight],
        secondaryInk: getComputedStyle(rows[0].querySelector('dt')).color,
        trailingDivider: getComputedStyle(rows.at(-1)).borderBottomWidth,
        quiet: quiet.map(button => [getComputedStyle(button).color, getComputedStyle(button).paddingInlineStart, getComputedStyle(button).textAlign]),
        ink: getComputedStyle(price).color,
        fits: node.getBoundingClientRect().left >= 0 && node.getBoundingClientRect().right <= innerWidth && node.scrollWidth <= node.clientWidth,
      };
    });
    assert(!geometry.clipped, 'The offer content must scroll with the title and actions');
    assert.notEqual(geometry.shadow, 'none');
    assert.equal(geometry.spot, 40);
    assert.equal(geometry.rowCount, 5);
    assert.deepEqual(geometry.valueStyle, ['600', 'tabular-nums', width === 320 ? 'start' : 'end']);
    assert(geometry.valueAlignments.every(align => align === (width === 320 ? 'start' : 'end')));
    assert.deepEqual(geometry.captionStyle, [geometry.secondaryInk, 'block', '400']);
    assert.notEqual(geometry.secondaryInk, geometry.ink);
    assert.equal(geometry.trailingDivider, '0px');
    assert(geometry.quiet.length >= 2 && geometry.quiet.every(([color, padding, align]) => color === geometry.ink && padding === '0px' && align === 'start'));
    assert(geometry.fits);
    await measureOfferPrice(offer, mode);
    if (width === 320) {
      assert(geometry.valuesShareLeftEdge);
      const title = offer.getByRole('heading');
      const before = await title.boundingBox();
      await offer.getByRole('button', { name: 'Use an activation code' }).scrollIntoViewIfNeeded();
      assert((await title.boundingBox()).y < before.y, 'Scrolling to the code entry moves the title with the rest of the offer');
    }
    await offer.getByRole('button', { name: 'Activate for free' }).focus();
    for (const name of ['Not now', 'Use an activation code']) {
      await page.keyboard.press('Tab');
      assert(await offer.getByRole('button', { name }).evaluate(button => button === document.activeElement));
    }
    await offer.getByRole('button', { name: 'Use an activation code' }).click();
    const code = page.getByRole('dialog', { name: 'Use an activation code' });
    assert.equal(await code.locator('.onboarding-spot').count(), 1);
    assert(await code.getByRole('textbox', { name: 'Activation code' }).evaluate(input => input === document.activeElement));
  } finally { await page.close(); }
});

for (const width of [320, 390, 600]) test(`supported activation recovery grows beyond its minimum at ${width} with doubled text`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 1000 }, reducedMotion: 'reduce' });
  try {
    await page.goto(harness.base + '?workspace=settings&state=loading&transitions=1&mode=light');
    await page.waitForFunction(() => window.__workspaceFixture);
    const push = (method, ...args) => page.evaluate(({ method, args }) => window.__workspaceFixture[method](...args), { method, args });
    await push('snapshot', 'fresh'); await push('connection', 'connected');
    for (const key of await push('pending')) await push('release', key, 'fresh');
    await openActivationReview(page);
    await push('desktopActionError', 'reply_lost');
    await page.getByRole('button', { name: 'Activate for free' }).click();
    const expected = 'Activation could not be confirmed.';
    await push('release', 'onboarding.readiness', 'fresh');
    await page.getByRole('alert').filter({ hasText: expected }).waitFor();
    await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
    await page.evaluate(() => document.fonts.ready); await settle(page);
    const section = page.getByRole('dialog');
    const notice = section.getByRole('alert');
    assert.equal(await notice.innerText(), expected);
    await notice.scrollIntoViewIfNeeded();
    await settle(page);
    const geometry = await section.evaluate(node => {
      const notice = node.querySelector('[role="alert"]');
      const box = notice.getBoundingClientRect();
      const action = node.querySelector('.MuiDialogActions-root').getBoundingClientRect();
      const range = document.createRange(); range.selectNodeContents(notice);
      return { height: box.height,
        fits: [...range.getClientRects()].every(text => text.left >= box.left && text.right <= box.right && text.bottom <= box.bottom),
        actionPrecedes: action.bottom <= box.top && box.bottom <= node.getBoundingClientRect().bottom,
        modalFits: node.getBoundingClientRect().left >= 0 && node.getBoundingClientRect().right <= innerWidth,
        pageFits: document.documentElement.scrollWidth <= innerWidth + 1 };
    });
    assert(geometry.height > 0, 'Recovery must occupy a readable row');
    assert(geometry.fits && geometry.actionPrecedes && geometry.modalFits && geometry.pageFits, 'The complete modal recovery and its action must remain separate and reachable: ' + JSON.stringify(geometry));
    assert.equal(await notice.getByRole('button', { name: 'Show details' }).count(), 0);
  } finally { await page.close(); }
});

for (const width of [320, 390]) for (const mode of ['light', 'dark']) test(`passkey pending and recovery fit at ${width} with doubled text in ${mode}`, async () => {
  const height = width === 320 ? 640 : 900;
  const page = await browser.newPage({ viewport: { width, height }, reducedMotion: 'reduce', colorScheme: mode });
  try {
    await page.addInitScript(() => { new MutationObserver(() => {
      if (document.documentElement && !document.documentElement.style.fontSize) document.documentElement.style.fontSize = '200%';
    }).observe(document, { childList: true, subtree: true }); });
    await page.goto(harness.base + `?workspace=passkey&state=loading&transitions=1&mode=${mode}`);
    await page.waitForFunction(() => window.__workspaceFixture);
    await page.evaluate(() => document.fonts.ready); await settle(page);
    const main = page.getByRole('main');
    const action = main.getByRole('button', { name: 'Sign in with passkey', exact: true });
    const flowBox = locator => locator.evaluate(node => {
      const { x, y, width, height } = node.getBoundingClientRect();
      return { x, y: y + window.scrollY, width, height };
    });
    const anchors = () => Promise.all([
      main.getByRole('heading', { level: 1 }), main.locator('.onboarding-surface-body'),
      main.locator('.onboarding-action-content'), main.getByRole('button', { name: 'Set up a passkey', exact: true }),
      page.getByRole('banner'),
    ].map(flowBox));
    const footer = page.getByRole('contentinfo');
    const restingFooter = await flowBox(footer);
    const before = await anchors();
    const buttonBox = await flowBox(action);
    const card = main.locator('[data-composition="trust"]');
    const restCard = await flowBox(card);
    const feedback = main.locator('[data-visual="passkey-feedback"]');
    assert.equal((await flowBox(feedback)).height, 0, 'Resting feedback has no reserved height');
    await action.click();
    const pendingRow = main.getByRole('status').filter({ hasText: 'Waiting for your passkey…' });
    await pendingRow.waitFor();
    await settle(page);
    assert.equal(await action.count(), 0, 'The pending row replaces the button');
    const pendingBox = await flowBox(pendingRow);
    assert.deepEqual([pendingBox.x, pendingBox.y, pendingBox.height], [buttonBox.x, buttonBox.y, buttonBox.height],
      'The pending row must start where the button starts and occupy its height');
    assert.deepEqual(await anchors(), before, 'Pending feedback must preserve layout anchors');
    assert.deepEqual(await flowBox(card), restCard, 'The pending row must preserve the card geometry');
    assert.deepEqual(await flowBox(footer), restingFooter, 'The pending row must preserve the footer geometry');
    assert.equal((await flowBox(feedback)).height, 0, 'Pending feedback has no reserved height');
    await page.evaluate(() => window.__workspaceFixture.reject('passkey.login', 'NotAllowedError'));
    await page.getByRole('alert').filter({ hasText: 'Sign-in didn’t finish.' }).waitFor(); await settle(page);
    assert.deepEqual(await anchors(), before, 'Incomplete sign-in must preserve layout anchors');
    assert.deepEqual(await flowBox(action), buttonBox, 'The restored action must return to its original geometry');
    const feedbackBox = await flowBox(feedback);
    assert(feedbackBox.height > 0 && feedbackBox.y >= before[3].y + before[3].height
      && await feedback.evaluate(node => node === node.parentElement.lastElementChild),
      'Visible feedback follows the content at the end of the card');
    const finalCard = await flowBox(card);
    const padding = await main.evaluate(node => parseFloat(getComputedStyle(node.parentElement).paddingBottom));
    assert.deepEqual(await flowBox(footer), { ...restingFooter,
      y: Math.max(restingFooter.y, finalCard.y + finalCard.height + padding) },
    'The footer stays at the viewport foot or follows the enlarged card');
    const geometry = await page.locator('[data-reserved-region="passkey-card"]').evaluate(node => {
      const box = node.getBoundingClientRect();
      const alert = node.querySelector('[role="alert"]');
      const range = document.createRange(); range.selectNodeContents(alert);
      return { textFits: [...range.getClientRects()].every(text => text.left >= box.left && text.right <= box.right && text.bottom <= box.bottom),
        pageFits: document.documentElement.scrollWidth <= innerWidth + 1 };
    });
    assert(geometry.textFits && geometry.pageFits, 'Passkey recovery must remain fully visible without horizontal scrolling');
    await page.getByRole('alert').scrollIntoViewIfNeeded();
    const box = await page.getByRole('alert').boundingBox();
    assert(box.y >= 0 && box.y + box.height <= height + 1, 'Passkey recovery must be reachable by scrolling');
    if (width === 320) assert(await page.evaluate(() => window.scrollY > 0), 'Overflowing recovery scrolls the document');
  } finally { await page.close(); }
});

for (const width of [320, 390]) for (const mode of ['light', 'dark']) for (const outcome of [true, false, 'unavailable']) {
  test(`delayed passkey status ${outcome} preserves the task at ${width} with doubled text in ${mode}`, async () => {
    const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce', colorScheme: mode });
    let release;
    const pending = new Promise(resolve => { release = resolve; });
    let protectedRequests = 0, statusReads = 0;
    try {
      await installWatcher(page, { requiredRegions: ['passkey-card', 'passkey-feedback'], taskTransitions: 'passkey' });
      await page.addInitScript(() => { new MutationObserver(() => {
        if (document.documentElement && !document.documentElement.style.fontSize) document.documentElement.style.fontSize = '200%';
      }).observe(document, { childList: true, subtree: true }); });
      await page.route('**/*', async route => {
        const path = new URL(route.request().url()).pathname.replace(/^\/static\/dist(?=\/)/u, '');
        if (path === '/api/v1/saved-data/available' && route.request().method() === 'POST') return route.fulfill({ json: { available: false } });
        if (route.request().method() === 'POST') protectedRequests++;
        if (path === '/api/v1/webauthn/session-state') {
          statusReads++;
          await pending;
          return route.fulfill({ status: outcome === 'unavailable' ? 503 : 200, contentType: 'application/json',
            body: JSON.stringify({ enrolled: outcome, authenticated: false, csrf_token: null }) });
        }
        if (path.startsWith('/api/')) return route.fulfill({ status: 503, contentType: 'application/json', body: '{}' });
        if (!/^\/(assets\/[\w.-]+|index.html)?$/.test(path)) return route.abort();
        const body = await readFile(new URL(path === '/' ? 'index.html' : path.slice(1), new URL('../../app/static/dist/', import.meta.url)));
        return route.fulfill({ body, contentType: path.endsWith('.js') ? 'text/javascript' : path.endsWith('.css') ? 'text/css' : path.endsWith('.woff2') ? 'font/woff2' : 'text/html' });
      });
      await page.goto('http://production-status.localhost/', { waitUntil: 'domcontentloaded' });
      await page.getByRole('heading', { name: 'Checking this computer…', exact: true }).waitFor();
      await page.evaluate(() => document.fonts.ready); await settle(page);
      const title = page.getByRole('heading', { level: 1 });
      const start = await title.boundingBox();
      assert.equal(await page.getByRole('button').count(), 0, 'Unknown status cannot expose a passkey ceremony');
      release();
      await page.getByRole('heading', { name: outcome === false ? 'Protect your analytics on this computer'
        : outcome === true ? 'Confirm it’s you' : 'Can’t check your passkey right now', exact: true }).waitFor();
      if (outcome === 'unavailable') {
        await page.waitForTimeout(1000);
        await page.getByRole('heading', { name: 'Can’t check your passkey right now', exact: true }).waitFor();
      }
      await settle(page);
      const end = await title.boundingBox();
      assert.deepEqual([end.x, end.y, end.width], [start.x, start.y, start.width], 'A delayed status result retains the heading anchor');
      assert.equal(await page.getByRole('button').count(), outcome === 'unavailable' ? 0 : 1,
        outcome === 'unavailable' ? 'Unavailable status must not expose a ceremony or manual retry button' : 'Only one passkey action is focusable');
      const geometry = await page.locator('[data-reserved-region="passkey-card"]').evaluate(node => {
        const bounds = node.getBoundingClientRect();
        const range = document.createRange(); range.selectNodeContents(node);
        return { fits: [...range.getClientRects()].every(box => box.left >= bounds.left && box.right <= bounds.right && box.bottom <= bounds.bottom),
          pageFits: document.documentElement.scrollWidth <= innerWidth + 1 };
      });
      assert(geometry.fits && geometry.pageFits, 'All passkey text must fit naturally without horizontal scrolling');
      assert.equal(protectedRequests, 0, 'A status read cannot start enrollment or sign-in');
      assert.equal(statusReads, outcome === 'unavailable' ? 4 : 1);
      assert.deepEqual((await readWatcher(page)).failures, []);
    } finally { release(); await page.close(); }
  });
}

for (const width of [320, 390, 1440]) for (const mode of ['light', 'dark']) for (const motion of ['reduce', 'no-preference']) {
  test(`passkey arrival stays stationary at ${width} in ${mode} with ${motion} motion`, async () => {
    const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: motion, colorScheme: mode });
    let protectedRequests = 0, statusReads = 0;
    try {
      await installWatcher(page, { requiredRegions: ['passkey-card', 'passkey-feedback'], taskTransitions: 'passkey' });
      await page.route('**/*', async route => {
        const path = new URL(route.request().url()).pathname.replace(/^\/static\/dist(?=\/)/u, '');
        if (path === '/api/v1/saved-data/available' && route.request().method() === 'POST') return route.fulfill({ json: { available: false } });
        if (route.request().method() === 'POST') protectedRequests++;
        if (path === '/api/v1/webauthn/session-state') {
          statusReads++;
          return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ enrolled: true, authenticated: false, csrf_token: null }) });
        }
        if (path.startsWith('/api/')) return route.fulfill({ status: 503, contentType: 'application/json', body: '{}' });
        if (!/^\/(assets\/[\w.-]+|index.html)?$/.test(path)) return route.abort();
        const body = await readFile(new URL(path === '/' ? 'index.html' : path.slice(1), new URL('../../app/static/dist/', import.meta.url)));
        return route.fulfill({ body, contentType: path.endsWith('.js') ? 'text/javascript' : path.endsWith('.css') ? 'text/css' : path.endsWith('.woff2') ? 'font/woff2' : 'text/html' });
      });
      await page.goto('http://production-arrival.localhost/', { waitUntil: 'domcontentloaded' });
      await page.getByRole('heading', { name: 'Confirm it’s you', exact: true }).waitFor();
      assert.equal(await page.locator('main').evaluate(node => getComputedStyle(node).animationName), 'none');
      await page.waitForTimeout(350);
      await settle(page);
      const observation = await readWatcher(page);
      assert(observation.frames.some(frame => frame.regions.some(region => region.id === 'passkey-card')), 'The first visible task must be measured');
      assert.deepEqual(observation.failures, [], 'Initial task arrival must preserve its painted geometry');
      assert.equal(protectedRequests, 0, 'Task arrival cannot start a passkey ceremony');
      assert.equal(statusReads, 1);
    } finally { await page.close(); }
  });
}

for (const width of [320, 390]) for (const mode of ['light', 'dark']) for (const fonts of ['delayed', 'missing', 'late']) {
  test(`passkey ${fonts} fonts keep the first visible task stable at ${width} in ${mode}`, async () => {
    const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce', colorScheme: mode });
    let release;
    const pending = new Promise(resolve => { release = resolve; });
    let protectedRequests = 0, statusReads = 0;
    try {
      await installWatcher(page, { requiredRegions: ['passkey-card', 'passkey-feedback'], taskTransitions: 'passkey' });
      await page.route('**/*', async route => {
        const path = new URL(route.request().url()).pathname.replace(/^\/static\/dist(?=\/)/u, '');
        if (path === '/api/v1/saved-data/available' && route.request().method() === 'POST') return route.fulfill({ json: { available: false } });
        if (route.request().method() === 'POST') protectedRequests++;
        if (path === '/api/v1/webauthn/session-state') {
          statusReads++;
          return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ enrolled: true, authenticated: false, csrf_token: null }) });
        }
        if (path.startsWith('/api/')) return route.fulfill({ status: 503, contentType: 'application/json', body: '{}' });
        if (!/^\/(assets\/[\w.-]+|index.html)?$/.test(path)) return route.abort();
        if (path.endsWith('.woff2')) {
          if (fonts === 'missing') return route.fulfill({ status: 404 });
          await pending;
        }
        const body = await readFile(new URL(path === '/' ? 'index.html' : path.slice(1), new URL('../../app/static/dist/', import.meta.url)));
        return route.fulfill({ body, contentType: path.endsWith('.js') ? 'text/javascript' : path.endsWith('.css') ? 'text/css' : path.endsWith('.woff2') ? 'font/woff2' : 'text/html' });
      });
      await page.goto('http://production-fonts.localhost/', { waitUntil: 'domcontentloaded' });
      if (fonts !== 'missing') {
        await page.waitForFunction(() => document.querySelector('[data-passkey-font]')?.dataset.passkeyFont === 'loading');
        assert.equal(await page.getByRole('button').count(), 0, 'Hidden setup controls cannot be exposed to assistive technology');
        assert.equal(await page.getByRole('heading', { level: 1 }).count(), 0);
        await page.keyboard.press('Tab');
        assert(await page.evaluate(() => !document.activeElement.closest('[data-passkey-font]')), 'Hidden setup cannot receive keyboard focus');
        await page.waitForTimeout(250);
        assert.equal(statusReads, 1, 'Font loading cannot delay the existing context request');
        if (fonts === 'delayed') release();
      }
      const expected = fonts === 'delayed' ? 'ready' : 'fallback';
      await page.waitForFunction(expected => document.querySelector('[data-passkey-font]')?.dataset.passkeyFont === expected, expected);
      await page.getByRole('heading', { name: 'Confirm it’s you', exact: true }).waitFor();
      await settle(page);
      const action = page.getByRole('button', { name: 'Sign in with passkey', exact: true });
      const bounds = () => Promise.all([page.locator('header'), page.getByRole('heading', { level: 1 }), action,
        page.locator('[data-reserved-region="passkey-feedback"]'), page.locator('footer')].map(node => node.boundingBox()));
      const before = await bounds();
      await action.focus();
      if (fonts === 'late') {
        release(); await page.evaluate(() => document.fonts.ready); await settle(page);
        assert.equal(await page.locator('[data-passkey-font]').getAttribute('data-passkey-font'), 'fallback');
        assert.deepEqual(await bounds(), before, 'Late resource success must not move the visible fallback task');
        assert(await action.evaluate(node => document.activeElement === node), 'Late resource success must preserve action focus');
      }
      const family = await page.getByRole('heading', { level: 1 }).evaluate(node => getComputedStyle(node).fontFamily);
      assert(family.includes(expected === 'ready' ? 'Inter' : 'Arial'));
      assert.equal(await page.getByRole('button').count(), 1);
      assert.equal(protectedRequests, 0, 'Resource readiness cannot start a passkey ceremony');
      assert.equal(statusReads, 1);
      const observation = await readWatcher(page);
      const firstTaskFrame = observation.frames.find(frame => frame.task?.startsWith('passkey:'));
      assert(firstTaskFrame?.regions.some(region => region.id === 'passkey-heading'), 'The heading must be visible on the first task frame');
      assert.deepEqual(observation.failures, []);
    } finally { release(); await page.close(); }
  });
}
