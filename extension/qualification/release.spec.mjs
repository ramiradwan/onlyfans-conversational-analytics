import { test, expect, chromium } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

test('extracted release starts disabled, records UI choices and deletes without a companion', async () => {
  if (!process.env.EXTENSION_ARTIFACT_DIR) throw new Error('EXTENSION_ARTIFACT_DIR is required');
  const directory = path.resolve(process.env.EXTENSION_ARTIFACT_DIR);
  const manifest = JSON.parse(await readFile(path.join(directory, 'manifest.json'), 'utf8'));
  const context = await chromium.launchPersistentContext('', {
    ...(process.env.OFCA_CHROMIUM ? { executablePath: process.env.OFCA_CHROMIUM } : { channel: 'chromium' }),
    headless: process.env.OFCA_HEADLESS === '1',
    args: [`--disable-extensions-except=${directory}`, `--load-extension=${directory}`],
  });
  try {
    const worker = context.serviceWorkers()[0] ?? await context.waitForEvent('serviceworker');
    const extensionId = new URL(worker.url()).hostname;
    const session = await context.newCDPSession(context.pages()[0]);
    const version = await session.send('Browser.getVersion');
    const major = Number(/(?:Chrome|Chromium)\/(\d+)/.exec(version.product)?.[1]);
    if (process.env.OFCA_BROWSER_LABEL === 'minimum') expect(major).toBe(132);
    else expect(major).toBeGreaterThan(132);
    const requests = [];
    context.on('request', (request) => {
      if (/^(https?:|wss?:)/.test(request.url())) requests.push(new URL(request.url()).origin);
    });
    const popup = await context.newPage();
    await popup.goto(`chrome-extension://${extensionId}/popup.html`);
    await expect(popup.locator('#legal-unavailable')).toBeHidden();
    await expect(popup.locator('#mode-label')).toHaveText('Analytics off — no OnlyFans access');
    await expect(popup.locator('#pre-mode')).toBeVisible();
    const access = () => worker.evaluate(() => chrome.permissions.getAll());
    expect((await access()).origins ?? []).toEqual([]);
    await popup.locator('#terms-accepted').check();
    await popup.locator('#risk-acknowledged').check();
    await popup.locator('#activate-software').click();
    await expect(popup.locator('#mode-choice')).toBeVisible();
    await popup.locator('#not-now-preview').click();
    await expect(popup.locator('#mode-label')).toHaveText('Analytics off — no OnlyFans access');
    expect((await access()).origins ?? []).toEqual([]);
    const manage = popup.locator('#manage-extension');
    if (!(await manage.evaluate((element) => element.open))) await manage.locator('summary').click();
    popup.once('dialog', (dialog) => dialog.accept());
    await popup.locator('#delete-local-data').click();
    await expect(popup.locator('#feedback')).toHaveText('All local extension data was deleted.');
    await expect(popup.locator('#pre-mode')).toBeVisible();
    // Reaccept in the same worker through the actual UI after closing IndexedDB.
    await popup.locator('#terms-accepted').check();
    await popup.locator('#risk-acknowledged').check();
    await popup.locator('#activate-software').click();
    await expect(popup.locator('#mode-choice')).toBeVisible();
    await popup.reload();
    await expect(popup.locator('#mode-choice')).toBeVisible();
    await expect(popup.locator('#mode-label')).toHaveText('Analytics off — no OnlyFans access');
    expect(requests).toEqual([]);
    const report = {
      label: process.env.OFCA_BROWSER_LABEL ?? 'current',
      product: version.product,
      revision: version.revision,
      extension_id: extensionId,
      minimum_chrome_version: manifest.minimum_chrome_version,
      result: 'passed',
      scenarios: ['fresh_install_off', 'legal_ui_choices', 'no_http_during_popup_actions', 'delete_reaccept_same_worker'],
    };
    if (process.env.OFCA_BROWSER_REPORT) {
      await writeFile(process.env.OFCA_BROWSER_REPORT, `${JSON.stringify(report, null, 2)}\n`);
    }
  } finally {
    await context.close();
  }
});
