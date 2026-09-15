import { test, expect } from '@playwright/test';
import { mkdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const STATES = Object.freeze({
  preview: Object.freeze({
    tone: 'info', mode: 'Activity preview enabled', badge: 'Preview', title: 'Preview is ready',
    body: 'You can keep using Preview without the desktop app. Activate Full analysis when you want message-level insights.',
    primary: 'Activate Full analysis', desktop: 'Not connected', delivery: 'Inactive',
  }),
  desktop_needed: Object.freeze({
    tone: 'warning', mode: 'Full setup in progress', badge: 'Next step', title: 'Desktop app needed for Full analysis',
    body: 'The desktop app download is not available from this release yet. You can keep using Preview in the meantime.',
    desktop: 'Not connected', delivery: 'Inactive',
  }),
  setup_incomplete: Object.freeze({
    tone: 'warning', mode: 'Full setup in progress', badge: 'Setup needed', title: 'Open your creator account to continue',
    body: 'Open OnlyFans and sign in to the creator account you want to analyze. Then return here to connect the extension.',
    primary: 'Open creator account', secondary: 'Open desktop app', desktop: 'Running', delivery: 'Inactive',
  }),
  pairing_compare: Object.freeze({
    tone: 'progress', mode: 'Full setup in progress', badge: 'Connecting', title: 'Confirm the connection',
    body: 'Compare the six-digit code here with the code in the desktop app. Confirm only when both codes match.',
    secondary: 'Cancel', desktop: 'Running', delivery: 'Inactive', pairingCode: '483 217',
  }),
  pairing_failed: Object.freeze({
    tone: 'error', mode: 'Full setup in progress', badge: 'Try again', title: 'Connection was not completed',
    body: 'Open a new connection window in the desktop app, then try again. No Full data is sent until the connection succeeds.',
    primary: 'Try connection again', desktop: 'Running', delivery: 'Inactive',
  }),
  desktop_stopped: Object.freeze({
    tone: 'warning', mode: 'Full mode connecting', badge: 'Needs attention', title: 'Desktop app is not running',
    body: 'Your previous connection is saved. Start the desktop app, then retry. Preview remains available while Full analysis is offline.',
    primary: 'Retry connection', desktop: 'Not running', delivery: 'Inactive',
  }),
  full_ready: Object.freeze({
    tone: 'success', mode: 'Full mode ready', badge: 'Ready', title: 'Full mode is ready',
    body: 'The extension is connected to the desktop app and Full analysis can receive new activity.',
    primary: 'Open analysis', desktop: 'Running', delivery: 'Connected',
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
    byId('brain-status').textContent = value.desktop;
    byId('delivery-status').textContent = value.delivery;
    byId('pending-count').textContent = '0';
    for (const [id, label] of [['journey-primary', value.primary], ['journey-secondary', value.secondary]]) {
      const button = byId(id);
      button.textContent = label ?? '';
      button.classList.toggle('hidden', !label);
    }
    if (value.pairingCode) {
      const block = byId('companion-pairing');
      block.classList.remove('hidden');
      byId('pairing-code').classList.remove('hidden');
      byId('pairing-code').textContent = value.pairingCode;
      byId('pairing-status').textContent = 'Compare this code with the desktop app. Confirm there only if both codes match.';
      byId('pair-companion').classList.add('hidden');
      byId('cancel-pairing').classList.remove('hidden');
    }
  }, state);
}

for (const [name, state] of Object.entries(STATES)) {
  test(`visual state: ${name}`, async ({ page }, testInfo) => {
    await renderState(page, state);
    await expect(page.locator('#journey-title')).toHaveText(state.title);
    await expect(page.locator('#journey-card')).toHaveAttribute('data-tone', state.tone);
    const output = process.env.OFCA_UX_SCREENSHOT_DIR
      ? path.join(process.env.OFCA_UX_SCREENSHOT_DIR, `extension-${name}.png`)
      : testInfo.outputPath(`extension-${name}.png`);
    await mkdir(path.dirname(output), { recursive: true });
    await page.screenshot({ path: output, fullPage: true });
    await testInfo.attach(`extension-${name}`, { path: output, contentType: 'image/png' });
  });
}
