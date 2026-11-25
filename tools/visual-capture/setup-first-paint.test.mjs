import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { chromium } from 'playwright';
import { onboardingStaticAssets, onboardingStaticFixtures } from './onboarding-static-fixtures.mjs';

const supportChannels = [
  ['whatsapp', 'WhatsApp', 'https://msg.example.com/setup'],
  ['telegram', 'Telegram', 'https://chat.example.com/setup-help-with-a-long-address/a&b'],
  ['tiktok', 'TikTok', 'https://video.example.com/setup'],
];
const supportProjection = Object.fromEntries(supportChannels.map(([key, , address]) => [key, address]));

async function supportGeometry(page, title, body, status) {
  return page.evaluate(({ title, body, status }) => {
    const measure = selector => {
      const node = selector && document.querySelector(selector);
      if (!node || !node.textContent.trim()) return null;
      const range = document.createRange();
      range.selectNodeContents(node);
      return { rect: node.getBoundingClientRect().toJSON(),
        lines: new Set([...range.getClientRects()].map(rect => rect.top)).size };
    };
    return { title: measure(title), body: measure(body), status: measure(status),
      header: document.querySelector('header').getBoundingClientRect().toJSON(),
      overflow: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth,
        document.body.scrollWidth - document.documentElement.clientWidth) };
  }, { title, body, status });
}

test('retained data recovery and terminal support fit the title column with native Close', async () => {
  const desktop = new URL('../../desktop/', import.meta.url);
  const source = await readFile(new URL('startup-help.js', desktop), 'utf8');
  const windowFrame = await readFile(new URL('window-frame.js', desktop), 'utf8');
  const shell = Object.fromEntries(await Promise.all([
    'index.html', 'data-recovery.html', 'unavailable.html', 'shell.css',
    'onboarding-theme.css', 'inter-latin-opsz-normal.woff2',
  ].map(async name => ['/' + name, {
    body: await readFile(new URL('shell/' + name, desktop)),
    contentType: { '.html': 'text/html', '.css': 'text/css', '.woff2': 'font/woff2' }[extname(name)],
  }])));
  const staticAssets = await onboardingStaticAssets();
  const fixtures = await onboardingStaticFixtures();
  const screenshotDirectory = process.env.OFCA_TERMINAL_HELP_SCREENSHOTS;
  if (screenshotDirectory) await mkdir(screenshotDirectory, { recursive: true });
  const recoveryMeasurements = [];
  const browser = await chromium.launch();
  const routes = [
    { name: 'native-data-recovery', path: '/data-recovery.html', origin: 'http://tauri.localhost', shell: true },
    { name: 'native-unavailable', path: '/unavailable.html', origin: 'http://tauri.localhost', shell: true },
    { ...fixtures.find(f => f.surface === 'desktop-onboarding'), path: '/onboarding/index.html',
      name: 'desktop-unavailable', origin: 'http://setup-fixture.test', state: { snapshot: null, connected: false, error: 'invalid_snapshot' } },
    ...['invariant_failed', 'authorization_refused'].map(recovery => ({
      ...fixtures.find(f => f.surface === 'browser-task'), name: `chrome-${recovery}`, path: '/browser-tasks/index.html',
      origin: 'http://setup-fixture.test', state: { state: 'service_unavailable', recovery, payload: null },
    })),
  ];
  try {
    for (const colorScheme of ['light', 'dark']) {
      for (const [width, scale] of [[1440, 1], [1440, 2], [640, 1], [390, 1], [390, 2], [320, 1], [320, 2]]) {
        const context = await browser.newContext({ colorScheme, viewport: { width, height: width === 640 ? 480 : 810 } });
        try {
          for (const route of routes) {
            if (!route.shell && (width === 640 || (width === 1440 && scale === 2))) continue;
            const browserTask = route.surface === 'browser-task';
            const anchors = [];
            for (const configured of [false, true]) {
              const showHelp = configured && !browserTask;
              const page = await context.newPage();
              const otherRequests = [];
              await page.addInitScript(({ size, state, native }) => {
                if (native) {
                  window.nativeCalls = [];
                  window.nativeShifts = [];
                  window.__TAURI__ = { core: { invoke: async (command, args) => {
                    window.nativeCalls.push([command, args.action]);
                    return false;
                  } } };
                  new PerformanceObserver(list => {
                    window.nativeShifts.push(...list.getEntries().filter(entry => !entry.hadRecentInput && entry.value > 0));
                  }).observe({ type: 'layout-shift', buffered: true });
                  function sampleFrame() {
                    const header = document.querySelector('.setup-header');
                    if (!header || getComputedStyle(header).display !== 'flex') {
                      requestAnimationFrame(sampleFrame);
                      return;
                    }
                    const root = header.querySelector('desktop-window-controls')?.shadowRoot;
                    window.firstNativeFrame = [...(root?.querySelectorAll('button') ?? [])]
                      .map(button => button.getAttribute('aria-label'));
                  }
                  requestAnimationFrame(sampleFrame);
                }
                window.__staticSurfaceFixture = state;
                document.addEventListener('DOMContentLoaded', () => { document.documentElement.style.fontSize = size + 'px'; });
              }, { size: scale * 16, state: route.state ?? null, native: route.shell ?? false });
              if (route.shell) {
                await page.addInitScript({ content: windowFrame });
                await page.addInitScript({ content: `(${source})(${JSON.stringify(configured ? supportProjection : {})});` });
              }
              await page.route('**/*', request => {
                const url = new URL(request.request().url());
                const path = url.pathname;
                if (url.origin === route.origin) {
                  if (route.shell && shell[path]) return request.fulfill(shell[path]);
                  if (!route.shell && path === route.path) {
                    let body = route.html;
                    if (configured) {
                      const template = '<template id="support-channels">';
                      const entries = supportChannels.map(([key, , address]) =>
                        '<span data-channel="' + key + '">' + address.replaceAll('&', '&amp;') + '</span>').join('');
                      body = body.includes(template) ? body.replace(template, template + entries)
                        : body.replace('</body>', template + entries + '</template></body>');
                    }
                    return request.fulfill({ contentType: 'text/html', body });
                  }
                  if (path === `/${route.surface}.js`) return request.fulfill({ contentType: 'text/javascript', body: route.script });
                  if (staticAssets[path]) return request.fulfill(staticAssets[path]);
                }
                otherRequests.push(url.origin);
                return request.abort();
              });
              await page.goto(route.origin + route.path);
              await page.evaluate(async () => {
                await document.fonts.ready;
                const arriving = document.getAnimations().filter(animation => Number.isFinite(animation.effect.getComputedTiming().endTime));
                await Promise.all(arriving.map(animation => animation.finished));
              });
              const help = page.locator('[data-support-help]');
              assert.equal(await help.count(), Number(showHelp), route.path);
              if (showHelp) {
                assert.equal(await help.locator('h2').textContent(), 'Get help');
                assert.deepEqual(await help.getByRole('listitem').allTextContents(),
                  supportChannels.map(([, label, address]) => label + ' ' + address));
                assert.equal(await help.locator('a, button, input').count(), 0);
                assert.deepEqual(await help.evaluate(element => {
                  const title = document.querySelector('h1').getBoundingClientRect();
                  const heading = element.querySelector('h2');
                  return {
                    weight: getComputedStyle(heading).fontWeight,
                    markers: getComputedStyle(element.querySelector('ul')).listStyleType,
                    offsets: [heading, ...element.querySelectorAll('li')].map(node => node.getBoundingClientRect().left - title.left),
                    selection: [...element.querySelectorAll('li span')].map(node => getComputedStyle(node).userSelect),
                  };
                }), { weight: '600', markers: 'none', offsets: [0, 0, 0, 0], selection: ['text', 'text', 'text'] }, route.name);
              }
              const position = await supportGeometry(page, 'h1', route.shell ? '.startup-card > p' : '#task-description',
                route.surface === 'browser-task' ? '#task-message' : '#task-status');
              if (browserTask) {
                const actionSlot = page.locator('.task-actions-slot > [data-region-content]');
                assert.equal((await actionSlot.boundingBox()).height, 0);
                assert.equal(await actionSlot.locator('a:visible, button:visible').count(), 0);
                assert.equal(await page.locator('#desktop-return').getAttribute('href'), null);
                assert.equal(await page.locator('h1').textContent(), 'Continue in the desktop app');
                assert.equal(await page.locator('#task-message').textContent(), route.state.recovery === 'invariant_failed'
                  ? 'This step couldn’t finish.' : 'This page can’t continue setup.');
                assert.equal(page.url(), route.origin + route.path);
                assert.equal(context.pages().length, 1);
              }
              assert.equal(position.overflow, 0, route.path);
              if (width === 1440 && scale === 1) {
                assert.equal(position.title.lines, 1);
                assert.ok((position.body?.lines ?? 0) <= 2, route.path);
                assert.ok((position.status?.lines ?? 0) <= 1, route.path);
              }
              if (route.shell) {
                await page.waitForFunction(() => window.firstNativeFrame !== undefined);
                assert.deepEqual(await page.evaluate(() => window.firstNativeFrame),
                  ['Minimize', 'Maximize', 'Close window'], route.path);
                const shifts = await page.evaluate(() => window.nativeShifts.map(entry => ({
                  value: entry.value,
                  sources: entry.sources.map(source => source.node?.className || source.node?.nodeName),
                })));
                assert.deepEqual(shifts, [], `${route.path} ${colorScheme} ${width} ${scale}x help=${configured}`);
                const close = page.getByRole('button', { name: 'Close window', exact: true });
                const { x, y, width: targetWidth, height: targetHeight } = await close.boundingBox();
                assert.deepEqual([targetWidth, targetHeight], [44, 40]);
                assert.equal(x + targetWidth, width - (width <= 600 ? 8 : 24));
                assert.equal(y, (position.header.height - targetHeight - 1) / 2);
                if (scale === 1 || width <= 600) assert.equal(position.header.height, 72);
                assert.equal(await page.evaluate(() => document.fonts.check('16px "Inter Variable"')), true);
                assert.deepEqual(await page.evaluate(() => window.nativeCalls.filter(([, action]) => action !== 'state')), []);
                for (const name of ['Minimize', 'Maximize', 'Close window']) {
                  await page.keyboard.press('Tab');
                  assert.equal(await page.getByRole('button', { name, exact: true }).evaluate(button =>
                    button === button.getRootNode().activeElement && button.matches(':focus-visible')), true, name);
                }
                await page.keyboard.press('Enter');
                assert.deepEqual(await page.evaluate(() => window.nativeCalls.filter(([, action]) => action !== 'state')),
                  [['desktop_window', 'close']]);
                assert.equal(page.url(), route.origin + route.path);
                assert.equal(context.pages().length, 1);
              }
              if (showHelp) {
                await help.scrollIntoViewIfNeeded();
                if (route.shell) {
                  assert.ok(await help.evaluate(node =>
                    node.scrollWidth === node.clientWidth && node.scrollHeight === node.clientHeight), route.path);
                  for (const row of await help.getByRole('listitem').all()) {
                    await row.scrollIntoViewIfNeeded();
                    assert.ok(await row.evaluate(node => {
                      const range = document.createRange();
                      range.selectNodeContents(node);
                      return [...range.getClientRects()].every(rect => rect.left >= 0 &&
                        rect.right <= innerWidth && rect.top >= 0 && rect.bottom <= innerHeight);
                    }), route.path);
                  }
                }
              }
              if (route.path === '/data-recovery.html') {
                recoveryMeasurements.push({
                  colorScheme, width, scale, configured, ...position, help: configured ? await help.boundingBox() : null,
                  close: await page.getByRole('button', { name: 'Close window', exact: true }).boundingBox(),
                });
              }
              if (screenshotDirectory && (configured || route.path === '/data-recovery.html')) {
                const suffix = configured ? '' : '-no-help';
                await page.screenshot({ path: join(screenshotDirectory, `${route.name}-${colorScheme}-${width}-${scale}x${suffix}.png`), fullPage: true });
              }
              if (route.shell && configured && width === 1440) {
                await page.goto(route.origin + route.path + '?unsupported=1');
                assert.equal(await help.count(), 0);
                assert.equal(await page.getByRole('button', { name: 'Close window', exact: true }).count(), 0);
                await page.goto(route.origin + '/index.html');
                assert.equal(await help.count(), 0);
              }
              assert.deepEqual(otherRequests, []);
              anchors.push(position);
              await page.close();
            }
            assert.deepEqual(anchors[1], anchors[0], route.path);
          }
        } finally { await context.close(); }
      }
    }
  } finally { await browser.close(); }
  if (screenshotDirectory) {
    await writeFile(join(screenshotDirectory, 'recovery-measurements.json'), JSON.stringify(recoveryMeasurements, null, 2) + '\n');
  }
});

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
  assert.match(waiting, /role="status"[^>]*>Getting things ready on this computer…<\/p>/);
  assert.match(unavailable, /<h1>Conversation Analytics can’t start right now<\/h1>/);
  assert.doesNotMatch(unavailable, /<p>|system tray|open it again/);
  assert.match(recovery, /<h1>Can’t open your saved data<\/h1>/);
  assert.match(recovery, /role="alert"/);
  assert.deepEqual([...recovery.matchAll(/<p>(.*?)<\/p>/g)].map(match => match[1]),
    ['The app stopped to protect your saved data.']);
  for (const html of [waiting, unavailable, recovery]) {
    assert.match(html, /class="setup-header"/);
    assert.match(html, /class="brand-mark"/);
    assert.match(html, /class="spot-tile"[^>]*aria-hidden="true"><svg[^>]+width="20"[^>]+height="20"/);
  }
  assert.match(recovery, /StorageOutlined/);
  assert.match(waiting, /class="status-icon"[^>]*aria-hidden="true"><svg[^>]*class="si-pending"/);
  assert.match(css, /--dipsy-intent-surface-dominant-border/);
  assert.match(css, /--dipsy-intent-surface-dominant-elevation/);
  assert.match(css, /font-size:\s*clamp\(1\.375rem,\s*1\.1rem\s*\+\s*\.9vw,\s*1\.75rem\)/);
  assert.match(css, /prefers-reduced-motion/);
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
      await page.addInitScript(() => customElements.define('desktop-window-controls', class extends HTMLElement {}));
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
        headerHeight: document.querySelector('header').getBoundingClientRect().height,
        tile: document.querySelector('.spot-tile').getBoundingClientRect().width,
        titleSize: parseFloat(getComputedStyle(document.querySelector('h1')).fontSize),
        bodySize: parseFloat(getComputedStyle(document.querySelector('body')).fontSize),
        pendingAnimation: getComputedStyle(document.querySelector('.si-pending')).animationName,
        statusColor: getComputedStyle(document.querySelector('[role="status"]')).color,
        ink: getComputedStyle(document.body).color,
        statusHeight: document.querySelector('.status-line').getBoundingClientRect().height,
        statusTop: document.querySelector('.status-line').getBoundingClientRect().top,
        titleBottom: document.querySelector('h1').getBoundingClientRect().bottom,
      }));
      assert.ok(metrics.overflow <= 1, `${scheme} 320px overflow: ${metrics.overflow}`);
      assert.ok(metrics.font, 'local Inter font must load');
      assert.ok(metrics.bg && metrics.text && metrics.heading > 0);
      assert.equal(metrics.role, 'Getting things ready on this computer…');
      assert.equal(metrics.headerHeight, 72);
      assert.equal(metrics.tile, 40);
      assert.equal(metrics.statusColor, metrics.ink);
      assert.equal(metrics.pendingAnimation, 'status-rotate');
      assert.ok(metrics.statusHeight >= 44);
      assert.ok(metrics.statusTop - metrics.titleBottom >= 15);
      await page.emulateMedia({ reducedMotion: 'reduce' });
      assert.equal(await page.locator('.si-pending').evaluate(node => getComputedStyle(node).animationName), 'none');
      await page.emulateMedia({ reducedMotion: 'no-preference' });
      await page.addStyleTag({ content: 'html { font-size: 32px !important; }' });
      const zoomed = await page.evaluate(() => ({
        title: parseFloat(getComputedStyle(document.querySelector('h1')).fontSize),
        body: parseFloat(getComputedStyle(document.body).fontSize),
        header: parseFloat(getComputedStyle(document.querySelector('.brand-name')).fontSize),
        overflow: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - innerWidth,
      }));
      assert.ok(zoomed.title >= 44, `title must scale with text size: ${zoomed.title}`);
      assert.equal(zoomed.body, 32);
      assert.equal(zoomed.header, 32);
      assert.ok(zoomed.overflow <= 1, `320px 2x text overflow: ${zoomed.overflow}`);
      assert.deepEqual(errors, []);
      for (const path of ['index.html', 'unavailable.html', 'data-recovery.html']) {
        await page.setViewportSize({ width: 320, height: 640 });
        await page.goto(`http://127.0.0.1:${address.port}/${path}`, { waitUntil: 'networkidle' });
        await page.addStyleTag({ content: 'html { font-size: 32px !important; }' });
        const narrow = await page.evaluate(() => ({
          overflow: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - innerWidth,
          title: parseFloat(getComputedStyle(document.querySelector('h1')).fontSize),
        }));
        assert.ok(narrow.overflow <= 1, `${scheme} ${path} 320px 2x text overflow: ${narrow.overflow}`);
        assert.ok(narrow.title >= 44, `${scheme} ${path} title did not scale: ${narrow.title}`);
        await page.setViewportSize({ width: 1440, height: 900 });
        await page.goto(`http://127.0.0.1:${address.port}/${path}`, { waitUntil: 'networkidle' });
        const titleLines = await page.locator('h1').evaluate(node => {
          const range = document.createRange(); range.selectNodeContents(node);
          return range.getClientRects().length;
        });
        assert.equal(titleLines, 1, `${scheme} ${path} title must occupy one line at 1440px`);
      }
      await context.close();
    }
    assert.ok(seen.includes('/inter-latin-opsz-normal.woff2'), 'the packaged font asset must be requested');
  } finally {
    await browser.close();
    await new Promise(done => server.close(done));
  }
});

test('native onboarding primitives render under the shell CSP', async () => {
  const root = new URL('../../desktop/shell/', import.meta.url);
  const config = JSON.parse(await readFile(new URL('../../desktop/src-tauri/tauri.conf.json', import.meta.url), 'utf8'));
  const layout = await readFile(new URL('../../frontend/src/theme/onboarding.css', import.meta.url), 'utf8');
  const icons = [...layout.matchAll(/^\.onboarding-icon\[data-icon="([a-z-]+)"\].*?url\("data:image\/svg\+xml,/gm)]
    .map(([, name]) => name);
  assert.equal(icons.length, 19);
  const icon = name => `<span class="onboarding-icon" data-icon="${name}" aria-hidden="true"></span>`;
  const document = scaled => `<!doctype html><html data-large="${scaled}"><head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet" href="/shell.css"><link rel="stylesheet" href="/fixture.css">
    </head><body><section class="onboarding-surface" data-composition="static">
    <div class="onboarding-surface-head"><span class="onboarding-spot">${icon('passkey')}</span>Device setup</div>
    <h1 class="onboarding-surface-title">Prepare this computer</h1>
    <div class="onboarding-surface-body">Follow the setup instructions.</div>
    <span class="onboarding-pill" data-tone="error"><span class="onboarding-tone-dot" aria-hidden="true"></span>Stopped</span>
    <span class="onboarding-pill" data-tone="neutral"><span class="onboarding-tone-dot" aria-hidden="true"></span>Ready</span>
    <div class="onboarding-status" data-tone="attention">${icon('attention')}<span>The status stays readable when the message wraps onto another line.</span></div>
    <div class="onboarding-status" data-tone="neutral">${icon('info')}<span>Setup is ready.</span></div>
    </section><div aria-hidden="true">${icons.map(icon).join('')}</div>
    <span class="reference-secondary"></span><span class="reference-primary"></span><span class="reference-error"></span>
    </body></html>`;
  const origin = 'http://native-onboarding.test';
  const types = { '.css': 'text/css', '.woff2': 'font/woff2', '.svg': 'image/svg+xml' };
  const browser = await chromium.launch();
  try {
    for (const colorScheme of ['light', 'dark']) for (const width of [1440, 390, 320]) {
      const page = await browser.newPage({ colorScheme, viewport: { width, height: 900 }, reducedMotion: 'reduce' });
      const loadedIcons = new Set();
      try {
        await page.addInitScript(() => {
          window.imageCspViolations = [];
          document.addEventListener('securitypolicyviolation', event => {
            if (event.effectiveDirective === 'img-src') window.imageCspViolations.push(event.blockedURI);
          });
        });
        await page.route(origin + '/**', async route => {
          const { pathname } = new URL(route.request().url());
          if (pathname === '/') return route.fulfill({ contentType: 'text/html', body: document(width === 320),
            headers: { 'content-security-policy': config.app.security.csp } });
          if (pathname === '/fixture.css') return route.fulfill({ contentType: 'text/css', body: `
            [data-large="true"] { font-size: 200%; }
            .reference-secondary { color: var(--dipsy-color-text-secondary); }
            .reference-primary { color: var(--dipsy-color-text); }
            .reference-error { color: var(--dipsy-color-error); }
            body > [aria-hidden] { position: absolute; inset-inline-start: 0; }
            body > [aria-hidden] > .onboarding-icon { inline-size: 1px; block-size: 1px; }
          ` });
          const name = pathname.slice(1);
          if (!['shell.css', 'onboarding-theme.css', 'inter-latin-opsz-normal.woff2'].includes(name)
            && !icons.some(iconName => name === `onboarding-icons/${iconName}.svg`)) return route.abort();
          const body = await readFile(new URL(name, root));
          await route.fulfill({ contentType: types[extname(name)], body });
          if (name.endsWith('.svg')) loadedIcons.add(name);
        });
        await page.goto(origin, { waitUntil: 'networkidle' });
        assert.deepEqual(await page.evaluate(() => window.imageCspViolations), []);
        assert.deepEqual([...loadedIcons].sort(), icons.map(name => `onboarding-icons/${name}.svg`).sort());
        const sizes = await page.evaluate(() => {
          const style = selector => getComputedStyle(document.querySelector(selector));
          const status = document.querySelector('.onboarding-status');
          const [mark, text] = status.children;
          const markRect = mark.getBoundingClientRect();
          return {
            head: ['fontSize', 'fontWeight', 'lineHeight', 'letterSpacing', 'textTransform'].map(key => style('.onboarding-surface-head')[key]),
            pill: ['fontSize', 'fontWeight', 'lineHeight', 'color'].map(key => style('.onboarding-pill')[key]),
            dot: style('.onboarding-tone-dot').backgroundColor,
            neutralDot: style('.onboarding-pill[data-tone="neutral"] .onboarding-tone-dot').backgroundColor,
            secondary: style('.reference-secondary').color,
            primary: style('.reference-primary').color,
            error: style('.reference-error').color,
            copy: getComputedStyle(text).color,
            secondaryInk: ['.onboarding-surface-head', '.onboarding-surface-body', '.onboarding-spot',
              '.onboarding-status .onboarding-icon', '.onboarding-status[data-tone="neutral"]'].map(selector => style(selector).color),
            firstLineDelta: markRect.top + markRect.height / 2 - text.getBoundingClientRect().top
              - parseFloat(getComputedStyle(text).lineHeight) / 2,
            overflow: document.documentElement.scrollWidth - innerWidth,
          };
        });
        const small = width === 320 ? 24 : 12;
        assert.deepEqual(sizes.head, [small + 'px', '600', width === 320 ? '33.6px' : '16.8px',
          width === 320 ? '1.92px' : '0.96px', 'uppercase']);
        assert.deepEqual(sizes.pill, [small + 'px', '500', (small * 1.5) + 'px', sizes.primary]);
        assert.equal(sizes.dot, sizes.error);
        assert.equal(sizes.neutralDot, sizes.secondary);
        assert.deepEqual(sizes.secondaryInk, Array(5).fill(sizes.secondary));
        assert.equal(sizes.copy, sizes.primary);
        assert.ok(Math.abs(sizes.firstLineDelta) < 0.1);
        assert.equal(sizes.overflow, 0);
      } finally {
        await page.close();
      }
    }
  } finally {
    await browser.close();
  }
});

for (const [selected, system] of [['dark', 'light'], ['light', 'dark'], ['system', 'dark'], ['system', 'light']]) {
  for (const [surface, relative] of [
    ['desktop', '../../app/static/dist/onboarding/index.html'],
    ['browser', '../../app/static/dist/browser-tasks/index.html'],
    ['setup code', '../../app/static/dist/setup-code/index.html'],
    ['admission', '../../app/onboarding/entry.html'],
  ]) {
    test('saved ' + selected + ' setup theme on ' + surface + ' with ' + system + ' system selects the canvas before CSS arrives', async () => {
      const browser = await chromium.launch();
      let release;
      const delayedStyles = new Promise(resolve => { release = resolve; });
      try {
        const context = await browser.newContext({ colorScheme: system });
        const page = await context.newPage();
        await page.addInitScript(theme => localStorage.setItem('mui-mode', theme), selected);
        const document = await readFile(new URL(relative, import.meta.url), 'utf8');
        assert.ok(document.includes('<script data-setup-first-paint>'));
        assert.ok(!document.includes('src="/static/dist/setup-theme-init.js"'));
        await page.route('http://127.0.0.1:17899/**', async route => {
          const path = new URL(route.request().url()).pathname;
          if (path === '/onboarding') return route.fulfill({ contentType: 'text/html', body: document });
          assert.notEqual(path, '/static/dist/setup-theme-init.js', 'no external theme initializer');
          if (path.endsWith('.css')) await delayedStyles;
          return route.fulfill({ contentType: path.endsWith('.css') ? 'text/css' : 'text/javascript', body: '' });
        });
        await page.goto('http://127.0.0.1:17899/onboarding', { waitUntil: 'commit' });
        await page.waitForFunction(theme => document.documentElement.dataset.colorScheme === theme
          || theme === 'system' && document.querySelector('style[data-setup-first-paint]')?.sheet, selected);
        const early = await page.evaluate(() => ({
          scheme: document.querySelector('meta[name="color-scheme"]').content,
          stylesheets: [...document.styleSheets].map(sheet => sheet.ownerNode?.hasAttribute('data-setup-first-paint')),
          background: getComputedStyle(document.documentElement).backgroundColor,
          resolved: getComputedStyle(document.documentElement).colorScheme,
        }));
        const expected = selected === 'system' ? system : selected;
        assert.deepEqual(early.stylesheets, [true], 'only inline critical CSS has arrived');
        assert.match(early.background, /^oklch\(/);
        assert.equal(early.resolved, expected);
        assert.equal(early.scheme, selected === 'system' ? 'light dark' : selected);
      } finally {
        release();
        await browser.close();
      }
    });
  }
}
