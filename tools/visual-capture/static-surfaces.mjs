// Browser qualification for the existing popup and setup page using synthetic states.
import assert from 'node:assert/strict';
import { runCaptureJobs } from './capture-jobs.mjs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

import { inspectTaskCopy } from './task-copy.mjs';
import { assertStaticAccessibility } from './static-accessibility.mjs';
import { installSurfaceFixture } from '../../extension/qualification/surface-runtime-fixture.mjs';
import { staticFixtures } from './static-fixtures.mjs';
import { onboardingStaticAssets } from './onboarding-static-fixtures.mjs';
import { staticCases } from './static-fixture-matrix.mjs';
import { staticConfiguration } from './ci/inventory.mjs';
import { installWatcher, readWatcher } from './shift-watcher.mjs';
import { REQUIRED_REGIONS } from './dynamic-transitions.mjs';
import { CANVAS_DELTA, LAYOUT_SHIFT, colorDistance, grade, gradeLayoutShifts } from './stability-contracts.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const ORIGIN = 'http://static-visual.localhost';
const KEY_ELEMENTS = 'h1, header, nav, .brand-mark, button, .primary-link';
export function observeStaticPaint(keySelector) {
  window.__staticPaint = { shifts: [], first: null, firstNumeric: null };
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
    if (!main?.getBoundingClientRect().height || getComputedStyle(main).visibility === 'hidden') return requestAnimationFrame(frame);
    const sample = {
      canvas: getComputedStyle(document.body).backgroundColor,
      faces: [...document.fonts].filter((face) => /Variable$/.test(face.family.replaceAll('"', '')))
        .map((face) => ({ family: face.family, status: face.status })),
    };
    if (!window.__staticPaint.first) window.__staticPaint.first = sample;
    const numeral = document.querySelector('#messages-count');
    if (!window.__staticPaint.firstNumeric && numeral?.getClientRects().length) window.__staticPaint.firstNumeric = sample;
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

async function actualFonts(session, selector) {
  const { root: document } = await session.send('DOM.getDocument');
  const { nodeId } = await session.send('DOM.querySelector', { nodeId: document.nodeId, selector });
  assert(nodeId, 'Font target is missing: ' + selector);
  return (await session.send('CSS.getPlatformFontsForNode', { nodeId })).fonts;
}

function injectedDocument({ html, script, surface }) {
  const body = /(<body\b[^>]*>)([\s\S]*?)<script src="\/[\w-]+\.js"><\/script>/u;
  assert(body.test(html), `Missing renderer mount point for ${surface}`);
  return html.replace(body, (_, open, markup) => `${open}<template>${markup}</template><script>
const fixtureMarkup = document.currentScript.previousElementSibling;
fixtureMarkup.replaceWith(fixtureMarkup.content);
${script.replace(/<\/script/gi, '<\\/script')}</script>`);
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
    }
    if (fixture.surface === 'setup') {
      const disclosure = { mode_choice: '#preview-disclosure',
        mode_choice_full: '#full-disclosure', full_review: '#full-disclosure' }[fixture.name];
      if (disclosure) {
        assert(await page.locator(disclosure).isVisible(), 'Required disclosure must be visible');
        const sizes = await page.locator(disclosure + ' p, ' + disclosure + ' label, ' + disclosure + ' li').evaluateAll((nodes) => nodes.map((node) => parseFloat(getComputedStyle(node).fontSize)));
        assert(sizes.every((size) => size >= 12), 'Disclosure text must not shrink below its original size');
        metrics.disclosureSizes = sizes;
      }
    }
    if (fixture.surface === 'setup-code') {
      const expected = fixture.state.state;
      assert.equal(await page.locator('#receiving-title').textContent(), 'Use a setup code');
      assert.equal(await page.locator('#receiving-guidance').textContent(), 'Enter the code shown on your other computer or browser.');
      assert.equal(await page.locator('#receiving-form').isHidden(), expected === 'setup_code.opening');
      assert.equal(await page.locator('#receiving-status').textContent(),
        expected === 'setup_code.opening' ? 'Getting ready for your code…'
          : expected === 'setup_code.checking' ? 'Checking the code…' : '');
      if (expected !== 'setup_code.opening') {
        assert.equal(await page.locator('#receiving-submit').textContent(),
          expected === 'setup_code.checking' ? 'Checking the code…' : 'Continue');
        const code = page.locator('#receiving-code');
        const previous = await code.inputValue();
        try {
          await code.evaluate(node => { node.value = '0123456789AB'; });
          await page.evaluate(() => document.fonts.ready);
          metrics.codeFonts = await actualFonts(session, '#receiving-code');
        } finally {
          await code.evaluate((node, value) => { node.value = value; }, previous);
        }
        assert(metrics.codeFonts.some(font => font.isCustomFont && /Space Grotesk/.test(font.familyName)),
          'Setup codes must render in Space Grotesk');
      }
    }
    const task = fixture.surface === 'setup-code' ? {} : undefined;
    const control = page.locator((await page.locator('dialog[open]').count()) ? 'dialog[open] button:visible:enabled' : 'button:visible:enabled, a.primary-link:visible, a.button:visible').first();
    if (await control.count()) {
      const stops = await page.locator('button:visible:enabled, a[href]:visible, input:visible:enabled, textarea:visible:enabled, select:visible:enabled').count();
      for (let index = 0; index < stops + 2 && !await control.evaluate(node => node === document.activeElement && node.matches(':focus-visible')); index++) {
        await page.keyboard.press('Tab');
      }
      await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
      const focus = await control.evaluate((node) => ({ visible: node.matches(':focus-visible'), width: getComputedStyle(node).outlineWidth }));
      assert(focus.visible); assert.equal(focus.width, '2px');
      metrics.focus = focus;
      await control.blur();
    }
    return { ...metrics, fonts, task: task ?? await inspectTaskCopy(page, fixture) };
  } finally { await session.detach(); }
}
export async function captureStaticSurfaces(browser, outDir, recorder = null) {
  const directory = join(outDir, 'static-surfaces');
  await mkdir(directory, { recursive: true });
  const popupCss = await readFile(join(root, 'extension/popup.css'), 'utf8');
  const setupCss = await readFile(join(root, 'extension/setup.css'), 'utf8');
  const onboardingAssets = await onboardingStaticAssets();
  const fixtures = await staticFixtures();
  const entries = [], failures = [], checks = [];
  const cases = staticCases(fixtures);
  await runCaptureJobs(cases, async ({ mode, fixture, width }) => {
        const viewport = fixture.pairing ? { width: 400, height: 488 }
          : { width, height: fixture.surface === 'popup' ? 600 : width > 600 ? 900 : 844 };
        const name = `${fixture.surface}-${fixture.name}-${mode}-${viewport.width}`;
        if (process.env.STATIC_SURFACE_ONLY && !name.includes(process.env.STATIC_SURFACE_ONLY)) return;
        const finish = recorder?.begin(`static-${name}`, staticConfiguration({ mode, fixture, width }));
        const observations = [], files = ['static-surfaces/acceptance.json'];
        let caseFailed = false;
        console.log('start ' + name);
        const context = await browser.newContext({ viewport, colorScheme: mode, reducedMotion: 'reduce', locale: 'en-US', deviceScaleFactor: 1 });
        const page = await context.newPage();
        await installWatcher(page, { requiredRegions: REQUIRED_REGIONS[fixture.surface],
          taskTransitions: fixture.surface === 'popup' ? 'popup' : fixture.surface === 'setup' });
        // Cold-delivery paint probes run without the CSS inspector affecting font discovery.
        const session = fixture.deliveryDelay ? null : await context.newCDPSession(page);
        if (session) { await session.send('DOM.enable'); await session.send('CSS.enable'); }
        const unexpected = [];
        page.on('pageerror', (error) => unexpected.push(error.message));
        await page.addInitScript(observeStaticPaint, KEY_ELEMENTS);
        if (fixture.injectedSurface) {
          await page.addInitScript(({ state, theme }) => {
            localStorage.setItem('mui-mode', theme);
            window.__staticSurfaceFixture = state;
          }, { state: fixture.state, theme: mode });
        } else if (fixture.state) {
          await page.addInitScript(installSurfaceFixture, fixture.state);
        }
        const documentPath = fixture.urlPath ?? `/${fixture.surface}.html`;
        const document = fixture.injectedSurface ? injectedDocument(fixture) : fixture.html;
        await page.route('**/*', async (route) => {
          const url = new URL(route.request().url());
          if (url.origin !== ORIGIN) { unexpected.push('Unexpected network request'); return route.abort(); }
          if (fixture.deliveryDelay && url.pathname === '/popup.css') {
            await new Promise((done) => setTimeout(done, fixture.deliveryDelay));
          }
          if (url.pathname === '/popup.css') return route.fulfill({ contentType: 'text/css', body: popupCss });
          if (url.pathname === '/setup.css') return route.fulfill({ contentType: 'text/css', body: setupCss });
          if (Object.hasOwn(onboardingAssets, url.pathname)) return route.fulfill(onboardingAssets[url.pathname]);
          if (fixture.script && url.pathname === `/${fixture.surface}.js`) return route.fulfill({ contentType: 'text/javascript', body: fixture.script });
          if (url.pathname === documentPath) return route.fulfill({ contentType: 'text/html', body: document });
          return route.fulfill({ status: 404 });
        });
        try {
          const hash = fixture.injectedSurface ? '' : (fixture.state?.hash ?? (fixture.surface === 'setup' ? 'journey=11111111-1111-4111-8111-111111111111' : ''));
          await page.goto(ORIGIN + `${documentPath}${hash ? '#' + hash : ''}`, { waitUntil: 'networkidle' });
          if (fixture.state) await page.waitForFunction(() => document.querySelector('main[data-ready="true"]') || document.querySelector('#runtime-unavailable:not(.hidden)'));
          if (fixture.state?.dialog) await page.locator('#delete-local-data').click();
          await page.evaluate(() => document.fonts.ready);
          await page.waitForFunction(() => window.__staticPaint.first !== null);
          await page.waitForTimeout(150);
          const paint = await page.evaluate(() => window.__staticPaint);
          const inter = paint.first.faces.find((face) => face.family === 'Inter Variable');
          assert.equal(inter?.status, 'loaded', 'Inter must be loaded at the first text frame');
          if (fixture.surface === 'popup' && fixture.name.startsWith('preview')) {
            const numeric = paint.firstNumeric?.faces.find((face) => face.family === 'Space Grotesk Variable');
            assert.equal(numeric?.status, 'loaded', 'Numeric font must be loaded at the first visible count frame');
          }
          const canvas = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
          const delta = colorDistance(paint.first.canvas, canvas);
          const shifts = gradeLayoutShifts(paint.shifts, LAYOUT_SHIFT);
          assert.notEqual(grade(delta, CANVAS_DELTA), 'fail', 'First-painted canvas changes after styling');
          assert.notEqual(shifts.level, 'fail', 'Static first-paint layout shifted: ' + JSON.stringify(shifts));
          const measurements = session ? await inspect(page, fixture, width, session) : { delayedAssetDeliveryMs: fixture.deliveryDelay };
          await assertStaticAccessibility(page);
          const watcher = await readWatcher(page);
          await writeFile(join(directory, `${name}-geometry.json`), JSON.stringify({ revision: process.env.VISUAL_CAPTURE_REVISION ?? null, view: fixture.surface, transition: `boot:${fixture.name}`, viewport, mode, ...watcher }) + '\n');
          assert.deepEqual(watcher.failures, []);
          assert.deepEqual(unexpected, []);
          observations.push(`check:${name}`);
          files.push(`static-surfaces/${name}-geometry.json`);
          await page.evaluate(() => window.scrollTo(0, 0));
          for (const [kind, fullPage] of [['fold', false], ['full', true]]) {
            const file = `${name}-${kind}.png`;
            await page.screenshot({ path: join(directory, file), animations: 'disabled', fullPage });
            observations.push(kind); files.push('static-surfaces/' + file);
            entries.push({ file: 'static-surfaces/' + file, surface: fixture.surface, state: fixture.name, mode, viewport, capture: kind });
          }
          console.log('captured ' + name);
          checks.push({ name, geometry: `${name}-geometry.json`, measurements, paint: { first: paint.first, firstNumeric: paint.firstNumeric, canvasDelta: delta, shifts } });
        } catch (error) {
          caseFailed = true;
          failures.push(`${name}: ${error.message}`); console.error(failures.at(-1));
          await page.screenshot({ path: join(directory, `${name}-failure.png`) });
          await writeFile(join(directory, `${name}-geometry.json`), JSON.stringify({ revision: process.env.VISUAL_CAPTURE_REVISION ?? null, view: fixture.surface, transition: `boot:${fixture.name}`, viewport, mode, error: error.message, ...await readWatcher(page) }) + '\n');
        }
        finally {
          finish?.({ outcome: caseFailed ? 'failed' : 'passed', observations, files });
          await context.close();
        }
  });
  entries.sort((a, b) => a.file.localeCompare(b.file));
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
