import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { extname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { chromium } from 'playwright';

test('native startup documents expose settled copy and preserve native semantics', async () => {
  const root = new URL('../../desktop/shell/', import.meta.url);
  const files = await Promise.all([
    'index.html', 'unavailable.html', 'data-recovery.html', 'shell.css', 'onboarding-theme.css'
  ].map(async name => [name, await readFile(new URL(name, root), 'utf8')])).then(entries => Object.fromEntries(entries));
  const waiting = files['index.html'];
  const unavailable = files['unavailable.html'];
  const recovery = files['data-recovery.html'];
  const css = files['shell.css'];
  const theme = files['onboarding-theme.css'];
  assert.match(waiting, /<h1 id="startup-title">Opening Conversation Analytics<\/h1>/);
  assert.match(waiting, /role="status">Getting things ready on this computer…<\/p>/);
  assert.match(unavailable, /<h1>Conversation Analytics can’t start right now<\/h1>/);
  assert.match(unavailable, /Quit Conversation Analytics from its icon near the Windows clock, then open it again\./);
  assert.match(recovery, /<h1>Can’t open your saved data<\/h1>/);
  assert.match(recovery, /role="alert"/);
  assert.match(recovery, /Keep your existing data and keys\./);
  assert.match(recovery, /If the problem continues, contact your administrator\./);
  assert.match(waiting, /data-desktop-drag-region/);
  assert.match(waiting, /<desktop-window-controls><\/desktop-window-controls>/);
  assert.match(css, /onboarding-theme\.css/);
  assert.match(css, /prefers-reduced-motion/);
  assert.match(theme, /prefers-color-scheme: dark/);
  assert.match(theme, /inter-latin-opsz-normal\.woff2/);
});

test('native startup first paint uses local assets and fits narrow 2x viewports in both schemes', async () => {
  const root = resolve(fileURLToPath(new URL('../../desktop/shell/', import.meta.url)));
  const seen = [];
  const types = { '.html': 'text/html', '.css': 'text/css', '.woff2': 'font/woff2' };
  const server = createServer(async (request, response) => {
    seen.push(request.url);
    const name = request.url?.slice(1) ?? '';
    if (!['index.html', 'unavailable.html', 'data-recovery.html', 'shell.css', 'onboarding-theme.css', 'inter-latin-opsz-normal.woff2'].includes(name)) {
      response.writeHead(404).end(); return;
    }
    response.setHeader('content-type', types[extname(name)]);
    response.end(await readFile(resolve(root, name)));
  });
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  const browser = await chromium.launch();
  try {
    const address = server.address();
    for (const scheme of ['light', 'dark']) {
      const context = await browser.newContext({ colorScheme: scheme, viewport: { width: 320, height: 640 }, deviceScaleFactor: 2 });
      const page = await context.newPage();
      const errors = [];
      page.on('request', request => { if (new URL(request.url()).origin !== `http://127.0.0.1:${address.port}`) errors.push('external request'); });
      await page.goto(`http://127.0.0.1:${address.port}/index.html`, { waitUntil: 'networkidle' });
      await page.evaluate(() => document.fonts.ready);
      const metrics = await page.evaluate(() => ({
        overflow: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - innerWidth,
        bg: getComputedStyle(document.body).backgroundColor,
        text: getComputedStyle(document.body).color,
        font: document.fonts.check('16px "Inter Variable"'),
        heading: document.querySelector('h1').getBoundingClientRect().height,
        role: document.querySelector('[role="status"]')?.textContent,
      }));
      assert.ok(metrics.overflow <= 1, `${scheme} 320px overflow: ${metrics.overflow}`);
      assert.ok(metrics.font, 'local Inter font must load');
      assert.ok(metrics.bg && metrics.text && metrics.heading > 0);
      assert.equal(metrics.role, 'Getting things ready on this computer…');
      assert.deepEqual(errors, []);
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.goto(`http://127.0.0.1:${address.port}/unavailable.html`, { waitUntil: 'networkidle' });
      const titleLines = await page.locator('h1').evaluate(node => {
        const range = document.createRange(); range.selectNodeContents(node);
        return range.getClientRects().length;
      });
      assert.equal(titleLines, 1, `${scheme} unavailable title must occupy one line at 1440px`);
      await context.close();
    }
    assert.ok(seen.includes('/inter-latin-opsz-normal.woff2'), 'the packaged font asset must be requested');
  } finally {
    await browser.close();
    await new Promise(done => server.close(done));
  }
});

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
