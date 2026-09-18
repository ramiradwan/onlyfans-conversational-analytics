// Measures what changes on screen after first paint: the canvas color before and after the app
// script runs, web fonts still loading when text first paints, and layout shifts on the production
// boot path and on each story harness screen. Usage: node layout-stability.mjs [outDir]
// LAYOUT_STABILITY_ONLY=boot or =harness limits the run to one part. Requires the frontend build.
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { dirname, extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { chromium } from 'playwright';

import { FIXED_NOW, SCREENS, startHarness } from './capture.mjs';
import {
  CANVAS_DELTA,
  FONT_SWAP_SHIFT,
  KEY_ELEMENTS,
  LAYOUT_SHIFT,
  colorDistance,
  grade,
  gradeLayoutShifts,
  parseColor,
} from './stability-contracts.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const productRoot = resolve(here, '../..');
const dist = join(productRoot, 'app/static/dist');
const outDir = resolve(process.argv[2] ?? join(here, 'output', 'layout-stability'));
const only = process.env.LAYOUT_STABILITY_ONLY;

const ORIGIN = 'http://localhost:4799';
const SLOW_FONT_MS = 1500;
const SETTLE_MS = 800;
const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'narrow', width: 390, height: 844 },
];
/** A laptop window too short to fit the passkey error below the card without moving the card. */
const SHORT_VIEWPORT = { name: 'short', width: 1366, height: 600 };
const COLOR_CASES = [
  { name: 'no saved mode, light system', colorScheme: 'light', savedMode: null },
  { name: 'no saved mode, dark system', colorScheme: 'dark', savedMode: null },
  { name: 'saved system mode, dark system', colorScheme: 'dark', savedMode: 'system' },
  { name: 'saved dark mode, light system', colorScheme: 'light', savedMode: 'dark' },
  { name: 'saved light mode, dark system', colorScheme: 'dark', savedMode: 'light' },
];
const CONTENT_TYPES = {
  '.css': 'text/css',
  '.js': 'text/javascript',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.woff2': 'font/woff2',
};

const results = [];
const record = (scenario, measure, value, level, detail) => {
  results.push({ scenario, measure, value, level, ...(detail ? { detail } : {}) });
};

/** Page init script: records layout shifts and the brand font state at the first frame with app text. */
function observe(keySelector) {
  const describe = (node) => {
    if (!(node instanceof Element)) return node ? '#text' : 'removed node';
    const label = node.getAttribute('data-visual') ?? node.getAttribute('aria-label') ?? node.getAttribute('role');
    const text = (node.textContent ?? '').trim().replace(/\s+/g, ' ').slice(0, 40);
    return `${node.tagName.toLowerCase()}${label ? `[${label}]` : ''}${text ? ` "${text}"` : ''}`;
  };
  const rect = ({ x, y, width, height }) => [x, y, width, height].map(Math.round);
  const brandFaces = () => [...document.fonts]
    .map((face) => ({ family: face.family.replace(/"/g, ''), range: face.unicodeRange, status: face.status }))
    .filter((face) => /^(Inter|Space Grotesk) Variable$/.test(face.family) && face.range.startsWith('U+0-FF,'))
    .map(({ family, status }) => ({ family, status }));
  window.__stability = { shifts: [], firstText: null };
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      window.__stability.shifts.push({
        at: Math.round(entry.startTime),
        value: entry.value,
        hadRecentInput: entry.hadRecentInput,
        sources: (entry.sources ?? []).map(({ node, previousRect, currentRect }) => ({
          node: describe(node),
          key: node instanceof Element && (node.matches(keySelector) || node.querySelector(keySelector) !== null),
          from: rect(previousRect),
          to: rect(currentRect),
        })),
      });
    }
  }).observe({ type: 'layout-shift', buffered: true });
  const watcher = new MutationObserver(() => {
    if (!document.getElementById('root')?.firstElementChild) return;
    watcher.disconnect();
    // The second callback runs after the first frame that laid out the app's text.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      window.__stability.firstText = { at: Math.round(performance.now()), faces: brandFaces() };
    }));
  });
  watcher.observe(document, { childList: true, subtree: true });
}

/** Page init script: preloads the given font files as soon as the document has a head. */
function preloadFonts(urls) {
  const add = () => {
    for (const href of urls) {
      const link = Object.assign(document.createElement('link'), { rel: 'preload', as: 'font', type: 'font/woff2', href });
      link.crossOrigin = '';
      document.head.append(link);
    }
  };
  if (document.head) return add();
  const watcher = new MutationObserver(() => {
    if (!document.head) return;
    watcher.disconnect();
    add();
  });
  watcher.observe(document, { childList: true, subtree: true });
}

/** Latin brand font files the harness loads, which production preloads from its index. */
async function brandFontUrls(context, base) {
  const page = await context.newPage();
  try {
    await page.goto(`${base}?workspace=home&state=populated&mode=light`);
    await page.waitForLoadState('networkidle');
    const urls = await page.evaluate(() => [...document.styleSheets]
      .flatMap((sheet) => {
        try {
          return [...sheet.cssRules];
        } catch {
          return [];
        }
      })
      .filter((rule) => rule instanceof CSSFontFaceRule
        && /^"?(Inter|Space Grotesk) Variable"?$/.test(rule.style.getPropertyValue('font-family'))
        && rule.style.getPropertyValue('unicode-range').startsWith('U+0-FF,'))
      .map((rule) => new URL(/url\("([^"]+)"\)/.exec(rule.style.getPropertyValue('src'))[1], document.baseURI).href));
    if (urls.length !== 2) throw new Error(`expected two latin brand font files, found ${urls.length}`);
    return urls;
  } finally {
    await page.close();
  }
}

/** Space between the passkey card and the end of the main area's content box. */
function roomBelowCard() {
  const main = document.querySelector('main');
  const card = document.querySelector('[data-visual="passkey-card"]');
  const end = main.getBoundingClientRect().bottom - Number.parseFloat(getComputedStyle(main).paddingBottom);
  return end - card.getBoundingClientRect().bottom;
}

/** Height the passkey error takes below the card, including its top margin. */
function errorHeight() {
  const alert = document.querySelector('[role="alert"]');
  return alert.getBoundingClientRect().height + Number.parseFloat(getComputedStyle(alert).marginTop);
}

async function settle(page) {
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(SETTLE_MS);
}

function recordShifts(scenario, shifts, limits) {
  const { score, level, keyMoves, moved } = gradeLayoutShifts(shifts, limits);
  const detail = moved.length ? { keyMoves, moved } : undefined;
  record(scenario, 'layout shift score', score, level, detail);
}

/** The visible canvas: the first opaque body or root background, else the browser canvas color. */
async function canvasColor(page) {
  const [body, root, system] = await page.evaluate(() => {
    const probe = document.createElement('div');
    probe.style.backgroundColor = 'Canvas';
    document.documentElement.append(probe);
    const colors = [document.body, document.documentElement, probe]
      .map((element) => (element ? getComputedStyle(element).backgroundColor : 'rgba(0, 0, 0, 0)'));
    probe.remove();
    return colors;
  });
  return [body, root].find((color) => parseColor(color).alpha > 0) ?? system;
}

function python() {
  const venv = process.platform === 'win32'
    ? join(productRoot, '.venv', 'Scripts', 'python.exe')
    : join(productRoot, '.venv', 'bin', 'python');
  return existsSync(venv) ? venv : 'python';
}

async function renderProductionIndex() {
  if (!existsSync(join(dist, 'manifest.json'))) {
    throw new Error('app/static/dist has no build; run npm run build --prefix frontend first');
  }
  const file = join(outDir, 'production-index.html');
  try {
    execFileSync(python(), [join(here, 'render_production_index.py'), file], { cwd: productRoot, stdio: 'pipe' });
  } catch (error) {
    throw new Error(`rendering the production index failed:\n${error.stderr ?? error.message}`);
  }
  return readFile(file, 'utf8');
}

async function openBoot(browser, indexHtml, { viewport, colorScheme = 'light', savedMode = null, holdScripts = false, fontDelay = 0 }) {
  const context = await browser.newContext({ viewport, colorScheme, deviceScaleFactor: 1, locale: 'en-US', reducedMotion: 'reduce' });
  const page = await context.newPage();
  let release = () => {};
  const held = holdScripts ? new Promise((resolveHeld) => { release = resolveHeld; }) : null;
  await page.route(`${ORIGIN}/**`, async (route) => {
    const { pathname } = new URL(route.request().url());
    if (pathname === '/') return route.fulfill({ body: indexHtml, contentType: 'text/html' });
    if (!pathname.startsWith('/static/dist/')) {
      return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    }
    const file = join(dist, decodeURIComponent(pathname.slice('/static/dist/'.length)));
    if (held && file.endsWith('.js')) await held;
    if (fontDelay && file.endsWith('.woff2')) await new Promise((wait) => setTimeout(wait, fontDelay));
    const body = await readFile(file).catch(() => null);
    if (!body) return route.fulfill({ status: 404 });
    return route.fulfill({ body, contentType: CONTENT_TYPES[extname(file)] ?? 'application/octet-stream' });
  });
  await page.addInitScript((mode) => {
    if (mode) localStorage.setItem('mui-mode', mode);
  }, savedMode);
  await page.addInitScript(observe, KEY_ELEMENTS);
  return { context, page, release };
}

async function measureBoot(browser) {
  const indexHtml = await renderProductionIndex();
  for (const colorCase of COLOR_CASES) {
    const scenario = `boot, ${colorCase.name}`;
    const { context, page, release } = await openBoot(browser, indexHtml, { ...colorCase, viewport: VIEWPORTS[0], holdScripts: true });
    try {
      await page.goto(`${ORIGIN}/`, { waitUntil: 'commit' });
      await page.waitForFunction(() => document.readyState !== 'loading'
        && [...document.querySelectorAll('link[rel="stylesheet"]')].every((link) => link.sheet));
      if (await page.evaluate(() => document.getElementById('root')?.childElementCount) !== 0) {
        throw new Error('the app rendered before its script was released');
      }
      const before = await canvasColor(page);
      release();
      await page.waitForFunction(() => document.getElementById('root')?.childElementCount > 0);
      await settle(page);
      const after = await canvasColor(page);
      const delta = colorDistance(before, after);
      record(scenario, 'canvas color change (OKLab)', Number(delta.toPrecision(3)), grade(delta, CANVAS_DELTA), delta > 0 ? { before, after } : undefined);
    } catch (error) {
      record(scenario, 'error', error.message.split('\n')[0], 'fail');
    } finally {
      await context.close();
    }
  }
  for (const viewport of VIEWPORTS) {
    for (const fontDelay of [0, SLOW_FONT_MS]) {
      const scenario = `boot, passkey, ${viewport.name}${fontDelay ? `, fonts delayed ${fontDelay} ms` : ''}`;
      const { context, page } = await openBoot(browser, indexHtml, { viewport, fontDelay });
      try {
        await page.goto(`${ORIGIN}/`);
        await page.waitForFunction(() => window.__stability.firstText);
        await settle(page);
        const { shifts, firstText } = await page.evaluate(() => window.__stability);
        const loading = firstText.faces.filter((face) => face.status === 'loading');
        if (fontDelay && loading.length === 0) {
          throw new Error('text painted with web fonts already loaded, so the fallback faces were not exercised');
        }
        if (!fontDelay) {
          record(scenario, 'web fonts loading at first text paint', loading.length, loading.length ? 'warn' : 'pass', loading.length ? { loading } : undefined);
        }
        recordShifts(scenario, shifts, fontDelay ? FONT_SWAP_SHIFT : LAYOUT_SHIFT);
      } catch (error) {
        record(scenario, 'error', error.message.split('\n')[0], 'fail');
      } finally {
        await context.close();
      }
    }
  }
}

async function measureHarness(browser) {
  const { base, vite } = await startHarness();
  try {
    for (const viewport of [...VIEWPORTS, SHORT_VIEWPORT]) {
      const context = await browser.newContext({
        viewport, colorScheme: 'light', deviceScaleFactor: 1, locale: 'en-US', reducedMotion: 'reduce', timezoneId: 'UTC',
      });
      // Font swaps are measured on the production boot path; preloading them here, as production
      // does, leaves the shifts that come from the app itself.
      await context.addInitScript(preloadFonts, await brandFontUrls(context, base));
      await context.addInitScript(observe, KEY_ELEMENTS);
      const open = async (screen) => {
        const page = await context.newPage();
        await page.clock.setFixedTime(FIXED_NOW);
        await page.goto(`${base}?workspace=${screen.workspace}&state=${screen.state}&mode=light`);
        await screen.ready(page).waitFor({ state: 'visible', timeout: 15_000 });
        await settle(page);
        return page;
      };
      const screens = viewport === SHORT_VIEWPORT ? [] : SCREENS.filter((candidate) => !candidate.variant && !candidate.act);
      for (const screen of screens) {
        const scenario = `${screen.workspace}, ${screen.state}, ${viewport.name}`;
        let page;
        try {
          page = await open(screen);
          recordShifts(scenario, await page.evaluate(() => window.__stability.shifts));
        } catch (error) {
          record(scenario, 'error', error.message.split('\n')[0], 'fail');
        } finally {
          await page?.close();
        }
      }
      const scenario = `passkey, sign-in error, ${viewport.name}`;
      let page;
      try {
        page = await open(SCREENS.find((screen) => screen.workspace === 'passkey' && screen.state === 'resting'));
        await page.evaluate(() => { window.__stability.shifts = []; });
        const room = await page.evaluate(roomBelowCard);
        // The error arrives after the system passkey prompt closes, so the click is dispatched
        // without user input and any resulting shift counts.
        await page.getByRole('button', { name: 'Sign in with passkey' }).evaluate((button) => button.click());
        await page.getByRole('alert').waitFor({ state: 'visible' });
        await settle(page);
        if (viewport === SHORT_VIEWPORT && await page.evaluate(errorHeight) <= room) {
          throw new Error('the error fit below the card, so the short window did not exercise it');
        }
        recordShifts(scenario, await page.evaluate(() => window.__stability.shifts));
      } catch (error) {
        record(scenario, 'error', error.message.split('\n')[0], 'fail');
      } finally {
        await page?.close();
      }
      await context.close();
    }
  } finally {
    vite.kill();
  }
}

function annotate(result) {
  const moved = result.detail?.moved?.slice(0, 3).map((source) => source.node).join('; ');
  const message = `${result.scenario}: ${result.measure} ${result.value}${moved ? ` (moved: ${moved})` : ''}`;
  return `::${result.level === 'fail' ? 'error' : 'warning'} title=Layout stability::${message.replace(/\r?\n/g, ' ')}`;
}

const started = Date.now();
await rm(outDir, { recursive: true, force: true });
await mkdir(outDir, { recursive: true });
const browser = await chromium.launch();
try {
  if (only !== 'harness') await measureBoot(browser);
  if (only !== 'boot') await measureHarness(browser);
} finally {
  await browser.close();
}

const counts = Object.fromEntries(['pass', 'warn', 'fail'].map((level) => [level, results.filter((result) => result.level === level).length]));
await writeFile(
  join(outDir, 'layout-stability.json'),
  `${JSON.stringify({ limits: { layoutShift: LAYOUT_SHIFT, fontSwapShift: FONT_SWAP_SHIFT, canvasDelta: CANVAS_DELTA, keyElements: KEY_ELEMENTS }, counts, results }, null, 2)}\n`,
);
for (const result of results) console.log(`${result.level.toUpperCase().padEnd(4)} ${result.scenario}: ${result.measure} ${result.value}`);
if (process.env.GITHUB_ACTIONS === 'true') {
  for (const result of results.filter((candidate) => candidate.level !== 'pass')) console.log(annotate(result));
}
console.log(`${counts.pass} passed, ${counts.warn} warned, ${counts.fail} failed in ${Math.round((Date.now() - started) / 1000)} s`);
if (counts.fail > 0) process.exitCode = 1;
