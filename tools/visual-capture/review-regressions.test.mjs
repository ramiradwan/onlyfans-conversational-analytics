import assert from 'node:assert/strict';
import { assertNumericTypography } from './appearance-contracts.mjs';
import { randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { after, before, test } from 'node:test';
import { chromium } from 'playwright';
import { FIXED_NOW, startHarness } from './capture.mjs';
import { auditScrolling, installWatcher, readWatcher } from './shift-watcher.mjs';
import { renderSurfaceState, SURFACE_STATES } from '../../extension/qualification/surface-fixtures.mjs';
import { publishActivation } from './desktop-decision-fixtures.mjs';

let browser, harness;
before(async () => { browser = await chromium.launch(); harness = await startHarness(); });
after(async () => { await browser?.close(); harness?.vite.kill(); });
const settle = (page) => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));

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

for (const width of [320, 1280]) for (const [state, title, count] of [
  [{ ...SURFACE_STATES.software_activation, fullIntent: true, browserTask: true }, 'activation-title', 2],
  [SURFACE_STATES.full_review, 'full-choice-title', 1],
]) test(`approved copy sections sit under ${title} at ${width}`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 900 } });
  try {
    await renderSurfaceState(page, state);
    const levels = await page.evaluate(id => {
      const size = node => parseFloat(getComputedStyle(node).fontSize);
      const top = node => node.getBoundingClientRect().top, bottom = node => node.getBoundingClientRect().bottom;
      const heading = document.getElementById(id);
      const sections = [...heading.closest('section').querySelectorAll('.points > h2')]
        .filter(node => node !== heading && node.getClientRects().length);
      const copy = sections[0].nextElementSibling;
      return { title: size(heading), sizes: sections.map(size), paragraph: top(copy.nextElementSibling) - bottom(copy),
        below: sections.map(node => top(node.nextElementSibling) - bottom(node)),
        above: sections.filter(node => node.parentElement.previousElementSibling?.matches('.points'))
          .map(node => top(node) - bottom(node.parentElement.previousElementSibling.lastElementChild)) };
    }, title);
    assert.equal(levels.sizes.length, count);
    assert(levels.sizes.every(size => size < levels.title), 'Approved copy headings must sit under the title');
    assert(levels.below.every(gap => gap < levels.paragraph), 'A section heading must sit closer to its copy than paragraphs do');
    assert(levels.above.length && levels.above.every(gap => gap > levels.paragraph), 'A new section must start further away than the next paragraph');
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
    const chip = section.locator('.MuiChip-root');
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
      const geometry = { slot: await section.locator('[aria-live]').boundingBox(), heading: await section.locator('h2').boundingBox() };
      initial ??= geometry;
      assert.deepEqual(geometry, initial, 'Status changes move the reserved slot or heading');
      const bounds = await section.boundingBox(), summary = await section.locator('#activation-notice p[data-region-content]').boundingBox();
      assert(summary.y >= bounds.y && summary.y + summary.height <= bounds.y + bounds.height, 'Summary escapes its reserved section');
      if (label) {
        assert.equal(await chip.innerText(), label);
        initialChip ??= await chip.boundingBox();
        assert.deepEqual(await chip.boundingBox(), initialChip, 'Status chip moves inside its slot');
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
    await page.getByRole('button', { name: 'Pause collecting', exact: true }).click();
    await push('resolve', 'browser.setCapture', 'unreachable');
    const feedback = page.locator('[data-reserved-region="browser-feedback"]');
    await feedback.locator('[data-region-content]').waitFor({ state: 'visible' });
    await page.evaluate(() => document.fonts.ready);
    await settle(page);
    await feedback.scrollIntoViewIfNeeded();
    assert(await page.getByRole('button', { name: 'Allow in extension', exact: true }).first().isVisible());
    const following = page.getByText('Extension linked to this app', { exact: true });
    assert(await following.isVisible());
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
    const previous = await following.boundingBox();
    assert(previous && previous.y + previous.height <= geometry.feedback.top, 'Connection facts must finish before shared feedback');
    const next = await page.locator('#full-analytics').boundingBox();
    assert(next && next.y >= geometry.feedback.bottom, 'The next section must follow recovery');
    const details = feedback.getByRole('button', { name: 'Show details' });
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
      const next = document.querySelector('[data-reserved-region="settings-history"]').getBoundingClientRect();
      const notice = node.querySelector('[data-reserved-region="activation-notice"]').getBoundingClientRect();
      const title = node.querySelector('h2');
      const header = title.parentElement;
      const status = header.nextElementSibling.getBoundingClientRect();
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

for (const width of [320, 390, 600]) test(`supported activation recovery grows beyond its minimum at ${width} with doubled text`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 1000 }, reducedMotion: 'reduce' });
  try {
    await page.goto(harness.base + '?workspace=settings&state=loading&transitions=1&mode=light');
    await page.waitForFunction(() => window.__workspaceFixture);
    const push = (method, ...args) => page.evaluate(({ method, args }) => window.__workspaceFixture[method](...args), { method, args });
    await push('snapshot', 'fresh'); await push('connection', 'connected');
    for (const key of await push('pending')) await push('release', key, 'fresh');
    await publishActivation(page, 'review');
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
      const notice = node.querySelector('[data-reserved-region="activation-offer-feedback"]');
      const content = notice.querySelector('[data-region-content]');
      const box = notice.getBoundingClientRect();
      const action = node.querySelector('.MuiDialogActions-root').getBoundingClientRect();
      const range = document.createRange(); range.selectNodeContents(content);
      return { height: box.height, minimum: parseFloat(getComputedStyle(notice).minHeight),
        fits: [...range.getClientRects()].every(text => text.left >= box.left && text.right <= box.right && text.bottom <= box.bottom),
        actionFollows: action.top >= box.bottom && action.bottom <= node.getBoundingClientRect().bottom,
        modalFits: node.getBoundingClientRect().left >= 0 && node.getBoundingClientRect().right <= innerWidth,
        pageFits: document.documentElement.scrollWidth <= innerWidth + 1 };
    });
    assert(geometry.height >= geometry.minimum, 'Recovery must preserve the reserved minimum while allowing complete text');
    assert(geometry.fits && geometry.actionFollows && geometry.modalFits && geometry.pageFits, 'The complete modal recovery and its action must remain separate and reachable: ' + JSON.stringify(geometry));
    assert.equal(await notice.getByRole('button', { name: 'Show details' }).count(), 0);
  } finally { await page.close(); }
});

for (const width of [320, 390]) for (const mode of ['light', 'dark']) test(`passkey pending and recovery fit at ${width} with doubled text in ${mode}`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce', colorScheme: mode });
  try {
    await page.addInitScript(() => { new MutationObserver(() => {
      if (document.documentElement && !document.documentElement.style.fontSize) document.documentElement.style.fontSize = '200%';
    }).observe(document, { childList: true, subtree: true }); });
    await page.goto(harness.base + `?workspace=passkey&state=loading&transitions=1&mode=${mode}`);
    await page.waitForFunction(() => window.__workspaceFixture);
    await page.evaluate(() => document.fonts.ready); await settle(page);
    const keyBoxes = () => Promise.all([page.getByRole('heading', { level: 1 }), page.getByRole('button', { name: 'Sign in with passkey', exact: true }), page.locator('[data-reserved-region="passkey-feedback"]')].map(node => node.boundingBox()));
    const before = await keyBoxes();
    await page.getByRole('button', { name: 'Sign in with passkey', exact: true }).click();
    await settle(page);
    const pending = await keyBoxes();
    assert.deepEqual(pending, before, 'Pending feedback must not move the task or its action');
    await page.evaluate(() => window.__workspaceFixture.reject('passkey.login', 'NotAllowedError'));
    await page.getByRole('alert').filter({ hasText: 'Sign-in did not finish.' }).waitFor(); await settle(page);
    assert.deepEqual(await keyBoxes(), before, 'Incomplete sign-in must not move the task or its action');
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
    assert(box.y >= 0 && box.y + box.height <= 901, 'Passkey recovery must be reachable by scrolling');
  } finally { await page.close(); }
});

for (const width of [320, 390]) for (const mode of ['light', 'dark']) for (const outcome of [true, false, 'unavailable']) {
  test(`delayed passkey status ${outcome} preserves the task at ${width} with doubled text in ${mode}`, async () => {
    const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce', colorScheme: mode });
    let release;
    const pending = new Promise(resolve => { release = resolve; });
    let protectedRequests = 0, statusReads = 0;
    try {
      await installWatcher(page, { requiredRegions: ['passkey-card', 'passkey-feedback'] });
      await page.addInitScript(() => { new MutationObserver(() => {
        if (document.documentElement && !document.documentElement.style.fontSize) document.documentElement.style.fontSize = '200%';
      }).observe(document, { childList: true, subtree: true }); });
      await page.route('**/*', async route => {
        const path = new URL(route.request().url()).pathname.replace(/^\/static\/dist(?=\/)/u, '');
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
      const boxes = () => Promise.all([page.getByRole('heading', { level: 1 }), page.getByRole('button'),
        page.locator('[data-reserved-region="passkey-feedback"]')].map(node => node.boundingBox()));
      const before = await boxes();
      assert(await page.getByRole('button').isDisabled(), 'Unknown status cannot start a passkey ceremony');
      release();
      await page.getByRole('heading', { name: outcome === false ? 'Create a passkey on this computer'
        : outcome === true ? 'Sign in on this computer' : 'Passkey setup could not be checked', exact: true }).waitFor();
      if (outcome === 'unavailable') {
        await page.waitForTimeout(3250);
        await page.getByRole('heading', { name: 'Passkey setup could not be checked', exact: true }).waitFor();
      }
      await settle(page);
      assert.deepEqual(await boxes(), before, 'A delayed status result must not move the heading, action or feedback');
      assert.equal(await page.getByRole('button').count(), 1, 'Only one passkey action is focusable');
      const geometry = await page.locator('[data-reserved-region="passkey-card"]').evaluate(node => {
        const bounds = node.getBoundingClientRect();
        const range = document.createRange(); range.selectNodeContents(node);
        return { fits: [...range.getClientRects()].every(box => box.left >= bounds.left && box.right <= bounds.right && box.bottom <= bounds.bottom),
          pageFits: document.documentElement.scrollWidth <= innerWidth + 1 };
      });
      assert(geometry.fits && geometry.pageFits, 'All passkey text must fit naturally without horizontal scrolling');
      assert.equal(protectedRequests, 0, 'A status read cannot start enrollment or sign-in');
      assert.equal(statusReads, outcome === 'unavailable' ? 3 : 1);
      assert.deepEqual((await readWatcher(page)).failures, []);
    } finally { release(); await page.close(); }
  });
}

for (const width of [320, 390, 1440]) for (const mode of ['light', 'dark']) for (const motion of ['reduce', 'no-preference']) {
  test(`passkey arrival stays stationary at ${width} in ${mode} with ${motion} motion`, async () => {
    const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: motion, colorScheme: mode });
    let protectedRequests = 0, statusReads = 0;
    try {
      await installWatcher(page, { requiredRegions: ['passkey-card', 'passkey-feedback'] });
      await page.route('**/*', async route => {
        const path = new URL(route.request().url()).pathname.replace(/^\/static\/dist(?=\/)/u, '');
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
      await page.getByRole('heading', { name: 'Sign in on this computer', exact: true }).waitFor();
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
      await installWatcher(page, { requiredRegions: ['passkey-card', 'passkey-feedback'] });
      await page.route('**/*', async route => {
        const path = new URL(route.request().url()).pathname.replace(/^\/static\/dist(?=\/)/u, '');
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
      await page.getByRole('heading', { name: 'Sign in on this computer', exact: true }).waitFor();
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
      assert.deepEqual((await readWatcher(page)).failures, []);
    } finally { release(); await page.close(); }
  });
}
