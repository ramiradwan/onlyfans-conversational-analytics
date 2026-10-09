import assert from 'node:assert/strict';
import { assertNumericTypography } from './appearance-contracts.mjs';
import { randomUUID } from 'node:crypto';
import { after, before, test } from 'node:test';
import { chromium } from 'playwright';
import { startHarness } from './capture.mjs';
import { auditScrolling, installWatcher, readWatcher } from './shift-watcher.mjs';
import { installProvisioningFixture, openProvisioningFixture } from './provisioning-driver.mjs';

let browser, harness;
before(async () => { browser = await chromium.launch(); harness = await startHarness(); });
after(async () => { await browser?.close(); harness?.vite.kill(); });
const settle = (page) => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));

for (const width of [390, 1440]) {
  test(`completed provisioning stays visible at ${width}`, async () => {
    const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce' });
    try {
      await installProvisioningFixture(page, { stage: null });
      await openProvisioningFixture(page);
      assert(await page.getByRole('heading', { name: 'Setup finished' }).isVisible(), 'Completion is blank');
      assert(await page.locator('#finalize-step-description').isVisible());
    } finally { await page.close(); }
  });
  test(`live provisioning completion and action positions at ${width}`, async () => {
    const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce' });
    try {
      await installWatcher(page, { requiredRegions: ['provisioning-actions'] });
      await installProvisioningFixture(page);
      await openProvisioningFixture(page);
      await settle(page);
      const before = await page.locator('#open-secure-setup').boundingBox();
      await page.locator('#claim-package').fill('abcdefgh');
      await settle(page);
      assert.deepEqual(await page.locator('#open-secure-setup').boundingBox(), before, 'The setup link moves after a paste');
      await page.locator('#claim-submit').click();
      await page.evaluate(() => window.__provisioningFixture.hold.push('acquire', 'finalize'));
      await page.locator('#confirm-identity').click();
      await page.evaluate(() => window.dispatchEvent(new Event('focus')));
      await page.waitForFunction(() => window.__provisioningFixture.calls.includes('acquire'));
      await settle(page);
      await page.evaluate(() => window.__provisioningFixture.release('acquire'));
      await page.waitForFunction(() => window.__provisioningFixture.calls.includes('finalize'));
      await page.evaluate(() => window.__provisioningFixture.release('finalize'));
      await settle(page);
      assert(await page.getByRole('heading', { name: 'Setup finished' }).isVisible(), 'Completion is blank');
      assert(await page.locator('#finalize-step-description').isVisible());
      assert.equal(await page.evaluate(() => window.__provisioningFixture.calls.filter((operation) => operation === 'finalize').length), 1);
      assert.deepEqual((await readWatcher(page)).failures, []);
    } finally { await page.close(); }
  });
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

for (const width of [390, 1440]) {
  test('recovery retains its action reservation at ' + width, async () => {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    try {
      await installWatcher(page, { requiredRegions: ['provisioning-actions'] });
      await installProvisioningFixture(page, { stage: 'recovery_required', name: 'recovery' });
      await openProvisioningFixture(page);
      assert.deepEqual((await readWatcher(page)).failures, []);
      assert.equal(await page.locator('#claim-submit').isVisible(), false);
    } finally { await page.close(); }
  });
  test('unavailable approval stays pending after focus at ' + width, async () => {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    try {
      await installWatcher(page);
      await installProvisioningFixture(page, { stage: 'creator_approval_pending', name: 'approval-unavailable-help' });
      await openProvisioningFixture(page);
      const before = await page.evaluate(() => window.__provisioningFixture.calls.filter((operation) => operation === 'acquire').length);
      const retry = page.locator('#acquire-association');
      assert(await retry.isVisible());
      await page.keyboard.press('Tab');
      await retry.focus();
      await retry.blur();
      await settle(page);
      assert.equal(await page.evaluate(() => window.__provisioningFixture.calls.filter((operation) => operation === 'acquire').length), before, 'Control focus must not repeat approval');
      assert.equal(await page.locator('#binding-step').getAttribute('data-state'), 'current');
      assert(await page.locator('#provisioning-status').evaluate((node) => Boolean(node.closest('[aria-current="step"]'))));
      assert.deepEqual((await readWatcher(page)).failures, []);
    } finally { await page.close(); }
  });
}

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
    const section = page.getByRole('heading', { name: 'Full analytics', exact: true }).locator('..').locator('..');
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
      for (const key of await push('pending')) await push('release', key, 'populated', key === 'activation.readiness'
        ? { schema: 'ofca-analysis-readiness/v1', commercial_authority, analysis_admission } : undefined);
      await settle(page);
      const geometry = { slot: await section.locator('[aria-live]').boundingBox(), heading: await section.locator('h2').boundingBox() };
      initial ??= geometry;
      assert.deepEqual(geometry, initial, 'Status changes move the reserved slot or heading');
      const header = await section.boundingBox(), summary = await section.locator('p').boundingBox();
      assert(summary.y + summary.height <= header.y + header.height, 'Summary escapes its reserved header');
      if (label) {
        assert.equal(await chip.innerText(), label);
        initialChip ??= await chip.boundingBox();
        assert.deepEqual(await chip.boundingBox(), initialChip, 'Status chip moves inside its slot');
      } else assert.equal(await chip.count(), 0);
    }
  } finally { await page.close(); }
});

for (const width of [320, 390, 1440]) for (const fontScale of [1, 2]) test(`provisioning recovery remains readable at ${width} with scale ${fontScale}`, async () => {
  const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce' });
  try {
    await installProvisioningFixture(page, { stage: 'creator_approval_pending', name: 'approval-offline' });
    await openProvisioningFixture(page);
    await page.evaluate((scale) => { document.documentElement.style.fontSize = `${16 * scale}px`; }, fontScale);
    for (const reason of ['hosted_unavailable', 'binding_acquisition_unavailable', 'candidate_resolution_conflict']) {
      await page.evaluate((refusal) => {
        window.__provisioningFixture.refusal = refusal;
        return window.__provisioningController.acquireAssociation();
      }, reason);
      await settle(page);
      assert.equal(await page.locator('#feedback-details').isVisible(), false, 'Recovery must not require another action to read');
      assert(await page.locator('#provisioning-status').isVisible(), 'Recovery must be visibly rendered');
      const geometry = await page.locator('#provisioning-status').evaluate((status) => {
        const bounds = status.getBoundingClientRect();
        const feedback = status.closest('[data-step-feedback]').getBoundingClientRect();
        const actions = status.closest('.step').querySelector('.actions').getBoundingClientRect();
        const range = document.createRange();
        range.selectNodeContents(status);
        let opaque = true;
        for (let element = status; element; element = element.parentElement) {
          const css = getComputedStyle(element);
          opaque &&= css.visibility === 'visible' && Number(css.opacity) === 1;
        }
        return {
          text: status.textContent,
          opaque,
          clipPath: getComputedStyle(status).clipPath,
          fits: [...range.getClientRects()].every((box) => box.left >= bounds.left - 1 && box.right <= bounds.right + 1 && box.top >= bounds.top - 1 && box.bottom <= bounds.bottom + 1),
          separate: bounds.bottom <= feedback.bottom + 1 && feedback.bottom <= actions.top,
          pageFits: document.documentElement.scrollWidth <= innerWidth + 1,
        };
      });
      assert(geometry.text.trim().length > 0);
      assert(geometry.opaque, 'Recovery and its ancestors must not be hidden or transparent');
      assert.equal(geometry.clipPath, 'none');
      assert(geometry.fits, 'All recovery text must fit its visible container');
      assert(geometry.separate, 'Recovery must not overlap the actions');
      assert(geometry.pageFits, 'Enlarged text must not introduce horizontal scrolling');
      const retry = page.locator('#acquire-association');
      await retry.scrollIntoViewIfNeeded();
      const bounds = await retry.boundingBox();
      assert(bounds && bounds.y >= 0 && bounds.y + bounds.height <= 901, 'The retry action must remain reachable by scrolling');
    }
  } finally { await page.close(); }
});

for (const width of [320, 390]) test(`browser recovery remains between guidance and pairing controls at ${width} with doubled text`, async () => {
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
    await feedback.getByRole('alert').waitFor();
    await page.evaluate(() => document.fonts.ready);
    await settle(page);
    await feedback.scrollIntoViewIfNeeded();
    assert(await page.getByText('Change these in the browser where the extension is installed.', { exact: true }).isVisible());
    const following = page.getByText('Extension linked to this app', { exact: true });
    assert(await following.isVisible());
    const geometry = await feedback.evaluate((node) => {
      const bounds = node.getBoundingClientRect();
      const facts = node.closest('[data-reserved-region="browser-facts"]');
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
      const rows = [...controls.children].slice(0, 3);
      return {
        instructionsBefore: [...rows, controls.children[3]].every((element) => textBoxes(element).every((box) => box.bottom <= bounds.top + 1)),
        rowsFit: rows.every((row) => textBoxes(row).every((box) => contains(row.getBoundingClientRect(), box))),
        guidanceBottom: controls.children[3].getBoundingClientRect().bottom,
        feedback: bounds.toJSON(),
        feedbackFits: textBoxes(node).every((box) => contains(bounds, box)),
        factsBottom: facts.getBoundingClientRect().bottom,
        horizontalOverflow: facts.scrollWidth > facts.clientWidth,
      };
    });
    assert(geometry.instructionsBefore && geometry.rowsFit, 'Browser instructions and control labels must fit their rows and finish before recovery');
    assert(geometry.feedbackFits && !geometry.horizontalOverflow, 'All recovery text must remain visible without horizontal scrolling');
    assert(geometry.guidanceBottom <= geometry.feedback.top, 'Browser-location guidance must finish before recovery');
    assert(geometry.feedback.bottom <= geometry.factsBottom + 1, 'The browser reservation must contain recovery');
    const next = await following.boundingBox();
    assert(next && next.y >= geometry.feedback.bottom, 'Pairing controls must follow the recovery text');
    assert.equal(await feedback.getByRole('button', { name: 'Show details' }).count(), 0);
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
    for (const key of await push('pending')) await push('release', key, 'populated', key === 'activation.readiness'
      ? { schema: 'ofca-analysis-readiness/v1', commercial_authority: 'active', analysis_admission: 'blocked' } : undefined);
    await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
    const section = page.locator('[data-reserved-region="settings-activation"]');
    const notice = section.getByRole('alert');
    await notice.waitFor();
    await page.evaluate(() => document.fonts.ready);
    await settle(page);
    assert.equal(await notice.innerText(), "New messages aren't being analyzed");
    assert(await section.getByRole('button', { name: 'Check again', exact: true }).isVisible());
    const geometry = await section.evaluate((node) => {
      const bounds = node.getBoundingClientRect();
      const content = node.querySelector('[data-region-content]').getBoundingClientRect();
      const next = document.querySelector('[data-reserved-region="settings-history"]').getBoundingClientRect();
      const notice = node.querySelector('[data-reserved-region="activation-notice"]').getBoundingClientRect();
      const title = node.querySelector('h2');
      const header = title.parentElement;
      const status = header.nextElementSibling.getBoundingClientRect();
      const action = node.querySelector('button').getBoundingClientRect();
      const range = document.createRange(); range.selectNodeContents(header);
      const titleRange = document.createRange(); titleRange.selectNodeContents(title);
      return { minimum: parseFloat(getComputedStyle(node).minBlockSize), height: bounds.height, bottom: bounds.bottom,
        contentBottom: content.bottom, nextTop: next.top, horizontalOverflow: node.scrollWidth > node.clientWidth,
        headerBeforeNotice: [...range.getClientRects()].every((box) => box.bottom <= notice.top) && status.bottom <= notice.top,
        statusFits: status.left >= bounds.left && status.right <= bounds.right,
        titleStatusSeparate: [...titleRange.getClientRects()].every((box) => box.bottom <= status.top || box.top >= status.bottom || box.right <= status.left || box.left >= status.right),
        actionFollowsNotice: action.top >= notice.bottom && action.bottom <= bounds.bottom };
    });
    if (width === 600) assert(geometry.height > geometry.minimum, 'Real warning copy must exercise growth beyond the desktop minimum');
    assert(geometry.contentBottom <= geometry.bottom + 1 && geometry.nextTop >= geometry.contentBottom,
      'The full notice and recovery action must fit before the next Settings section');
    assert(geometry.headerBeforeNotice, 'The heading and summary must finish before the warning');
    assert(geometry.statusFits && geometry.titleStatusSeparate, 'The status must fit the section and remain separate from its title');
    assert(geometry.actionFollowsNotice, 'The recovery action must follow the whole warning and fit the section');
    assert(!geometry.horizontalOverflow, 'Expanded essential copy must not require horizontal scrolling');
    assert.equal(await notice.getByRole('button', { name: 'Show details' }).count(), 0);
  } finally { await page.close(); }
});
