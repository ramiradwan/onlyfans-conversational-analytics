import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { chromium } from 'playwright';
import { waitForPairingResultPresentation } from './pairing-result-completion.mjs';
import { installWatcher, nativeBoundary, readWatcher, settleNativeWatcher } from './shift-watcher.mjs';
import { inspectSettingsFrames } from './settings-stability.mjs';

let browser;
before(async () => { browser = await chromium.launch(); });
after(async () => { await browser?.close(); });

async function fixture() {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await installWatcher(page, { requiredRegions: ['settings'], nativeTimeline: true });
  await page.addInitScript(() => { window.__nativeResultTask = setTimeout.bind(window); });
  await page.goto('data:text/html,' + encodeURIComponent(`
    <style>html{font-size:16px}main{width:300px}#actions{min-height:2.5rem}.active #actions{min-height:5.5rem}</style>
    <main data-reserved-region="settings">
      <section class="active" data-journey-state="desktop.extension_pairing" data-pairing-active="true" data-pairing-path="operator-compared">
        <div id="comparison"><input type="checkbox"><button>Confirm connection</button></div>
        <div data-browser-feedback><div data-reserved-region="browser-feedback"><span role="status">Connecting</span></div><div><div id="actions"><button>Cancel</button></div></div></div>
      </section><div id="following" style="height:40px;background:blue;color:white">Following content</div>
    </main>`));
  await settleNativeWatcher(page);
  await page.clock.install({ time: new Date('2026-06-30T12:05:00Z') });
  return page;
}

const terminal = (page, feedback, delayed = false) => page.evaluate(({ feedback, delayed }) => {
  const owner = document.querySelector('section');
  owner.removeAttribute('data-pairing-active');
  owner.setAttribute('data-pairing-path', 'desktop-task');
  document.querySelector('#comparison').remove();
  document.querySelector('[role="status"]').textContent = feedback;
  document.querySelector('#actions').textContent = '';
  // Keep the old computed rule after terminal DOM, as the real trace did.
  // The delay is controlled solely by the captured native clock.
  if (delayed) window.__nativeResultTask(() => owner.classList.remove('active'), 120);
  else owner.classList.remove('active');
}, { feedback, delayed });

for (const [outcome, feedback] of [
  ['declined', 'Connection declined. Try again.'],
  ['cancelled', 'Connection cancelled.'],
  ['confirmed', 'Extension connected.'],
]) test(`explicit ${outcome} waits for its delayed terminal computed rule`, async () => {
  const page = await fixture();
  try {
    const before = await nativeBoundary(page);
    await terminal(page, feedback, true);
    const completed = await waitForPairingResultPresentation(page, outcome);
    assert(completed.at - before.nativeAt >= 100, 'Terminal text alone must not complete the result');
    assert.equal(completed.minHeight, 40); assert.equal(completed.height, 40);
    await settleNativeWatcher(page);
    const after = await nativeBoundary(page);
    const raw = await readWatcher(page);
    const report = inspectSettingsFrames(raw.frames, [{ id: `pairing:${outcome}:result`, before, after }], ['settings'], raw);
    assert.deepEqual(report.failures, []);
    assert(raw.rawShifts.length >= 1);
    assert(raw.rawShifts.every(shift => shift.at > before.nativeAt && shift.at < after.nativeAt));
    assert.deepEqual(raw.frames.flatMap(frame => frame.shifts), raw.rawShifts);
  } finally { await page.close(); }
});

test('wrong terminal feedback and rejected active result cannot satisfy cancellation', async () => {
  const page = await fixture();
  try {
    await page.evaluate(() => { document.querySelector('[role="status"]').textContent = "The connection couldn't be checked. Try again."; });
    await assert.rejects(waitForPairingResultPresentation(page, 'cancelled', 120), /did not complete/);
    await terminal(page, 'Connection declined. Try again.');
    await assert.rejects(waitForPairingResultPresentation(page, 'cancelled', 120), /did not complete/);
    await assert.rejects(waitForPairingResultPresentation(page, 'refused'), /Unsupported terminal/);
  } finally { await page.close(); }
});

for (const location of ['gap', 'tail']) test(`a later ${location} shift still fails after exact cancellation completion`, async () => {
  const page = await fixture();
  try {
    const before = await nativeBoundary(page);
    await terminal(page, 'Connection cancelled.');
    await waitForPairingResultPresentation(page, 'cancelled');
    await settleNativeWatcher(page);
    const after = await nativeBoundary(page);
    const transitions = [{ id: 'pairing:cancelled:result', before, after }];
    await page.evaluate(() => { document.querySelector('#following').style.marginLeft = '30px'; });
    if (location === 'gap') {
      await settleNativeWatcher(page);
      const stableBefore = await nativeBoundary(page);
      await settleNativeWatcher(page);
      transitions.push({ id: 'activation:accepted', before: stableBefore, after: await nativeBoundary(page) });
    }
    const raw = await readWatcher(page);
    const report = inspectSettingsFrames(raw.frames, transitions, ['settings'], raw);
    assert(report.failures.some(message => message.includes('gap, tail or boundary')));
    assert(report.shiftAttribution.some(shift => shift.classification === 'unattributed' && shift.at > after.nativeAt));
    assert.deepEqual(raw.frames.flatMap(frame => frame.shifts), raw.rawShifts);
  } finally { await page.close(); }
});
