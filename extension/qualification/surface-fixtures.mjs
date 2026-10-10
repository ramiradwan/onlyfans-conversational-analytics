// Synthetic inputs for the production page renderers. No live customer data or permissions.
import { readFile } from 'node:fs/promises';
import { build } from 'esbuild';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
import { SURFACE_STATES } from './surface-states.mjs';
export { SURFACE_STATES } from './surface-states.mjs';

const bundles = new Map();
async function bundle(surface, receiving = false) {
  const key = `${surface}:${receiving}`;
  if (!bundles.has(key)) bundles.set(key, build({ entryPoints: [path.join(ROOT, `${surface}.js`)],
    bundle: true, format: 'iife', write: false, logLevel: 'silent', plugins: [{ name: 'fixture-download', setup(context) {
      context.onLoad({ filter: /customer-release-config\.mjs$/ }, () => ({ contents:
        `export const customerReleaseConfig = { desktop_app_download_url: 'https://downloads.example.test/desktop' };`, loader: 'js' }));
      if (receiving) context.onLoad({ filter: /onboarding-release-config\.mjs$/ }, () => ({ contents:
        `export const onboardingHostedOrigin = 'https://setup.example.test';`, loader: 'js' }));
    } }] }).then((result) => result.outputFiles[0].text));
  return bundles.get(key);
}

export const surfaceBundle = bundle;
export async function surfaceDocument(state) {
  return readFile(path.join(ROOT, `${state.surface}.html`), 'utf8');
}
export async function surfaceStyles(name) {
  if (!['popup.css', 'setup.css'].includes(name)) throw new Error('Unknown surface stylesheet');
  return readFile(path.join(ROOT, name), 'utf8');
}

import { installSurfaceFixture } from './surface-runtime-fixture.mjs';
export async function renderSurfaceState(page, state) {
  const html = await surfaceDocument(state);
  const script = await surfaceBundle(state.surface, Boolean(state.receiving));
  await page.addInitScript(installSurfaceFixture, state);
  await page.route('http://extension-ui.test/**', async (route) => {
    const name = new URL(route.request().url()).pathname.slice(1);
    if (name.endsWith('.html')) return route.fulfill({ contentType: 'text/html', body: html });
    if (name === `${state.surface}.js`) return route.fulfill({ contentType: 'text/javascript', body: script });
    if (name.endsWith('.css')) return route.fulfill({ contentType: 'text/css', body: await surfaceStyles(name) });
    return route.fulfill({ status: 404 });
  });
  await page.goto(`http://extension-ui.test/${state.surface}.html${state.hash ? '#' + state.hash : ''}`, { waitUntil: 'load' });
  await page.waitForFunction(() => document.querySelector('main[data-ready="true"]')
    || document.querySelector('#runtime-unavailable:not(.hidden)'));
  await page.evaluate(() => document.fonts.ready);
  if (state.dialog) await page.locator('#delete-local-data').click();
}
