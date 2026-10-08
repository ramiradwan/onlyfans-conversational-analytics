// Captures production views from the frontend story harness in fixed states,
// themes and widths. Usage: node capture.mjs [outDir]
// VISUAL_CAPTURE_ONLY=home,settings limits the run to the named workspaces.
import { spawn } from 'node:child_process';
import { mkdir, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:net';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { chromium } from 'playwright';
import { createScreens, FIXED_NOW } from './screen-matrix.mjs';

import { runCaptureJobs } from './capture-jobs.mjs';
import { captureStaticSurfaces } from './static-surfaces.mjs';
import { installWatcher, readWatcher } from './shift-watcher.mjs';
import { captureFreshnessTransitions } from './freshness-transitions.mjs';
import { captureDynamicTransitions, REQUIRED_REGIONS } from './dynamic-transitions.mjs';

import { captureReviewChecks } from './review-contracts.mjs';
import { assertNumericTypography } from './appearance-contracts.mjs';
import { captureVisionDiagnostics } from './vision-diagnostics.mjs';
import { ordinaryCases, ordinaryName } from './ci/inventory.mjs';
import { CaptureRecorder, parseCaptureOptions, sourceIdentity } from './ci/recording.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const frontend = resolve(here, '../../frontend');

const MAX_HEIGHT = 6000;
export { FIXED_NOW } from './screen-matrix.mjs';
export const SCREENS = createScreens({ assertMaxWidth, assertCentered, assertLoadingGeometry, assertMetricHierarchy });

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

export async function startHarness() {
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
  const tile = page.locator('[data-visual="brand-tile"]').filter({ visible: true }).first();
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

export async function capture(argv = process.argv.slice(2)) {
  const options = parseCaptureOptions(argv, process.env, join(here, 'output'));
  const outDir = options.output;
  const repository = resolve(here, '../..');
  // Never recursively clear the repository, a parent, or a drive root.
  if (outDir === repository || relative(outDir, repository).split(/[\\/]/).every(part => part !== '..')) throw new Error('visual_ci_unsafe_output_directory');
  const source = sourceIdentity(process.env, repository);
  const previousRevision = process.env.VISUAL_CAPTURE_REVISION;
  const recorder = new CaptureRecorder({ group: options.group, source, partial: options.partial });
  const only = process.env.VISUAL_CAPTURE_ONLY?.split(',').filter(Boolean);
  const selectedScreens = only?.length ? SCREENS.filter(screen => only.includes(screen.workspace)) : SCREENS;
  const remaining = options.group !== 'dynamic', dynamicGroup = options.group !== 'remaining';
  const phaseTimings = {};
  let phase = 'prepare', started = performance.now();
  const nextPhase = (next) => {
    phaseTimings[phase] = (performance.now() - started) / 1000;
    console.log('capture phase ' + phase + ': ' + phaseTimings[phase].toFixed(3) + 's');
    phase = next;
    started = performance.now();
  };
  await rm(outDir, { recursive: true, force: true });
  await mkdir(outDir, { recursive: true });
  await recorder.writeInventory(outDir);
  const entries = [], diagnostics = [], failures = [];
  let review = null, dynamic = [], freshness = [];
  let browser, vite, base, exitCode = 1;
  const progress = setInterval(() => console.log(recorder.progress(phase)), 30_000);
  process.env.VISUAL_CAPTURE_REVISION = source.source_commit;
  try {
    await new Promise((done, reject) => {
      const build = spawn(process.execPath, [join(frontend, 'node_modules/vite/bin/vite.js'), 'build'], { cwd: frontend, stdio: 'inherit' });
      build.once('error', reject);
      build.once('exit', code => code === 0 ? done() : reject(new Error('visual_ci_fixture_build_failed')));
    });
    ({ base, vite } = await startHarness());
    browser = await chromium.launch();
    if (remaining) {
    nextPhase('frontend');
    const cases = ordinaryCases(selectedScreens);
    await runCaptureJobs(cases, async ({ viewport, mode, screen }) => {
        const name = ordinaryName({ viewport, mode, screen });
        const finish = recorder.begin(`ordinary-${name}`, { workspace: screen.workspace, state: screen.state, variant: screen.variant ?? null, mode, viewport: viewport.name, width: viewport.width, height: viewport.height });
        const observations = [], files = []; let caseFailed = false;
        const context = await browser.newContext({
          colorScheme: mode,
          deviceScaleFactor: 1,
          locale: 'en-US',
          reducedMotion: 'reduce',
          timezoneId: 'UTC',
          viewport: { width: viewport.width, height: viewport.height },
        });

          const page = await context.newPage();
          await installWatcher(page, { requiredRegions: REQUIRED_REGIONS[screen.workspace] });
          const errors = [];
          page.on('pageerror', (error) => errors.push(String(error)));
          await page.clock.setFixedTime(FIXED_NOW);
          const url = `${base}?workspace=${screen.workspace}&state=${screen.state}&mode=${mode}`;
          try {
            await page.goto(url, { waitUntil: 'networkidle' });
            await page.evaluate(() => document.fonts.ready);
            if (screen.act) await screen.act(page);
            await screen.ready(page).waitFor({ state: 'visible', timeout: 15_000 });
            await page.waitForLoadState('networkidle');
            await assertBrandMarkIfPresent(page);
            await assertNumericTypography(page, viewport);
            if (screen.assert) await screen.assert(page, viewport);
            const watcher = await readWatcher(page);
            await writeFile(join(outDir, `${name}-geometry.json`), JSON.stringify({ revision: process.env.VISUAL_CAPTURE_REVISION ?? null, view: screen.workspace, transition: `boot:${screen.state}`, viewport, mode, ...watcher }) + '\n');
            files.push(`${name}-geometry.json`);
            if (watcher.failures.length) throw new Error(watcher.failures.join('\n'));

            const overflow = await horizontalOverflow(page);
            if (overflow > 0) errors.push(`${overflow}px of unintended horizontal page overflow`);

            const foldFile = join(outDir, screen.workspace, `${name}-fold.png`);
            await screenshot(page, foldFile, false);
            observations.push('fold'); files.push(relative(outDir, foldFile).replaceAll('\\', '/'));
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
            observations.push('full'); files.push(relative(outDir, fullFile).replaceAll('\\', '/'));
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

            if (screen.workspace === 'analytics' && screen.state === 'model' && !screen.variant) {
              const captures = await captureVisionDiagnostics(page, outDir, name);
              observations.push(...captures.map(value => `vision:${value.type}`)); files.push(...captures.map(value => value.file));
              diagnostics.push(...captures.map((capture) => ({ ...capture, mode, viewport: viewport.name })));
            }
            if (errors.length) throw new Error(errors.join('\n'));
            console.log(`captured ${name} fold + full`);
          } catch (error) {
            caseFailed = true;
            await page.screenshot({ path: join(outDir, `${name}-failure.png`) });
            await writeFile(join(outDir, `${name}-geometry.json`), JSON.stringify({ revision: process.env.VISUAL_CAPTURE_REVISION ?? null, view: screen.workspace, transition: `boot:${screen.state}`, viewport, mode, error: error.message, ...await readWatcher(page) }) + '\n');
            failures.push(`${name}: ${error.message.split('\n')[0]}`);
            console.error(`failed ${name}: ${error.message}`);
          } finally {
            finish({ outcome: caseFailed ? 'failed' : 'passed', observations, files });
            await page.close();
          }
        await context.close();
    });
    entries.sort((a, b) => a.file.localeCompare(b.file));
    }
    if (dynamicGroup) {
      nextPhase('dynamic');
      dynamic = await captureDynamicTransitions(browser, base, outDir, undefined, false, recorder);
      failures.push(...dynamic.flatMap(report => report.failures.map(failure => `${report.file}: ${failure}`)));
    }
    if (remaining) {
      nextPhase('freshness');
      freshness = await captureFreshnessTransitions(browser, base, outDir, recorder);
      failures.push(...freshness.flatMap(report => report.failures.map(failure => `${report.file}: ${failure}`)));
      nextPhase('review');
      review = await captureReviewChecks(browser, base, outDir, recorder);
      failures.push(...review.failures);
      nextPhase('static');
      const staticReport = await captureStaticSurfaces(browser, outDir, recorder);
      failures.push(...staticReport.failures);
      review.static = { file: 'static-surfaces/acceptance.json', passed: staticReport.checks.length, screenshots: staticReport.entries.length, failures: staticReport.failures.length };
    }
    exitCode = failures.length ? 1 : 0;
  } catch {
    failures.push('visual_ci_capture_incomplete');
    console.error('visual-ci: capture did not complete; restricted receipt will record incomplete execution');
  } finally {
    nextPhase('cleanup');
    try { if (browser) await browser.close(); }
    catch { exitCode = 1; failures.push('visual_ci_cleanup_failed'); }
    finally { vite?.kill(); }
    nextPhase('manifest');
    try {
      const manifest = () => ({ revision: source.source_commit, fixedNow: FIXED_NOW, phaseTimings,
        entries, diagnostics, dynamic, freshness, static: review?.static ?? null,
        review: review && { file: 'review/acceptance.json', passed: review.checks.length, failures: review.failures.length }, failures });
      await writeFile(join(outDir, 'manifest.json'), JSON.stringify(manifest(), null, 2) + '\n');
      phaseTimings.manifest = (performance.now() - started) / 1000;
      await writeFile(join(outDir, 'manifest.json'), JSON.stringify(manifest(), null, 2) + '\n');
      const receipt = await recorder.finish(outDir, { exitCode, phaseTimings });
      if (receipt.exit_code) process.exitCode = receipt.exit_code;
      console.log(`visual-ci complete group=${options.group} status=${receipt.complete ? 'passed' : 'failed'} completed=${receipt.cases.length}`);
    } finally {
      clearInterval(progress);
      if (previousRevision === undefined) delete process.env.VISUAL_CAPTURE_REVISION;
      else process.env.VISUAL_CAPTURE_REVISION = previousRevision;
    }
  }
  if (failures.length) process.exitCode = 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  capture().catch(() => { console.error('visual-ci: unable to complete capture evidence'); process.exitCode = 1; });
}
