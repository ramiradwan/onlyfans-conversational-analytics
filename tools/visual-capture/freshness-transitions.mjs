import { randomUUID } from 'node:crypto';
import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { installWatcher, readWatcher } from './shift-watcher.mjs';
import { runCaptureJobs } from './capture-jobs.mjs';

import { FRESHNESS_TRANSITIONS, FRESHNESS_VERTICES, freshnessCases } from './freshness-matrix.mjs';
export { FRESHNESS_TRANSITIONS } from './freshness-matrix.mjs';

export async function captureDirectedTransition({ from, to }, push, read) {
  await push(from);
  const before = await read();
  await push(to);
  return { from: from.id, to: to.id, before, after: await read() };
}

export async function captureFreshnessTransitions(browser, base, outDir, recorder = null) {
  const reports = [];
  const seed = randomUUID();
  const cases = freshnessCases();
  await runCaptureJobs(cases, async ({ width, mode, fontScale, motion }) => {
    const name = ['freshness', width, mode, fontScale, motion].join('-');
    const finish = recorder?.begin(name, { width, mode, fontScale, motion });
    const viewport = { width, height: width === 390 ? 844 : 900 };
    const page = await browser.newPage({ viewport, colorScheme: mode, reducedMotion: motion, timezoneId: 'Europe/Helsinki' });
    let caseFailed = false;
    const files = [];
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
        await page.clock.fastForward(220);
        await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
      };
      for (const { from, to } of FRESHNESS_TRANSITIONS) {
        const record = await captureDirectedTransition({ from, to }, push,
          () => page.evaluate(() => ({ at: performance.now(), boxes: window.__regionWatcher.frames.at(-1)?.regions })));
        const label = await page.locator('[data-reserved-region="freshness-status"]:visible').innerText();
        const canBeCurrent = to.snapshotUsable && to.bridge === 'connected' && (to.freshness.status === 'current' || (to.freshness.status === 'checking' && to.freshness.reason === 'canary' && to.freshness.last_closed_at !== null));
        assert.equal(label === 'Up to date', canBeCurrent);
        transitions.push(record);
      }
      for (const frame of FRESHNESS_VERTICES) {
        const before = await page.evaluate(() => ({ at: performance.now(), boxes: window.__regionWatcher.frames.at(-1)?.regions }));
        await push(frame);
        await page.getByRole('button', { name: /^Status:/ }).click();
        const dialog = page.getByRole('dialog', { name: 'Status details' });
        assert(await dialog.isVisible());
        assert((await dialog.boundingBox()).width <= Math.min(320, width - 32));
        await page.keyboard.press('Escape');
        await page.clock.fastForward(250);
        overlays.push({ state: frame.id, before, after: await page.evaluate(() => ({ at: performance.now(), boxes: window.__regionWatcher.frames.at(-1)?.regions })) });
      }
      const report = { revision: process.env.VISUAL_CAPTURE_REVISION ?? null, view: 'freshness', viewport, mode, fontScale, motion, seed, expected: FRESHNESS_TRANSITIONS.length, transitions, overlays, ...await readWatcher(page) };
      await writeFile(join(outDir, name + '.json'), JSON.stringify(report) + '\n');
      files.push(name + '.json');
      reports.push({ file: name + '.json', transitions: transitions.length, overlays: overlays.length, failures: report.failures });
      assert.equal(transitions.length, FRESHNESS_TRANSITIONS.length);
      assert.deepEqual(report.failures, []);
    } catch (error) {
      caseFailed = true;
      await page.screenshot({ path: join(outDir, name + '-failure.png') });
      await writeFile(join(outDir, name + '-failure.json'), JSON.stringify({ revision: process.env.VISUAL_CAPTURE_REVISION ?? null, view: 'freshness', viewport, mode, fontScale, motion, seed, transitions, overlays, ...await readWatcher(page) }) + '\n');
      if (!reports.some((report) => report.file === name + '.json')) reports.push({ file: name + '-failure.json', transitions: transitions.length, failures: [error.message] });
    } finally {
      finish?.({ outcome: caseFailed ? 'failed' : 'passed', observations: [...transitions.map(({ from, to }) => `edge:${from}:${to}`), ...overlays.map(({ state }) => `overlay:${state}`)], files });
      await page.close();
    }
  });
  assert.equal(reports.length, cases.length, 'Missing freshness configuration');
  reports.sort((first, second) => first.file.localeCompare(second.file));
  await writeFile(join(outDir, 'freshness-transitions.json'), JSON.stringify(reports, null, 2) + '\n');
  return reports;
}
