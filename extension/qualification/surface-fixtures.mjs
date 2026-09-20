// Synthetic inputs for the production page renderers. No live customer data or permissions.
import { readFile } from 'node:fs/promises';
import { build } from 'esbuild';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const make = (surface, mode = 'full', extras = {}) => ({ surface, mode, paired: false, reachable: true, ...extras });
export const SURFACE_STATES = Object.freeze({
  off: Object.freeze(make('popup', 'off')),
  preview: Object.freeze(make('popup', 'preview')),
  paused: Object.freeze(make('popup', 'paused', { resume: 'preview' })),
  popup_connect: Object.freeze(make('popup')),
  popup_comparing: Object.freeze(make('popup', 'full', { pairing: 'compare' })),
  full_ready: Object.freeze(make('popup', 'full', { paired: true, ready: true })),
  software_activation: Object.freeze(make('setup', 'off', { agreement: true })),
  software_activation_ready: Object.freeze(make('setup', 'off', { agreement: true, accepted: true })),
  legal_unavailable: Object.freeze(make('setup', 'off', { configured: false })),
  mode_choice: Object.freeze(make('setup', 'off', { choice: true })),
  mode_choice_full: Object.freeze(make('setup', 'off', { choice: true, hash: 'full' })),
  full_review: Object.freeze(make('setup', 'preview', { hash: 'full' })),
  preview_complete: Object.freeze(make('setup', 'preview')),
  permission_required: Object.freeze(make('setup', 'preview', { phase: 'permission_required' })),
  reload_required: Object.freeze(make('setup', 'preview', { reload: true })),
  desktop_app_needed: Object.freeze(make('setup', 'full', { reachable: false, download: true })),
  desktop_app_unavailable: Object.freeze(make('setup', 'full', { paired: true, reachable: false })),
  setup_incomplete: Object.freeze(make('setup', 'full', { pairing: 'setup_incomplete' })),
  pairing_required: Object.freeze(make('setup')),
  pairing_waiting: Object.freeze(make('setup', 'full', { pairing: 'pairing' })),
  pairing_compare: Object.freeze(make('setup', 'full', { pairing: 'compare' })),
  pairing_failed: Object.freeze(make('setup', 'full', { pairing: 'pairing_failed' })),
  pairing_not_ready: Object.freeze(make('setup', 'full', { pairing: 'desktop_not_ready' })),
  activation_checking: Object.freeze(make('setup', 'full', { paired: true, checking: true })),
  activation_required: Object.freeze(make('setup', 'full', { paired: true, commercial: 'required' })),
  activation_unavailable: Object.freeze(make('setup', 'full', { paired: true, commercial: 'unavailable' })),
  activation_active: Object.freeze(make('setup', 'full', { paired: true })),
  setup_complete: Object.freeze(make('setup', 'full', { paired: true, ready: true })),
  full_unavailable: Object.freeze(make('setup', 'full', { paired: true, phase: 'unavailable' })),
  connection: Object.freeze(make('options', 'full', { paired: true, ready: true, hash: 'connection' })),
  manage: Object.freeze(make('options', 'preview')),
  delete_confirmation: Object.freeze(make('options', 'full', { paired: true, dialog: true })),
  runtime_unavailable: Object.freeze(make('setup', 'off', { runtimeUnavailable: true })),
  reauthorization: Object.freeze(make('setup', 'paused', { agreement: true, reauthorization: true })),
});

const bundles = new Map();
async function bundle(surface) {
  if (!bundles.has(surface)) bundles.set(surface, build({ entryPoints: [path.join(ROOT, `${surface}.js`)],
    bundle: true, format: 'iife', write: false, logLevel: 'silent', plugins: [{ name: 'fixture-download', setup(context) {
      context.onLoad({ filter: /customer-release-config\.mjs$/ }, () => ({ contents:
        `export const customerReleaseConfig = { desktop_app_download_url: 'https://downloads.example.test/desktop' };`, loader: 'js' }));
    } }] }).then((result) => result.outputFiles[0].text));
  return bundles.get(surface);
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
  const script = await surfaceBundle(state.surface);
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
