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
  { name: 'tablet', width: 820, height: 900, targetedOnly: true },
];

const heading = (name) => (page) => page.getByRole('heading', { level: 1, name });
const text = (value) => (page) => page.getByText(value, { exact: true }).first();

/** Each screen names the locator that proves its state rendered before capture. */
const SCREENS = [
  { workspace: 'home', state: 'loading', ready: text('Processing your data…') },
  {
    workspace: 'home',
    state: 'fresh',
    ready: (page) => page.getByRole('link', { name: 'Continue setup' }),
    assert: async (page, viewport) => {
      if (viewport.name !== 'desktop') return;
      const prompt = page.locator('[data-visual="setup-prompt"]');
      await assertMaxWidth(prompt, 560);
      await assertCentered(prompt, page.getByRole('main'));
    },
  },
  { workspace: 'home', state: 'syncing', ready: (page) => page.getByRole('progressbar', { name: /History \d+% synced/ }) },
  { workspace: 'home', state: 'populated', ready: (page) => page.getByRole('region', { name: 'Overview' }) },
  {
    workspace: 'analytics',
    state: 'loading',
    ready: (page) => page.getByRole('main').getByRole('status').first(),
    assert: assertLoadingGeometry,
  },
  { workspace: 'analytics', state: 'building', ready: text('Updating your analytics') },
  { workspace: 'analytics', state: 'unavailable', ready: text('Analytics are unavailable') },
  { workspace: 'analytics', state: 'baseline', ready: text('Early estimates') },
  {
    workspace: 'analytics',
    state: 'model',
    ready: (page) => page.getByRole('region', { name: 'Your replies' }),
    assert: async (page, viewport) => {
      await assertMetricHierarchy(page);
      if (viewport.name === 'desktop') {
        const replies = page.getByRole('region', { name: 'Your replies' });
        const box = await replies.boundingBox();
        if (!box || box.width < 280) throw new Error('Your replies panel became too narrow');
      }
    },
  },
  {
    workspace: 'analytics',
    state: 'error',
    ready: (page) => page.getByRole('main').getByRole('alert'),
    assert: async (page, viewport) => {
      const state = page.locator('[data-visual="analytics-empty-state"]');
      await assertMaxWidth(state, 640);
      if (viewport.name === 'desktop') await assertCentered(state, page.getByRole('main'));
    },
  },
  ...['loading', 'fresh', 'syncing', 'populated'].map((state) => ({ workspace: 'inbox', state, ready: heading('Inbox') })),
  ...['loading', 'fresh', 'syncing', 'populated'].map((state) => ({
    workspace: 'settings',
    state,
    ready: heading('Settings'),
    assert: async (page, viewport) => {
      if (viewport.name !== 'desktop') return;
      const frame = page.locator('[data-visual="settings-frame"]');
      await assertMaxWidth(frame, 880);
      await assertCentered(frame, page.getByRole('main'));
    },
  })),
  { workspace: 'passkey', state: 'resting', ready: heading('Protect access to your messages') },
  {
    workspace: 'passkey', state: 'cancelled',
    act: (page) => page.getByRole('button', { name: 'Sign in with passkey' }).click(),
    ready: (page) => page.getByRole('alert').filter({ hasText: 'Sign-in was cancelled or timed out.' }),
  },
  {
    workspace: 'analytics', state: 'model', variant: 'date-expanded', viewports: ['desktop', 'narrow'],
    act: (page) => page.getByRole('button', { name: 'Change dates' }).click(),
    ready: (page) => page.getByLabel('Start date'),
  },
  {
    workspace: 'inbox', state: 'populated', variant: 'selected-conversation', viewports: ['narrow'],
    act: (page) => page.getByRole('list', { name: 'Conversation list' }).getByRole('button').first().click(),
    ready: (page) => page.getByRole('button', { name: 'Back to conversations' }),
  },
  {
    workspace: 'inbox', state: 'populated', variant: 'tablet-list', viewports: ['tablet'], modes: ['light'],
    ready: (page) => page.getByRole('list', { name: 'Conversation list' }),
  },
  {
    workspace: 'inbox', state: 'populated', variant: 'tablet-selected-conversation', viewports: ['tablet'], modes: ['light'],
    act: (page) => page.getByRole('list', { name: 'Conversation list' }).getByRole('button').first().click(),
    ready: (page) => page.getByRole('button', { name: 'Back to conversations' }),
  },
  {
    workspace: 'home', state: 'populated', variant: 'mobile-navigation-open', viewports: ['narrow'], modes: ['light'],
    act: (page) => page.getByRole('button', { name: 'Open navigation' }).click(),
    ready: (page) => page.locator('#mobile-navigation'),
  },
  {
    workspace: 'home', state: 'populated', variant: 'status-open', viewports: ['narrow'], modes: ['light'],
    act: (page) => page.getByRole('button', { name: /Status: .*Show details/ }).click(),
    ready: (page) => page.getByRole('dialog', { name: 'Status details' }),
  },
  {
    workspace: 'settings', state: 'fresh', variant: 'activation-expanded', viewports: ['narrow'], modes: ['light'],
    act: (page) => page.getByRole('button', { name: 'Turn on full analytics' }).click(),
    ready: (page) => page.getByRole('link', { name: 'Open secure setup' }),
  },
  {
    workspace: 'settings', state: 'fresh', variant: 'activation-return', viewports: ['narrow'], modes: ['light'],
    act: async (page) => {
      await page.getByRole('button', { name: 'Turn on full analytics' }).click();
      await page.getByRole('link', { name: 'Open secure setup' }).dispatchEvent('click');
    },
    ready: (page) => page.getByRole('status').filter({ hasText: 'Secure setup opened.' }),
  },
  {
    workspace: 'settings', state: 'fresh', variant: 'activation-error', viewports: ['narrow'], modes: ['light'],
    act: async (page) => {
      await page.getByRole('button', { name: 'Turn on full analytics' }).click();
      await page.getByRole('textbox', { name: 'Activation code' }).fill('invalid');
      await page.getByRole('button', { name: 'Activate', exact: true }).click();
    },
    ready: (page) => page.getByRole('alert').filter({ hasText: 'Enter the full activation code and try again.' }),
  },
  {
    workspace: 'settings', state: 'fresh', variant: 'pairing-comparison', viewports: ['narrow'], modes: ['light'],
    act: (page) => page.getByRole('button', { name: 'Connect extension' }).click(),
    ready: (page) => page.getByText('Check the code', { exact: true }),
  },
  {
    workspace: 'settings', state: 'fresh', variant: 'history-consent', viewports: ['narrow'], modes: ['light'],
    act: async (page) => {
      await page.getByRole('button', { name: 'Connect extension' }).click();
      await page.getByText('Check the code', { exact: true }).waitFor({ state: 'visible' });
      await page.getByRole('checkbox', { name: 'The codes match' }).check();
      await page.getByRole('button', { name: 'Confirm connection' }).click();
    },
    ready: (page) => page.getByRole('checkbox', { name: /I allow read-only syncing/ }),
  },
  {
    workspace: 'settings', state: 'fresh', variant: 'archive-editing', viewports: ['narrow'], modes: ['light'],
    act: async (page) => {
      await page.getByRole('button', { name: 'Manage stored messages' }).click();
      await page.getByRole('button', { name: 'Turn on archive' }).click();
    },
    ready: (page) => page.getByRole('spinbutton', { name: 'Days to keep' }),
  },
  {
    workspace: 'settings', state: 'populated', variant: 'delete-disclosure', viewports: ['narrow'], modes: ['light'],
    act: async (page) => {
      await page.getByRole('button', { name: 'Manage stored messages' }).click();
      await page.getByRole('button', { name: 'Delete messages' }).click();
    },
    ready: (page) => page.getByRole('button', { name: 'Delete all messages' }),
  },
  {
    workspace: 'settings', state: 'populated', variant: 'delete-confirmation', viewports: ['narrow'], modes: ['light'],
    act: async (page) => {
      await page.getByRole('button', { name: 'Manage stored messages' }).click();
      await page.getByRole('button', { name: 'Delete messages' }).click();
      await page.getByRole('button', { name: 'Delete all messages' }).click();
    },
    ready: (page) => page.getByRole('dialog').filter({ hasText: 'Delete all messages?' }),
  },
  {
    workspace: 'settings', state: 'loading', variant: 'linked-reconnecting', viewports: ['narrow'], modes: ['light'],
    ready: (page) => page.getByText('Extension linked to this app', { exact: true }),
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

async function assertCentered(subject, container, tolerance = 16) {
  const [subjectBox, containerBox] = await Promise.all([subject.boundingBox(), container.boundingBox()]);
  if (!subjectBox || !containerBox) throw new Error('visual assertion target was not measurable');
  const subjectCenter = subjectBox.x + subjectBox.width / 2;
  const containerCenter = containerBox.x + containerBox.width / 2;
  const delta = Math.abs(subjectCenter - containerCenter);
  if (delta > tolerance) throw new Error(`visual centering drifted by ${delta.toFixed(1)}px`);
}

async function assertMaxWidth(locator, maximum, tolerance = 1) {
  const box = await locator.boundingBox();
  if (!box) throw new Error('visual assertion target was not measurable');
  if (box.width > maximum + tolerance) {
    throw new Error(`visual width ${box.width.toFixed(1)}px exceeds ${maximum}px`);
  }
}

async function assertMetricHierarchy(page) {
  const values = page.locator('[data-visual="reply-metric-value"]');
  if (await values.count() !== 3) throw new Error('Your replies must expose exactly three metric values');
  const sizes = await values.evaluateAll((nodes) =>
    nodes.map((node) => Number.parseFloat(getComputedStyle(node).fontSize)),
  );
  if (sizes.some((size) => size < 27.5)) {
    throw new Error(`reply metric typography regressed: ${sizes.join(', ')}px`);
  }
}

async function assertBrandMarkIfPresent(page) {
  const tile = page.locator('[data-visual="brand-tile"]').first();
  if (await tile.count() === 0) return;
  const box = await tile.boundingBox();
  if (!box || Math.abs(box.width - 32) > 1 || Math.abs(box.height - 32) > 1) {
    throw new Error('brand tile must remain 32×32px');
  }
  if (await tile.locator('svg[data-brand-mark="conversation-analytics"]').count() !== 1) {
    throw new Error('approved Conversation Analytics mark is missing');
  }
}

async function assertLoadingGeometry(page, viewport) {
  const primary = await page.locator('[data-visual="analytics-loading-primary"]').boundingBox();
  const replies = await page.locator('[data-visual="analytics-loading-replies"]').boundingBox();
  const topics = await page.locator('[data-visual="analytics-loading-topics"]').boundingBox();
  if (!primary || !replies || !topics) throw new Error('analytics loading geometry was not measurable');
  if (viewport.name === 'desktop') {
    if (Math.abs(primary.y - replies.y) > 2) throw new Error('analytics loading panels no longer share a row');
    if (topics.width <= primary.width) throw new Error('analytics topics loading panel must remain full width');
  } else if (viewport.name === 'narrow' && replies.y <= primary.y) {
    throw new Error('analytics loading panels must stack on narrow screens');
  }
}

/** Height of content the app shell clips without offering a scroll container. */
function shellClipping(page) {
  return page.evaluate(() => {
    const frame = document.querySelector('#main-content > div');
    return frame ? Math.max(0, frame.scrollHeight - frame.clientHeight - 1) : 0;
  });
}

/** Width by which the page scrolls sideways; layouts are expected to fit the viewport width. */
function horizontalOverflow(page) {
  return page.evaluate(() => Math.max(
    0,
    document.documentElement.scrollWidth - document.documentElement.clientWidth - 1,
    document.body.scrollWidth - document.body.clientWidth - 1,
  ));
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

async function screenshot(page, file, fullPage) {
  await mkdir(dirname(file), { recursive: true });
  await page.screenshot({ path: file, animations: 'disabled', caret: 'hide', fullPage });
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
          if (viewport.targetedOnly && !screen.viewports?.includes(viewport.name)) continue;
          if (screen.viewports && !screen.viewports.includes(viewport.name)) continue;
          if (screen.modes && !screen.modes.includes(mode)) continue;
          const page = await context.newPage();
          const errors = [];
          page.on('pageerror', (error) => errors.push(String(error)));
          await page.clock.setFixedTime(FIXED_NOW);
          const url = `${base}?workspace=${screen.workspace}&state=${screen.state}&mode=${mode}`;
          const variant = screen.variant ? `-${screen.variant}` : '';
          const name = `${screen.workspace}-${screen.state}${variant}-${mode}-${viewport.name}`;
          try {
            await page.goto(url, { waitUntil: 'networkidle' });
            await page.evaluate(() => document.fonts.ready);
            if (screen.act) await screen.act(page);
            await screen.ready(page).waitFor({ state: 'visible', timeout: 15_000 });
            await page.waitForLoadState('networkidle');
            await assertBrandMarkIfPresent(page);
            if (screen.assert) await screen.assert(page, viewport);

            const overflow = await horizontalOverflow(page);
            if (overflow > 0) errors.push(`${overflow}px of unintended horizontal page overflow`);

            const foldFile = join(outDir, screen.workspace, `${name}-fold.png`);
            await screenshot(page, foldFile, false);
            entries.push({
              file: relative(outDir, foldFile).replaceAll('\\', '/'),
              capture: 'fold',
              workspace: screen.workspace,
              state: screen.state,
              variant: screen.variant ?? null,
              mode,
              viewport: viewport.name,
              width: viewport.width,
              height: viewport.height,
            });

            const height = await fitContent(page, viewport);
            const clipped = await shellClipping(page);
            if (clipped > 0) errors.push(`${clipped}px of content is clipped and cannot be scrolled to`);
            const fullFile = join(outDir, screen.workspace, `${name}-full.png`);
            await screenshot(page, fullFile, true);
            entries.push({
              file: relative(outDir, fullFile).replaceAll('\\', '/'),
              capture: 'full',
              workspace: screen.workspace,
              state: screen.state,
              variant: screen.variant ?? null,
              mode,
              viewport: viewport.name,
              width: viewport.width,
              foldHeight: viewport.height,
              capturedHeight: height,
            });

            if (errors.length) throw new Error(errors.join('\n'));
            console.log(`captured ${name} fold + full`);
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