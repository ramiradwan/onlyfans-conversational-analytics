import { randomUUID } from 'node:crypto';
import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { installWatcher, readWatcher } from './shift-watcher.mjs';
import { runCaptureJobs } from './capture-jobs.mjs';

const date = '2026-06-30T10:00:00.000Z';
const states = [
  ['never_checked', null], ['current', null],
  ...['canary', 'catch_up', 'first_check'].map((reason) => ['checking', reason]),
  ...['awaiting_check', 'daily_cap', 'check_incomplete', 'not_observing'].map((reason) => ['behind', reason]),
  ...['user_paused', 'consent_needed', 'extension_offline', 'no_onlyfans_tab', 'onlyfans_sleeping', 'account_changed', 'applying_settings', 'capture_off', 'extension_outdated', 'unrecognized-reason'].map((reason) => ['paused', reason]),
];
const frames = states.map(([status, reason]) => ({ freshness: { status, reason, uncertain_since: status === 'behind' ? '2025-12-31T21:59:00.000Z' : null, last_closed_at: status === 'never_checked' ? null : date, observing_since: date }, bridge: 'connected', snapshotUsable: true }));
frames.push({ ...frames[2], freshness: { ...frames[2].freshness, last_closed_at: null } });
for (const bridge of ['connecting', 'handshaking', 'disconnected', 'reconnecting', 'error']) frames.push({ ...frames[1], bridge });
frames.push({ ...frames[1], snapshotUsable: false });

const vertices = frames.map((frame, index) => ({ ...frame, id: 'freshness-' + index }));
const matrixVertices = vertices.slice(0, states.length);
export const FRESHNESS_TRANSITIONS = matrixVertices.flatMap((from) => matrixVertices.filter((to) => to.id !== from.id).map((to) => ({ from, to })));

export async function captureDirectedTransition({ from, to }, push, read) {
  await push(from);
  const before = await read();
  await push(to);
  return { from: from.id, to: to.id, before, after: await read() };
}

export async function captureFreshnessTransitions(browser, base, outDir) {
  const reports = [];
  const seed = randomUUID();
  const cases = [];
  for (const width of [390, 1440]) for (const mode of ['light', 'dark']) for (const fontScale of [1, 1.25]) for (const motion of ['reduce', 'no-preference']) {
    cases.push({ width, mode, fontScale, motion });
  }
  await runCaptureJobs(cases, async ({ width, mode, fontScale, motion }) => {
    const viewport = { width, height: width === 390 ? 844 : 900 };
    const page = await browser.newPage({ viewport, colorScheme: mode, reducedMotion: motion, timezoneId: 'Europe/Helsinki' });
    const name = ['freshness', width, mode, fontScale, motion].join('-');
    const transitions = [], overlays = [];
    try {
      await installWatcher(page, { requiredRegions: ['toolbar', ...(width === 390 ? ['status-row'] : []), 'freshness-status', 'following-content'] });
      await page.addInitScript((scale) => { new MutationObserver(() => { if (document.documentElement && !document.documentElement.style.fontSize) document.documentElement.style.fontSize = (16 * scale) + 'px'; }).observe(document, { childList: true, subtree: true }); }, fontScale);
      await page.goto(base + '?stability=freshness&mode=' + mode, { waitUntil: 'networkidle' });
      await page.clock.install({ time: new Date('2026-06-30T12:05:00.000Z') });
      const push = async (frame) => {
        const detail = structuredClone(frame);
        if (detail.freshness.reason === 'unrecognized-reason') detail.freshness.reason = seed + 'x'.repeat(4096) + ' 界文字 '.repeat(16);
        await page.evaluate((detail) => window.dispatchEvent(new CustomEvent('freshness-fixture', { detail })), detail);
        await page.clock.fastForward(850);
        await page.clock.runFor(220);
      };
      for (const { from, to } of FRESHNESS_TRANSITIONS) {
        const record = await captureDirectedTransition({ from, to }, push,
          () => page.evaluate(() => ({ at: performance.now(), boxes: window.__regionWatcher.frames.at(-1)?.regions })));
        const label = await page.locator('[data-reserved-region="freshness-status"]:visible').innerText();
        const canBeCurrent = to.snapshotUsable && to.bridge === 'connected' && (to.freshness.status === 'current' || (to.freshness.status === 'checking' && to.freshness.reason === 'canary' && to.freshness.last_closed_at !== null));
        assert.equal(label === 'Up to date', canBeCurrent);
        transitions.push(record);
      }
      for (const frame of vertices) {
        const before = await page.evaluate(() => ({ at: performance.now(), boxes: window.__regionWatcher.frames.at(-1)?.regions }));
        await push(frame);
        await page.getByRole('button', { name: /^Status:/ }).click();
        const dialog = page.getByRole('dialog', { name: 'Status details' });
        assert(await dialog.isVisible());
        assert((await dialog.boundingBox()).width <= Math.min(320, width - 32));
        await page.keyboard.press('Escape');
        await page.clock.runFor(250);
        overlays.push({ state: frame.id, before, after: await page.evaluate(() => ({ at: performance.now(), boxes: window.__regionWatcher.frames.at(-1)?.regions })) });
      }
      const report = { revision: process.env.VISUAL_CAPTURE_REVISION ?? null, view: 'freshness', viewport, mode, fontScale, motion, seed, expected: FRESHNESS_TRANSITIONS.length, transitions, overlays, ...await readWatcher(page) };
      await writeFile(join(outDir, name + '.json'), JSON.stringify(report) + '\n');
      reports.push({ file: name + '.json', transitions: transitions.length, failures: report.failures });
      assert.equal(transitions.length, FRESHNESS_TRANSITIONS.length);
      assert.deepEqual(report.failures, []);
    } catch (error) {
      await page.screenshot({ path: join(outDir, name + '-failure.png') });
      await writeFile(join(outDir, name + '-failure.json'), JSON.stringify({ revision: process.env.VISUAL_CAPTURE_REVISION ?? null, view: 'freshness', viewport, mode, fontScale, motion, seed, transitions, overlays, ...await readWatcher(page) }) + '\n');
      if (!reports.some((report) => report.file === name + '.json')) reports.push({ file: name + '-failure.json', transitions: transitions.length, failures: [error.message] });
    } finally { await page.close(); }
  });
  assert.equal(reports.length, cases.length, 'Missing freshness configuration');
  reports.sort((first, second) => first.file.localeCompare(second.file));
  await writeFile(join(outDir, 'freshness-transitions.json'), JSON.stringify(reports, null, 2) + '\n');
  return reports;
}
