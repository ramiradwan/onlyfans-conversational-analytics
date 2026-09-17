import { test, expect } from '@playwright/test';
import { mkdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const STATES = Object.freeze({
  preview: Object.freeze({
    tone: 'info', mode: 'Activity preview enabled', badge: 'Preview', title: 'Preview is ready',
    body: 'Your activity counts update as you use OnlyFans. For insights from your conversations, add Full analysis.',
    primary: 'Activate Full analysis', preview: true,
  }),
  paused: Object.freeze({
    tone: 'info', mode: 'Analytics paused', badge: 'Paused', title: 'Analytics are paused',
    body: 'Nothing new is collected while paused. Resume whenever you are ready.',
    primary: 'Resume analytics', preview: true,
  }),
  desktop_needed: Object.freeze({
    tone: 'warning', mode: 'Full setup in progress', badge: 'Next step', title: 'Desktop app needed for Full analysis',
    body: 'The desktop app download is not available yet. You can keep using Preview in the meantime.',
    desktop: 'Not connected', delivery: 'Off', activation: 'Not checked', analysis: 'Not ready',
  }),
  setup_incomplete: Object.freeze({
    tone: 'warning', mode: 'Full setup in progress', badge: 'Setup needed', title: 'Open your creator account to continue',
    body: 'Open OnlyFans and sign in to the creator account you want to analyze. Then return here to connect the extension.',
    primary: 'Open creator account', secondary: 'Open desktop app', desktop: 'Running', delivery: 'Off',
    activation: 'Not checked', analysis: 'Not ready',
  }),
  pairing_required: Object.freeze({
    tone: 'info', mode: 'Full setup in progress', badge: 'Next step', title: 'Connect this extension to the desktop app',
    body: 'The desktop app is running. Pair it with this extension to start Full analysis.',
    secondary: 'Open desktop app', pairing: 'pair', desktop: 'Running', delivery: 'Off',
    activation: 'Not checked', analysis: 'Not ready',
  }),
  pairing_compare: Object.freeze({
    tone: 'progress', mode: 'Full setup in progress', badge: 'Connecting', title: 'Confirm the connection',
    body: 'Compare the six-digit code here with the code in the desktop app. Confirm only when both codes match.',
    pairing: 'compare', pairingCode: '483 217', desktop: 'Running', delivery: 'Off',
    activation: 'Not checked', analysis: 'Not ready',
  }),
  pairing_failed: Object.freeze({
    tone: 'error', mode: 'Full setup in progress', badge: 'Try again', title: 'Connection was not completed',
    body: 'Open a new connection window in the desktop app, then try again. Nothing is shared until the connection works.',
    primary: 'Try connection again', desktop: 'Running', delivery: 'Off',
    activation: 'Not checked', analysis: 'Not ready',
  }),
  desktop_stopped: Object.freeze({
    tone: 'warning', mode: 'Full setup in progress', badge: 'Needs attention', title: 'Desktop app is not running',
    body: 'Start the desktop app, then try again. Your connection is saved.',
    primary: 'Retry connection', desktop: 'Not running', delivery: 'Off',
    activation: 'Not checked', analysis: 'Not ready',
  }),
  activation_checking: Object.freeze({
    tone: 'progress', mode: 'Desktop connected', badge: 'Checking', title: 'Checking activation',
    body: 'Checking whether Full analysis is active in the desktop app.',
    desktop: 'Running', delivery: 'Connected', activation: 'Checking…', analysis: 'Not ready',
  }),
  activation_required: Object.freeze({
    tone: 'warning', mode: 'Desktop connected', badge: 'Activation required', title: 'Full activation required',
    body: 'To see insights from your conversations, finish activation in Settings in the desktop app.',
    primary: 'Open desktop app', secondary: 'Check activation', desktop: 'Running', delivery: 'Connected',
    activation: 'Required', analysis: 'Waiting for activation',
  }),
  activation_active_analysis_blocked: Object.freeze({
    tone: 'warning', mode: 'Desktop connected', badge: 'Needs attention', title: 'Analysis is not available right now',
    body: 'Full activation is active, but analysis cannot run at the moment. Check again shortly; your saved data is not affected.',
    primary: 'Check again', secondary: 'Open desktop app', desktop: 'Running', delivery: 'Connected',
    activation: 'Active', analysis: 'Not ready',
  }),
  activation_unavailable: Object.freeze({
    tone: 'error', mode: 'Desktop connected', badge: 'Needs attention', title: 'Full activation needs attention',
    body: 'Activation could not be confirmed right now. Check again in a moment; your saved data is not affected.',
    primary: 'Check again', secondary: 'Open desktop app', desktop: 'Running', delivery: 'Connected',
    activation: 'Needs attention', analysis: 'Unavailable',
  }),
  full_ready: Object.freeze({
    tone: 'success', mode: 'Desktop connected', badge: 'Ready', title: 'Full analysis is ready',
    body: 'Everything is connected. Your insights are in the desktop app.',
    primary: 'Open analysis', desktop: 'Running', delivery: 'Connected',
    activation: 'Active', analysis: 'Ready',
  }),
});

async function popupDocument() {
  const [html, css] = await Promise.all([
    readFile(path.join(ROOT, 'popup.html'), 'utf8'),
    readFile(path.join(ROOT, 'popup.css'), 'utf8'),
  ]);
  return html
    .replace('<link rel="stylesheet" href="popup.css">', `<style>${css}</style>`)
    .replace('<script src="popup.js"></script>', '');
}

async function renderState(page, state) {
  await page.setViewportSize({ width: 390, height: 600 });
  await page.setContent(await popupDocument());
  await page.evaluate((value) => {
    const byId = (id) => document.getElementById(id);
    byId('mode-label').textContent = value.mode;
    byId('journey-card').dataset.tone = value.tone;
    byId('journey-badge').textContent = value.badge;
    byId('journey-title').textContent = value.title;
    byId('journey-body').textContent = value.body;
    byId('preview-metrics').classList.toggle('hidden', value.preview !== true);
    byId('connection-details').classList.toggle('hidden', value.desktop === undefined);
    if (value.desktop !== undefined) {
      byId('brain-status').textContent = value.desktop;
      byId('delivery-status').textContent = value.delivery;
      byId('activation-status').textContent = value.activation;
      byId('analysis-status').textContent = value.analysis;
      byId('pending-count').textContent = '0';
    }
    for (const [id, label] of [['journey-primary', value.primary], ['journey-secondary', value.secondary]]) {
      const button = byId(id);
      button.textContent = label ?? '';
      button.classList.toggle('hidden', !label);
    }
    if (value.pairing) {
      byId('companion-pairing').classList.remove('hidden');
      byId('pair-companion').classList.toggle('hidden', value.pairing !== 'pair');
      byId('cancel-pairing').classList.toggle('hidden', value.pairing !== 'compare');
    }
    if (value.pairingCode) {
      byId('pairing-code').classList.remove('hidden');
      byId('pairing-code').textContent = value.pairingCode;
    }
  }, state);
}

test('visual state copy matches the popup sources', async () => {
  const sources = (await Promise.all(
    ['popup.html', 'popup.js', 'runtime/customer-journey.mjs'].map((file) => readFile(path.join(ROOT, file), 'utf8')),
  )).join('\n');
  for (const state of Object.values(STATES)) {
    for (const key of ['mode', 'badge', 'title', 'body', 'primary', 'secondary', 'activation', 'analysis']) {
      if (state[key] === undefined) continue;
      const literals = [`'${state[key]}'`, `>${state[key]}<`];
      expect(literals.some((literal) => sources.includes(literal)), `${key}: ${state[key]}`).toBe(true);
    }
  }
});

for (const [name, state] of Object.entries(STATES)) {
  test(`visual state: ${name}`, async ({ page }, testInfo) => {
    await renderState(page, state);
    await expect(page.locator('#journey-title')).toHaveText(state.title);
    await expect(page.locator('#journey-card')).toHaveAttribute('data-tone', state.tone);
    if (state.desktop !== undefined) {
      await expect(page.locator('#activation-status')).toHaveText(state.activation);
      await expect(page.locator('#analysis-status')).toHaveText(state.analysis);
    }
    const output = process.env.OFCA_UX_SCREENSHOT_DIR
      ? path.join(process.env.OFCA_UX_SCREENSHOT_DIR, `extension-${name}.png`)
      : testInfo.outputPath(`extension-${name}.png`);
    await mkdir(path.dirname(output), { recursive: true });
    await page.screenshot({ path: output, fullPage: true });
    await testInfo.attach(`extension-${name}`, { path: output, contentType: 'image/png' });
  });
}
