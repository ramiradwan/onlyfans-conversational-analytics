// Browser qualification for the existing popup and setup page using synthetic states.
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

import { assertStaticAccessibility } from './static-accessibility.mjs';
import { staticFixtures } from './static-fixtures.mjs';
import { CANVAS_DELTA, LAYOUT_SHIFT, colorDistance, grade, gradeLayoutShifts } from './stability-contracts.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const ORIGIN = 'http://static-visual.localhost';
const KEY_ELEMENTS = 'h1, header, nav, .brand-mark, button, .primary-link';
function observe(keySelector) {
  window.__staticPaint = { shifts: [], first: null };
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) window.__staticPaint.shifts.push({
      value: entry.value, hadRecentInput: entry.hadRecentInput,
      sources: (entry.sources ?? []).map(({ node, previousRect, currentRect }) => ({
        node: node instanceof Element ? node.tagName.toLowerCase() + (node.id ? '#' + node.id : '') : 'text',
        key: node instanceof Element && (node.matches(keySelector) || Boolean(node.querySelector(keySelector))),
        from: [previousRect.x, previousRect.y, previousRect.width, previousRect.height],
        to: [currentRect.x, currentRect.y, currentRect.width, currentRect.height],
      })),
    });
  }).observe({ type: 'layout-shift', buffered: true });
  const frame = () => {
    const main = document.querySelector('main');
    if (!main?.getBoundingClientRect().height) return requestAnimationFrame(frame);
    window.__staticPaint.first = {
      canvas: getComputedStyle(document.body).backgroundColor,
      faces: [...document.fonts].filter((face) => /Variable$/.test(face.family.replaceAll('"', '')))
        .map((face) => ({ family: face.family, status: face.status })),
    };
  };
  requestAnimationFrame(frame);
}

async function actualFonts(session, selector) {
  const { root: document } = await session.send('DOM.getDocument');
  const { nodeId } = await session.send('DOM.querySelector', { nodeId: document.nodeId, selector });
  assert(nodeId, 'Font target is missing: ' + selector);
  return (await session.send('CSS.getPlatformFontsForNode', { nodeId })).fonts;
}
async function inspect(page, fixture, width, session) {
  try {
    const fonts = await actualFonts(session, 'h1');
    assert(fonts.some((font) => font.isCustomFont && /Inter/.test(font.familyName)), 'Inter must render the heading');
    const metrics = await page.evaluate(() => {
      const css = getComputedStyle(document.documentElement);
      const brand = document.querySelector('.brand-mark').getBoundingClientRect();
      return { canvas: getComputedStyle(document.body).backgroundColor,
        brand: { x: brand.x, y: brand.y, width: brand.width, height: brand.height },
        paper: css.getPropertyValue('--dipsy-color-paper').trim(),
        canvasToken: css.getPropertyValue('--dipsy-color-background').trim(),
        overflow: Math.max(document.documentElement.scrollWidth - innerWidth, document.body.scrollWidth - innerWidth, 0) };
    });
    assert(metrics.overflow <= 1, 'Unexpected horizontal overflow: ' + metrics.overflow);
    assert.equal(metrics.brand.width, 32); assert.equal(metrics.brand.height, 32);
    assert(colorDistance(metrics.canvas, metrics.canvasToken) < 0.001, 'Canvas must use its semantic color');
    if (fixture.surface === 'popup') {
      if (fixture.name === 'full_ready') {
        const fill = await page.locator('#journey-card').evaluate((node) => getComputedStyle(node).backgroundColor);
        assert(colorDistance(fill, metrics.paper) < 0.001, 'Settled success must remain paper');
        metrics.successFill = fill;
      }
      if (fixture.name === 'preview') {
        metrics.numericFonts = await actualFonts(session, '#messages-count');
        assert(metrics.numericFonts.some((font) => font.isCustomFont && /Space Grotesk/.test(font.familyName)), 'Counts must use Space Grotesk');
      }
      if (['software_activation', 'mode_choice', 'full_review'].includes(fixture.name)) {
        const disclosure = fixture.name === 'software_activation' ? '#pre-mode' : '#full-disclosure';
        assert(await page.locator(disclosure).isVisible(), 'Required disclosure must be visible');
        if (disclosure === '#full-disclosure') assert(await page.locator(disclosure + ' details').evaluate((node) => node.open));
        const sizes = await page.locator(disclosure + ' p, ' + disclosure + ' label, ' + disclosure + ' li').evaluateAll((nodes) => nodes.map((node) => parseFloat(getComputedStyle(node).fontSize)));
        assert(sizes.every((size) => size >= 12), 'Disclosure text must not shrink below its original size');
        metrics.disclosureSizes = sizes;
      }
    } else {
      assert.equal(metrics.brand.x, width >= 600 ? 28 : 16);
      assert.equal(metrics.brand.y, 20);
      const steps = await page.locator('[data-step]').evaluateAll((nodes) => nodes.map((node) => node.dataset.state));
      assert.equal(steps.filter((state) => state === 'current').length, fixture.name === 'completed' ? 0 : 1);
      metrics.steps = steps;
      const active = page.locator('.step[data-state="current"]');
      if (await active.count()) assert.equal(await active.getAttribute('aria-current'), 'step');
      if (fixture.name === 'invalid-code') assert.equal(await page.locator('#claim-package').getAttribute('aria-invalid'), 'true');
      if (fixture.name.includes('unavailable')) assert(!await page.locator('#open-secure-setup').isVisible());
    }
    const control = page.locator('button:visible:enabled, a.primary-link:visible').first();
    if (await control.count()) {
      await page.keyboard.press('Tab'); await control.focus();
      await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
      const focus = await control.evaluate((node) => ({ visible: node.matches(':focus-visible'), width: getComputedStyle(node).outlineWidth }));
      assert(focus.visible); assert.equal(focus.width, '2px');
      metrics.focus = focus;
      await control.blur();
    }
    return { ...metrics, fonts };
  } finally { await session.detach(); }
}
export async function captureStaticSurfaces(browser, outDir) {
  const directory = join(outDir, 'static-surfaces');
  await mkdir(directory, { recursive: true });
  const popupCss = await readFile(join(root, 'extension/popup.css'), 'utf8');
  const fixtures = await staticFixtures();
  fixtures.push(...fixtures.filter((item) => item.name === 'preview' || item.name === 'connect')
    .map((item) => ({ ...item, sourceName: item.name, name: item.name + '-cold-assets', deliveryDelay: 1500 })));
  const entries = [], failures = [], checks = [];
  for (const mode of ['light', 'dark']) {
    for (const fixture of fixtures) {
      for (const width of fixture.widths) {
        const viewport = fixture.pairing ? { width: 400, height: 488 }
          : { width, height: fixture.surface === 'popup' ? 600 : width > 600 ? 900 : 844 };
        const name = `${fixture.surface}-${fixture.name}-${mode}-${viewport.width}`;
        if (process.env.STATIC_SURFACE_ONLY && !name.includes(process.env.STATIC_SURFACE_ONLY)) continue;
        console.log('start ' + name);
        const context = await browser.newContext({ viewport, colorScheme: mode, reducedMotion: 'reduce', locale: 'en-US', deviceScaleFactor: 1 });
        const page = await context.newPage();
        // Cold-delivery paint probes run without the CSS inspector affecting font discovery.
        const session = fixture.deliveryDelay ? null : await context.newCDPSession(page);
        if (session) { await session.send('DOM.enable'); await session.send('CSS.enable'); }
        const unexpected = [];
        page.on('pageerror', (error) => unexpected.push(error.message));
        await page.addInitScript(observe, KEY_ELEMENTS);
        await page.route('**/*', async (route) => {
          const url = new URL(route.request().url());
          if (url.origin !== ORIGIN) { unexpected.push('Unexpected network request'); return route.abort(); }
          if (fixture.deliveryDelay && (url.pathname === '/popup.css' || fixture.surface === 'provisioning')) {
            await new Promise((done) => setTimeout(done, fixture.deliveryDelay));
          }
          if (url.pathname === '/popup.css') return route.fulfill({ contentType: 'text/css', body: popupCss });
          if (url.pathname === '/popup.html' || url.pathname === '/provisioning') return route.fulfill({ contentType: 'text/html', body: fixture.html });
          return route.fulfill({ status: 404 });
        });
        try {
          await page.goto(ORIGIN + (fixture.surface === 'popup' ? '/popup.html' : '/provisioning'), { waitUntil: 'networkidle' });
          await page.evaluate(() => document.fonts.ready);
          await page.waitForFunction(() => window.__staticPaint.first !== null);
          await page.waitForTimeout(150);
          const paint = await page.evaluate(() => window.__staticPaint);
          const inter = paint.first.faces.find((face) => face.family === 'Inter Variable');
          assert.equal(inter?.status, 'loaded', 'Inter must be loaded at the first text frame');
          if (fixture.name.startsWith('preview')) {
            const numeric = paint.first.faces.find((face) => face.family === 'Space Grotesk Variable');
            assert.equal(numeric?.status, 'loaded', 'Numeric font must be loaded at the first text frame');
          }
          const canvas = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
          const delta = colorDistance(paint.first.canvas, canvas);
          const shifts = gradeLayoutShifts(paint.shifts, LAYOUT_SHIFT);
          assert.notEqual(grade(delta, CANVAS_DELTA), 'fail', 'First-painted canvas changes after styling');
          assert.notEqual(shifts.level, 'fail', 'Static first-paint layout shifted: ' + JSON.stringify(shifts));
          const measurements = session ? await inspect(page, fixture, width, session) : { delayedAssetDeliveryMs: fixture.deliveryDelay };
          await assertStaticAccessibility(page);
          assert.deepEqual(unexpected, []);
          await page.evaluate(() => window.scrollTo(0, 0));
          for (const [kind, fullPage] of [['fold', false], ['full', true]]) {
            const file = `${name}-${kind}.png`;
            await page.screenshot({ path: join(directory, file), animations: 'disabled', fullPage });
            entries.push({ file: 'static-surfaces/' + file, surface: fixture.surface, state: fixture.name, mode, viewport, capture: kind });
          }
          console.log('captured ' + name);
          checks.push({ name, measurements, paint: { first: paint.first, canvasDelta: delta, shifts } });
        } catch (error) { failures.push(`${name}: ${error.message}`); console.error(failures.at(-1)); }
        finally { await context.close(); }
      }
    }
  }
  const report = { limits: { layout: LAYOUT_SHIFT, canvas: CANVAS_DELTA, keyElements: KEY_ELEMENTS }, revision: process.env.VISUAL_CAPTURE_REVISION ?? null, entries, checks, failures };
  await writeFile(join(directory, 'acceptance.json'), JSON.stringify(report, null, 2) + '\n');
  return report;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const browser = await chromium.launch();
  try {
    const report = await captureStaticSurfaces(browser, resolve(process.argv[2] ?? 'artifacts/visual-capture'));
    console.log(JSON.stringify({ checks: report.checks.length, screenshots: report.entries.length, failures: report.failures.length }));
    if (report.failures.length) process.exitCode = 1;
  } finally { await browser.close(); }
}
