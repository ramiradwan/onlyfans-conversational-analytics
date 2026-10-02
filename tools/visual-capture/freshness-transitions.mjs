import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { installWatcher, assertWatcher } from './shift-watcher.mjs';

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

export async function captureFreshnessTransitions(browser, base, outDir) {
  const reports = [];
  for (const width of [390, 1440]) {
    const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce', timezoneId: 'Europe/Helsinki' });
    try {
      await installWatcher(page);
      await page.goto(`${base}?stability=freshness&mode=light`, { waitUntil: 'networkidle' });
      await page.evaluate(() => document.fonts.ready);
      await page.clock.install({ time: new Date('2026-06-30T12:05:00.000Z') });
      for (const frame of frames) {
        await page.evaluate((detail) => window.dispatchEvent(new CustomEvent('freshness-fixture', { detail })), frame);
        await page.clock.runFor(850);
        await assertWatcher(page);
        const label = await page.locator('[data-reserved-region="freshness-status"]:visible').innerText();
        const canBeCurrent = frame.snapshotUsable && frame.bridge === 'connected' && (frame.freshness.status === 'current' || (frame.freshness.status === 'checking' && frame.freshness.reason === 'canary' && frame.freshness.last_closed_at !== null));
        assert.equal(label === 'Up to date', canBeCurrent);
        await page.getByRole('button', { name: /^Status:/ }).click();
        const dialog = page.getByRole('dialog', { name: 'Status details' });
        assert(await dialog.isVisible());
        const box = await dialog.boundingBox();
        assert(box.width <= Math.min(320, width - 32));
        await page.keyboard.press('Escape');
        await page.clock.runFor(250);
      }
      const report = await assertWatcher(page);
      reports.push({ width, states: frames.length, ...report });
      await page.screenshot({ path: join(outDir, `freshness-${width}.png`) });
    } finally { await page.close(); }
  }
  await writeFile(join(outDir, 'freshness-transitions.json'), JSON.stringify(reports, null, 2) + '\n');
  return reports;
}
