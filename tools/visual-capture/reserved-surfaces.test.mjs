import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import test from 'node:test';
import { chromium } from 'playwright';
import { onboardingStaticAssets, onboardingStaticFixtures } from './onboarding-static-fixtures.mjs';
import { DESKTOP_ONBOARDING_STATES } from './onboarding-surface-states.mjs';
import { observeStaticPaint } from './static-surfaces.mjs';
import { CANVAS_DELTA, LAYOUT_SHIFT, colorDistance, grade, gradeLayoutShifts } from './stability-contracts.mjs';

const { JSDOM } = createRequire(new URL('../../frontend/package.json', import.meta.url))('jsdom');
for (const surface of ['popup', 'setup', 'options']) {
  test(`${surface} retains feedback and content frames from its first document`, async () => {
    const html = await readFile(new URL(`../../extension/${surface}.html`, import.meta.url), 'utf8');
    const dom = new JSDOM(html);
    try {
      const document = dom.window.document;
      assert(document.querySelector('[data-reserved-region]'));
      assert(document.querySelector('#feedback').closest('[data-reserved-region]'));
      assert.equal(document.querySelector('[data-reserved-region].hidden'), null);
      if (surface === 'popup') assert(document.querySelector('[data-reserved-region="popup-status"] [data-region-content]'));
      if (surface === 'options') assert(document.querySelector('#connection-details-dialog'));
    } finally { dom.window.close(); }
  });
}

test('native recovery needs no package input and keeps setup-code receiving separate', async () => {
  const html = await readFile(new URL('../../frontend/onboarding/index.html', import.meta.url), 'utf8');
  const dom = new JSDOM(html);
  try {
    const document = dom.window.document;
    assert.equal(document.querySelectorAll('[data-step]').length, 3);
    assert.equal(document.querySelector('#recovery-package'), null);
    assert.equal(document.querySelector('textarea'), null);
    assert.equal(document.querySelector('#task-status').getAttribute('role'), 'status');
    assert.equal(document.querySelector('#task-action').getAttribute('aria-describedby'), 'task-description task-status');
    assert.equal(document.querySelector('#setup-code-open').textContent, 'I have a setup code');
    assert.equal(document.querySelector('#setup-code-open').hidden, true);
  } finally { dom.window.close(); }
});

async function withDesktop(width, scale, mode, name, run) {
  const [assets, fixtures] = await Promise.all([onboardingStaticAssets(), onboardingStaticFixtures()]);
  const fixture = fixtures.find(item => item.surface === 'desktop-onboarding' && item.name === name);
  const origin = 'http://static-visual.localhost';
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width, height: 900 }, colorScheme: mode, reducedMotion: 'reduce' });
    await page.addInitScript(observeStaticPaint, 'h1, header, nav, .brand-mark, button');
    await page.addInitScript(({ state, mode }) => {
      localStorage.setItem('mui-mode', mode);
      window.__staticSurfaceFixture = state;
    }, { state: fixture.state, mode });
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, origin);
      if (url.pathname === fixture.urlPath) return route.fulfill({ contentType: 'text/html', body: fixture.html });
      if (url.pathname === '/desktop-onboarding.js') return route.fulfill({ contentType: 'text/javascript', body: fixture.script });
      return assets[url.pathname] ? route.fulfill(assets[url.pathname]) : route.abort();
    });
    await page.goto(origin + fixture.urlPath);
    await page.locator('main[data-ready="true"]').waitFor();
    await page.evaluate(async scale => {
      await document.fonts.ready;
      if (scale !== 1) document.documentElement.style.fontSize = `${16 * scale}px`;
    }, scale);
    const render = async state => {
      await page.evaluate(state => { window.__staticSurfaceFixture = state; }, state);
      await page.addScriptTag({ content: fixture.script });
    };
    await run(page, render);
  } finally { await browser.close(); }
}

test('desktop empty task actions occupy no more than one line', async () => {
  await withDesktop(1440, 1, 'light', 'finish_getting_ready', async page => {
    const region = await page.locator('[data-reserved-region="desktop-task-actions"]').evaluate(node => ({
      height: node.getBoundingClientRect().height, line: parseFloat(getComputedStyle(node).lineHeight),
    }));
    assert(region.height <= region.line, `Empty desktop action region occupies ${region.height}px`);
  });
});

test('desktop marks and pending swaps stay aligned through current-step and reason changes', async () => {
  await withDesktop(1440, 1, 'light', 'browser_access_needed', async (page, render) => {
    await page.waitForFunction(() => window.__staticPaint.first !== null);
    const { first, canvas, shifts } = await page.evaluate(() => ({ ...window.__staticPaint, canvas: getComputedStyle(document.body).backgroundColor }));
    assert.equal(first.faces.find(face => face.family === 'Inter Variable')?.status, 'loaded');
    assert.notEqual(grade(colorDistance(first.canvas, canvas), CANVAS_DELTA), 'fail');
    assert.notEqual(gradeLayoutShifts(shifts, LAYOUT_SHIFT).level, 'fail');
    await page.keyboard.press('Tab');
    assert(await page.locator('#task-action').evaluate(node => node.matches(':focus-visible')));
    const marks = () => page.locator('.setup-step-badge').evaluateAll(nodes => nodes.map(node => {
      const { x, y, width, height } = node.getBoundingClientRect();
      return { x, y, width, height };
    }));
    const original = await marks();
    assert(original.every(mark => mark.width === 28 && mark.height === 28 && mark.y === original[0].y));
    const stepper = await page.locator('.setup-steps li').first().evaluate(node => ({
      y: node.getBoundingClientRect().y, height: node.getBoundingClientRect().height,
      align: getComputedStyle(node).alignItems, connector: getComputedStyle(node, '::after').height,
    }));
    assert.deepEqual([stepper.align, stepper.connector], ['center', '1px']);
    assert.equal(stepper.y + stepper.height / 2, original[0].y + 14);
    const pending = DESKTOP_ONBOARDING_STATES.browser_access_connecting;
    await render(pending);
    const row = async id => {
      const { y, height } = await page.locator(id).boundingBox();
      return { y, height };
    };
    const waiting = await row('#task-pending');
    assert.equal(await page.locator('#task-pending').evaluate(node => getComputedStyle(node, '::before').animationName), 'none');
    const opened = { ...pending, acting: null };
    await render(opened);
    assert.deepEqual(await row('#browser-reopen'), waiting);
    await render({ ...opened, error: 'extension_unavailable' });
    assert.deepEqual(await row('#browser-reopen'), waiting);
    await render({ ...opened, connected: false, error: 'status_unavailable' });
    assert.deepEqual(await row('#task-pending'), waiting);
    await render(DESKTOP_ONBOARDING_STATES.account_confirm);
    assert.deepEqual(await marks(), original);
    await render(DESKTOP_ONBOARDING_STATES.finish_connect_extension);
    assert.deepEqual(await marks(), original);
    const connect = await row('#task-action');
    await render(DESKTOP_ONBOARDING_STATES.finish_connecting_extension);
    assert.deepEqual(await row('#task-pending'), connect);
    await render(DESKTOP_ONBOARDING_STATES.finish_secure_app_setup);
    const create = await row('#local-authenticate');
    await render(DESKTOP_ONBOARDING_STATES.finish_passkey_creating);
    assert.deepEqual(await row('#local-auth-pending'), create);
  });
});

test('desktop titles wrap freely and narrow step names remain accessible at 320px and 2× text', async () => {
  await withDesktop(320, 2, 'dark', 'finish_secure_app_setup', async page => {
    const result = await page.locator('#task-title').evaluate(node => {
      const css = getComputedStyle(node);
      const range = document.createRange();
      range.selectNodeContents(node);
      const lines = [...range.getClientRects()];
      return { count: lines.length, clamp: css.webkitLineClamp,
        fits: lines.every(rect => rect.left >= 0 && rect.right <= innerWidth) && node.scrollHeight <= node.clientHeight,
        pageFits: document.documentElement.scrollWidth <= innerWidth };
    });
    assert(result.count > 2 && result.fits && result.pageFits && result.clamp === 'none', JSON.stringify(result));
    assert.match(await page.getByRole('navigation').ariaSnapshot(), /Set up Chrome[\s\S]*Connect your account[\s\S]*Finish setup/u);
  });
});

test('desktop task copy fits the 1440px budgets in both color schemes', async () => {
  for (const mode of ['light', 'dark']) {
    await withDesktop(1440, 1, mode, 'browser_access_needed', async (page, render) => {
      for (const [name, state] of Object.entries(DESKTOP_ONBOARDING_STATES)) {
        await render(state);
        const lines = await page.evaluate(() => Object.fromEntries([
          'task-title', 'task-description', 'task-status', 'task-pending', 'local-auth-status', 'local-auth-pending',
        ].map(id => {
          const node = document.getElementById(id);
          const range = document.createRange();
          range.selectNodeContents(node);
          return [id, new Set([...range.getClientRects()].map(rect => rect.top)).size];
        })));
        for (const [id, count] of Object.entries(lines)) {
          assert(count <= (id === 'task-description' ? 2 : 1), `${mode} ${name} ${id} wraps to ${count} lines`);
        }
      }
    });
  }
});
