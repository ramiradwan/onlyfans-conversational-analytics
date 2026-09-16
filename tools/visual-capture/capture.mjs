// Captures production views from the frontend story harness in fixed states,
// themes and widths. Usage: node capture.mjs [outDir]
// VISUAL_CAPTURE_ONLY=home,settings limits the run to the named workspaces.
import { spawn } from 'node:child_process';
import { mkdir, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:net';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { chromium } from 'playwright';

const here = dirname(fileURLToPath(import.meta.url));
const frontend = resolve(here, '../../frontend');
const outDir = resolve(process.argv[2] ?? join(here, 'output'));

const FIXED_NOW = '2026-06-30T12:05:00.000Z';
const MAX_HEIGHT = 6000;
const MODES = ['light', 'dark'];
const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'narrow', width: 390, height: 844 },
];

const heading = (name) => (page) => page.getByRole('heading', { level: 1, name });
const text = (value) => (page) => page.getByText(value, { exact: true }).first();

/** Each screen names the locator that proves its state rendered before capture. */
const SCREENS = [
  { workspace: 'home', state: 'loading', ready: text('Processing your data…') },
  { workspace: 'home', state: 'fresh', ready: (page) => page.getByRole('link', { name: 'Continue setup' }) },
  { workspace: 'home', state: 'syncing', ready: (page) => page.getByRole('progressbar', { name: /History \d+% synced/ }) },
  { workspace: 'home', state: 'populated', ready: (page) => page.getByRole('region', { name: 'Overview' }) },
  { workspace: 'analytics', state: 'loading', ready: (page) => page.getByRole('main').getByRole('status').first() },
  { workspace: 'analytics', state: 'building', ready: text('Updating your analytics') },
  { workspace: 'analytics', state: 'unavailable', ready: text('Analytics are unavailable') },
  { workspace: 'analytics', state: 'baseline', ready: text('Early estimates') },
  { workspace: 'analytics', state: 'model', ready: (page) => page.getByRole('region', { name: 'Your replies' }) },
  { workspace: 'analytics', state: 'error', ready: (page) => page.getByRole('main').getByRole('alert') },
  ...['loading', 'fresh', 'syncing', 'populated'].map((state) => ({
    workspace: 'inbox',
    state,
    ready: heading('Inbox'),
  })),
  ...['loading', 'fresh', 'syncing', 'populated'].map((state) => ({
    workspace: 'settings',
    state,
    ready: heading('Settings'),
  })),
  { workspace: 'passkey', state: 'resting', ready: heading('Sign in to Conversation Analytics') },
  {
    workspace: 'passkey',
    state: 'cancelled',
    act: (page) => page.getByRole('button', { name: 'Sign in with passkey' }).click(),
    ready: (page) => page.getByRole('alert').filter({ hasText: 'Sign-in was cancelled or timed out.' }),
  },
];

const only = process.env.VISUAL_CAPTURE_ONLY?.split(',').filter(Boolean);
const selectedScreens = only?.length ? SCREENS.filter((screen) => only.includes(screen.workspace)) : SCREENS;

function freePort() {
  return new Promise((resolvePort, reject) => {
    const server = createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolvePort(port));
    });
  });
}

async function startHarness() {
  const port = await freePort();
  const vite = spawn(
    process.execPath,
    [join(frontend, 'node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', String(port), '--strictPort'],
    { cwd: frontend, env: { ...process.env, BROWSER: 'none' }, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  let log = '';
  vite.stdout.on('data', (chunk) => { log += chunk; });
  vite.stderr.on('data', (chunk) => { log += chunk; });
  const base = `http://127.0.0.1:${port}/visual-harness.html`;
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (vite.exitCode !== null) throw new Error(`Vite exited early:\n${log}`);
    try {
      if ((await fetch(base)).ok) return { base, vite };
    } catch {
      // Server not listening yet.
    }
    await new Promise((wait) => setTimeout(wait, 250));
  }
  vite.kill();
  throw new Error(`Vite did not start within 60 s:\n${log}`);
}

/** Height of content the app shell clips without offering a scroll container. */
function shellClipping(page) {
  return page.evaluate(() => {
    const frame = document.querySelector('#main-content > div');
    return frame ? Math.max(0, frame.scrollHeight - frame.clientHeight - 1) : 0;
  });
}

/** Grows the viewport so content inside the app's scroll containers is fully visible. */
async function fitContent(page, viewport) {
  const hidden = await page.evaluate(() => {
    let most = 0;
    for (const element of document.querySelectorAll('body *')) {
      if (!/(auto|scroll)/.test(getComputedStyle(element).overflowY)) continue;
      most = Math.max(most, element.scrollHeight - element.clientHeight);
    }
    return most;
  });
  const height = Math.min(MAX_HEIGHT, viewport.height + hidden);
  if (height !== viewport.height) {
    await page.setViewportSize({ width: viewport.width, height });
  }
  return height;
}

async function capture() {
  await rm(outDir, { recursive: true, force: true });
  await mkdir(outDir, { recursive: true });
  const { base, vite } = await startHarness();
  const browser = await chromium.launch();
  const entries = [];
  const failures = [];
  try {
    for (const viewport of VIEWPORTS) {
      for (const mode of MODES) {
        const context = await browser.newContext({
          colorScheme: mode,
          deviceScaleFactor: 1,
          locale: 'en-US',
          reducedMotion: 'reduce',
          timezoneId: 'UTC',
          viewport: { width: viewport.width, height: viewport.height },
        });
        for (const screen of selectedScreens) {
          const page = await context.newPage();
          const errors = [];
          page.on('pageerror', (error) => errors.push(String(error)));
          await page.clock.setFixedTime(FIXED_NOW);
          const url = `${base}?workspace=${screen.workspace}&state=${screen.state}&mode=${mode}`;
          const name = `${screen.workspace}-${screen.state}-${mode}-${viewport.name}`;
          try {
            await page.goto(url, { waitUntil: 'networkidle' });
            await page.evaluate(() => document.fonts.ready);
            if (screen.act) await screen.act(page);
            await screen.ready(page).waitFor({ state: 'visible', timeout: 15_000 });
            await page.waitForLoadState('networkidle');
            const height = await fitContent(page, viewport);
            const clipped = await shellClipping(page);
            if (clipped > 0) errors.push(`${clipped}px of content is clipped and cannot be scrolled to`);
            const file = join(outDir, screen.workspace, `${name}.png`);
            await mkdir(dirname(file), { recursive: true });
            await page.screenshot({ path: file, animations: 'disabled', caret: 'hide', fullPage: true });
            if (errors.length) throw new Error(errors.join('\n'));
            entries.push({
              file: relative(outDir, file).replaceAll('\\', '/'),
              workspace: screen.workspace,
              state: screen.state,
              mode,
              viewport: viewport.name,
              width: viewport.width,
              foldHeight: viewport.height,
              capturedHeight: height,
            });
            console.log(`captured ${name}`);
          } catch (error) {
            failures.push(`${name}: ${error.message.split('\n')[0]}`);
            console.error(`failed ${name}: ${error.message}`);
          } finally {
            await page.close();
          }
        }
        await context.close();
      }
    }
  } finally {
    await browser.close();
    vite.kill();
  }
  await writeFile(
    join(outDir, 'manifest.json'),
    `${JSON.stringify({ revision: process.env.VISUAL_CAPTURE_REVISION ?? null, fixedNow: FIXED_NOW, entries, failures }, null, 2)}\n`,
  );
  if (failures.length) {
    console.error(`${failures.length} screen(s) did not reach their ready state:\n${failures.join('\n')}`);
    process.exitCode = 1;
  }
}

await capture();
