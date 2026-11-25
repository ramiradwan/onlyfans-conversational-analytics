import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { chromium } from 'playwright';

const desktop = new URL('../../desktop/', import.meta.url);
const origin = 'http://bridge.localhost:17871';

test('Get help opens in a signed-out desktop window without sending the review reference or launching a browser', async () => {
  const [frame, help] = await Promise.all(['window-frame.js', 'startup-help.js']
    .map(name => readFile(new URL(name, desktop), 'utf8')));
  const contacts = { telegram: 'https://chat.example.com/support' };
  const reference = 'R'.repeat(43);
  const browser = await chromium.launch();
  try {
    for (const [path, width] of [['/onboarding', 640], ['/', 320]]) {
      const context = await browser.newContext({ viewport: { width, height: 480 } });
      try {
        const page = await context.newPage();
        const url = origin + path;
        const requests = [];
        await page.route('**/*', route => {
          requests.push(route.request().url());
          return route.request().url() === url
            ? route.fulfill({ contentType: 'text/html', body: `<header data-desktop-drag-region><desktop-window-controls></desktop-window-controls></header><main>Sign in. Your review reference is ${reference}</main>` })
            : route.abort();
        });
        await page.addInitScript({ content: `window.__TAURI__ = { core: { invoke: async () => false } };\n${frame}\n(${help})(${JSON.stringify(contacts)});` });
        await page.goto(url);
        const open = page.getByRole('button', { name: 'Get help', exact: true });
        const dialog = page.getByRole('dialog', { name: 'Get help', exact: true });
        await open.focus();
        await page.keyboard.press('Enter');
        await dialog.waitFor();
        assert.deepEqual(await dialog.getByRole('listitem').allTextContents(), ['Telegram ' + contacts.telegram]);
        assert.ok(!(await dialog.innerText()).includes(reference));
        assert.equal(await dialog.locator('a, input, textarea, form').count(), 0);
        const bounds = await dialog.boundingBox();
        assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width && bounds.y + bounds.height <= 480);
        await page.keyboard.press('Escape');
        await dialog.waitFor({ state: 'hidden' });
        await open.click();
        await dialog.getByRole('button', { name: 'Close' }).click();
        await dialog.waitFor({ state: 'hidden' });
        assert.equal(context.pages().length, 1);
        assert.deepEqual(requests, [url]);
      } finally { await context.close(); }
    }

    const browserPage = await browser.newPage();
    await browserPage.route('**/*', route => route.fulfill({ contentType: 'text/html', body: '<main>Sign in</main>' }));
    await browserPage.addInitScript({ content: `(${help})(${JSON.stringify(contacts)});` });
    await browserPage.goto('https://account.example/');
    assert.equal(await browserPage.evaluate(() => Boolean(customElements.get('desktop-help'))), false);
    assert.equal(await browserPage.getByRole('button', { name: 'Get help' }).count(), 0);
  } finally { await browser.close(); }
});
