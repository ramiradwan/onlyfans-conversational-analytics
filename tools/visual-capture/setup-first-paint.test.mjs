import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { chromium } from 'playwright';

for (const selected of ['dark', 'light']) {
  test(`saved ${selected} setup theme selects the canvas before CSS arrives`, async () => {
    const browser = await chromium.launch();
    let release;
    const delayedStyles = new Promise(resolve => { release = resolve; });
    try {
      const context = await browser.newContext({ colorScheme: selected === 'dark' ? 'light' : 'dark' });
      const page = await context.newPage();
      await page.addInitScript(theme => localStorage.setItem('mui-mode', theme), selected);
      const document = await readFile(new URL('../../app/static/dist/onboarding/index.html', import.meta.url), 'utf8');
      const initializer = await readFile(new URL('../../frontend/public/setup-theme-init.js', import.meta.url), 'utf8');
      await page.route('http://127.0.0.1:17899/**', async route => {
        const path = new URL(route.request().url()).pathname;
        if (path === '/onboarding') return route.fulfill({ contentType: 'text/html', body: document });
        if (path === '/static/dist/setup-theme-init.js') return route.fulfill({ contentType: 'text/javascript', body: initializer });
        if (path.endsWith('.css')) await delayedStyles;
        return route.fulfill({ contentType: path.endsWith('.css') ? 'text/css' : 'text/javascript', body: '' });
      });
      await page.goto('http://127.0.0.1:17899/onboarding', { waitUntil: 'commit' });
      await page.waitForFunction(theme => document.documentElement.dataset.colorScheme === theme, selected);
      const early = await page.evaluate(() => ({
        scheme: document.querySelector('meta[name="color-scheme"]').content,
        stylesheets: document.styleSheets.length,
      }));
      assert.equal(early.stylesheets, 0, 'check must precede stylesheet arrival');
      assert.equal(early.scheme, selected, 'browser canvas must use the saved theme before CSS');
    } finally {
      release();
      await browser.close();
    }
  });
}
