import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { chromium } from 'playwright';
import { installWatcher, nativeBoundary, readWatcher, settleNativeWatcher } from './shift-watcher.mjs';
import { inspectSettingsFrames } from './settings-stability.mjs';

let browser;
before(async () => { browser = await chromium.launch(); });
after(async () => { await browser?.close(); });

// Delay actual browser entries, not invented scores or modified timestamps.
function delayedObserverFixture() {
  const NativeObserver = PerformanceObserver;
  const pending = [];
  window.__shiftDelivery = { hold: true, count: () => pending.length,
    release() { this.hold = false; for (const item of pending.splice(0)) item.invoke(); } };
  window.PerformanceObserver = class {
    static supportedEntryTypes = NativeObserver.supportedEntryTypes;
    constructor(callback) {
      this.callback = callback;
      this.observer = new NativeObserver(list => this.deliver(list.getEntries()));
    }
    observe(options) { this.type = options.type; this.observer.observe(options); }
    disconnect() { this.observer.disconnect(); }
    deliver(entries) {
      if (!entries.length) return;
      const invoke = () => this.callback({ getEntries: () => entries });
      if (this.type === 'layout-shift' && window.__shiftDelivery.hold) pending.push({ observer: this, entries, invoke }); else invoke();
    }
    takeRecords() {
      const entries = this.observer.takeRecords();
      if (this.type === 'layout-shift' && window.__shiftDelivery.hold) { this.deliver(entries); return []; }
      const retained = pending.filter(item => item.observer === this);
      for (const item of retained) pending.splice(pending.indexOf(item), 1);
      return [...retained.flatMap(item => item.entries), ...entries];
    }
  };
}

async function fixture({ delayed = false } = {}) {
  const page = await browser.newPage({ viewport: { width: 500, height: 400 } });
  if (delayed) await page.addInitScript(delayedObserverFixture);
  await installWatcher(page, { requiredRegions: ['settings'], nativeTimeline: true });
  await page.goto('data:text/html,' + encodeURIComponent('<main data-reserved-region="settings" style="width:300px;height:120px"><div id="subject" style="width:80px;height:30px;background:blue;color:white">Probe</div></main>'));
  await settleNativeWatcher(page);
  await page.clock.install({ time: new Date('2026-06-30T12:05:00Z') });
  const transitions = [];
  const step = async (id, action = async () => {}) => {
    const before = await nativeBoundary(page);
    await action();
    await settleNativeWatcher(page);
    transitions.push({ id, before, after: await nativeBoundary(page) });
  };
  const finish = async () => {
    const report = await readWatcher(page);
    return { ...report, ...inspectSettingsFrames(report.frames, transitions, ['settings'], report) };
  };
  return { page, step, finish, transitions };
}
const move = (page, left) => page.evaluate(left => { document.querySelector('#subject').style.marginLeft = left + 'px'; }, left);

test('native clock survives virtual fast-forward and brackets real browser layout entries', { timeout: 15_000 }, async () => {
  const { page, step, finish, transitions } = await fixture();
  try {
    const before = await nativeBoundary(page);
    await page.clock.fastForward(300_000);
    const after = await nativeBoundary(page);
    assert(after.at - before.at >= 300_000);
    assert(after.nativeAt - before.nativeAt < 10_000);
    await step('activation:review', () => move(page, 30));
    const report = await finish();
    assert.deepEqual(report.failures, []);
    assert(report.rawShifts.length > 0, 'Real Chromium must emit the positive control');
    const { before: start, after: end } = transitions[0];
    for (const shift of report.rawShifts) {
      assert(shift.at > start.nativeAt && shift.at < end.nativeAt, 'Native occurrence must share the action clock domain');
      assert(shift.deliveredAt >= shift.at);
    }
    assert.equal(report.shiftAttribution.length, report.rawShifts.length);
    assert.deepEqual(report.frames.flatMap(frame => frame.shifts), report.rawShifts);
  } finally { await page.close(); }
});

for (const passive of [false, true]) test(`real delayed ${passive ? 'passive shift fails during later content' : 'content shift belongs to its earlier action'}`, { timeout: 15_000 }, async () => {
  const { page, step, finish, transitions } = await fixture({ delayed: true });
  try {
    await step(passive ? 'activation:uncertain' : 'activation:review', () => move(page, 30));
    assert(await page.evaluate(() => window.__shiftDelivery.count()) > 0, 'The fixture must hold actual browser entries');
    await step(passive ? 'activation:review' : 'activation:uncertain', () => page.evaluate(() => window.__shiftDelivery.release()));
    const report = await finish();
    assert(report.rawShifts.length > 0);
    assert(report.rawShifts.every(entry => entry.at < transitions[1].before.nativeAt && entry.deliveredAt > transitions[1].before.nativeAt));
    assert(report.shiftAttribution.every(entry => entry.window.id === transitions[0].id));
    if (passive) assert.deepEqual(report.failures, ['activation:uncertain: layout shift after first paint']);
    else assert.deepEqual(report.failures, []);
    assert.deepEqual(report.frames.flatMap(frame => frame.shifts), report.rawShifts);
  } finally { await page.close(); }
});

test('real internal movement away and back fails despite identical reserved boxes', { timeout: 15_000 }, async () => {
  const { page, step, finish, transitions } = await fixture();
  try {
    await step('activation:uncertain', async () => {
      await move(page, 30); await settleNativeWatcher(page);
      await move(page, 0); await settleNativeWatcher(page);
    });
    const report = await finish();
    assert.deepEqual(transitions[0].before.boxes, transitions[0].after.boxes);
    assert(report.rawShifts.length >= 2, 'Both painted directions must remain in raw evidence');
    assert.deepEqual(report.failures, ['activation:uncertain: layout shift after first paint']);
  } finally { await page.close(); }
});

for (const location of ['gap', 'tail']) test(`real uninstrumented ${location} shift fails`, { timeout: 15_000 }, async () => {
  const { page, step, finish } = await fixture();
  try {
    await step('activation:review');
    await move(page, 30);
    if (location === 'gap') { await settleNativeWatcher(page); await step('activation:accepted'); }
    const report = await finish();
    assert(report.rawShifts.length > 0);
    assert(report.failures.some(value => value.includes('gap, tail or boundary')));
    assert(report.shiftAttribution.every(entry => entry.classification === 'unattributed'));
  } finally { await page.close(); }
});

test('final takeRecords drain retains real entries withheld from callback delivery', { timeout: 15_000 }, async () => {
  const { page, step, finish } = await fixture({ delayed: true });
  try {
    await step('activation:uncertain', () => move(page, 30));
    assert(await page.evaluate(() => window.__shiftDelivery.count()) > 0);
    const drainStart = await nativeBoundary(page);
    await page.evaluate(() => { window.__shiftDelivery.hold = false; });
    const report = await finish();
    assert(report.rawShifts.length > 0);
    assert(report.rawShifts.every(entry => entry.deliveredAt > drainStart.nativeAt));
    assert.deepEqual(report.failures, ['activation:uncertain: layout shift after first paint']);
    assert.equal(await page.evaluate(() => window.__shiftDelivery.count()), 0);
    assert.deepEqual(report.frames.flatMap(frame => frame.shifts), report.rawShifts);
  } finally { await page.close(); }
});

test('watcher installed after a mocked clock refuses native qualification', { timeout: 15_000 }, async () => {
  const page = await browser.newPage();
  try {
    await page.clock.install();
    // Install in the current document so the ordering violation is deliberate.
    const { observeRegions } = await import('./shift-watcher.mjs');
    await page.evaluate(observeRegions, { nativeTimeline: true });
    await assert.rejects(nativeBoundary(page), /Native observation timeline is unavailable/);
  } finally { await page.close(); }
});
