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
      await page.locator('#confirm-identity').click();
      await page.evaluate(() => { window.__provisioningFixture.hold.push('acquire', 'finalize'); window.dispatchEvent(new Event('focus')); });
      await settle(page);
      await page.evaluate(() => window.__provisioningFixture.release('acquire'));
      await settle(page);
      await page.evaluate(() => window.__provisioningFixture.release('finalize'));
      await settle(page);
      assert(await page.getByRole('heading', { name: 'Setup finished' }).isVisible(), 'Completion is blank');
      assert(await page.locator('#finalize-step-description').isVisible());
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
      await page.locator('#recovery-open').click();
      await page.keyboard.press('Tab');
      await page.locator('#recovery-close').focus();
      await page.locator('#recovery-close').blur();
      await settle(page);
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
