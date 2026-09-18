import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const POPUP_STATES = Object.freeze({
  analytics_off: Object.freeze({
    tone: 'info', mode: 'Analytics off', badge: '', title: 'Start with Preview',
    body: 'See how many messages you send and receive each day.',
    primary: 'Set up Preview',
  }),
  preview: Object.freeze({
    tone: 'info', mode: 'Preview on', badge: '', title: 'Preview is ready',
    body: 'Add Full analytics for insights from your conversations.',
    primary: 'Review Full analytics', preview: true,
  }),
  paused: Object.freeze({
    tone: 'info', mode: 'Analytics paused', badge: '', title: 'Analytics paused',
    body: 'No new activity is collected.',
    primary: 'Resume analytics', preview: true,
  }),
  desktop_needed: Object.freeze({
    tone: 'warning', mode: 'Full setup in progress', badge: 'Setup', title: 'Desktop app needed for Full analytics',
    body: 'Full analytics runs in the desktop app on this computer. Start it, then check again.',
    primary: 'Check again',
    desktop: 'Not connected', delivery: 'Off', activation: 'Not checked', analysis: 'Not ready',
  }),
  setup_incomplete: Object.freeze({
    tone: 'warning', mode: 'Full setup in progress', badge: 'Setup', title: 'Sign in to your creator account',
    body: 'Use OnlyFans in this browser, then return here.',
    primary: 'Open creator account', secondary: 'Open desktop app', desktop: 'Running', delivery: 'Off',
    activation: 'Not checked', analysis: 'Not ready',
  }),
  pairing_required: Object.freeze({
    tone: 'info', mode: 'Full setup in progress', badge: 'Setup', title: 'Connect to the desktop app',
    body: 'Connect to view insights from your conversations.',
    secondary: 'Open desktop app', pairing: 'pair', desktop: 'Running', delivery: 'Off',
    activation: 'Not checked', analysis: 'Not ready',
  }),
  pairing_compare: Object.freeze({
    tone: 'progress', mode: 'Full setup in progress', badge: 'Connecting', title: 'Confirm the connection',
    body: 'Check that the desktop app shows the same code, then confirm there.',
    pairing: 'compare', pairingCode: '483 217', desktop: 'Running', delivery: 'Off',
    activation: 'Not checked', analysis: 'Not ready',
  }),
  pairing_not_ready: Object.freeze({
    tone: 'warning', mode: 'Full setup in progress', badge: 'Setup', title: 'Continue in the desktop app',
    body: 'In the desktop app, open Settings and choose Connect extension.',
    primary: 'Open desktop app settings', secondary: 'Pair device', desktop: 'Running', delivery: 'Off',
    activation: 'Not checked', analysis: 'Not ready',
  }),
  pairing_failed: Object.freeze({
    tone: 'error', mode: 'Full setup in progress', badge: 'Needs attention', title: 'Connection was not completed',
    body: 'In the desktop app, choose Connect extension and try again.',
    primary: 'Try connection again', desktop: 'Running', delivery: 'Off',
    activation: 'Not checked', analysis: 'Not ready',
  }),
  pairing_window_not_ready: Object.freeze({
    view: 'pairing', tone: 'warning', badge: 'Setup', title: 'Continue in the desktop app',
    body: 'In the desktop app, open Settings and choose Connect extension.',
    primary: 'Open desktop app settings', secondary: 'Pair device',
  }),
  pairing_window_compare: Object.freeze({
    view: 'pairing', tone: 'progress', badge: 'Connecting', title: 'Confirm the connection',
    body: 'Check that the desktop app shows the same code, then confirm there.',
    pairing: 'compare', pairingCode: '483 217',
  }),
  pairing_window_connected: Object.freeze({
    view: 'pairing', tone: 'success', badge: 'Connected', title: 'Connected to the desktop app',
    body: 'This window closes automatically.',
  }),
  desktop_stopped: Object.freeze({
    tone: 'warning', mode: 'Full setup in progress', badge: 'Needs attention', title: 'Desktop app is not running',
    body: 'Start the desktop app, then try again. Your connection is saved.',
    primary: 'Retry connection', desktop: 'Not running', delivery: 'Off',
    activation: 'Not checked', analysis: 'Not ready',
  }),
  activation_checking: Object.freeze({
    tone: 'progress', mode: 'Desktop connected', badge: 'Checking', title: 'Checking activation',
    body: '',
    desktop: 'Running', delivery: 'Connected', activation: 'Checking…', analysis: 'Not ready',
  }),
  activation_required: Object.freeze({
    tone: 'warning', mode: 'Desktop connected', badge: 'Setup', title: 'Finish activating Full analytics',
    body: 'Continue in Settings in the desktop app.',
    primary: 'Open desktop app', secondary: 'Check activation', desktop: 'Running', delivery: 'Connected',
    activation: 'Required', analysis: 'Waiting for activation',
  }),
  activation_active_analysis_blocked: Object.freeze({
    tone: 'warning', mode: 'Desktop connected', badge: 'Needs attention', title: 'Analysis is not available right now',
    body: 'Full analytics is activated. Your saved data is unchanged.',
    primary: 'Check again', secondary: 'Open desktop app', desktop: 'Running', delivery: 'Connected',
    activation: 'Active', analysis: 'Not ready',
  }),
  activation_unavailable: Object.freeze({
    tone: 'error', mode: 'Desktop connected', badge: 'Needs attention', title: 'Couldn\'t check activation',
    body: 'Your saved data is unchanged.',
    primary: 'Check again', secondary: 'Open desktop app', desktop: 'Running', delivery: 'Connected',
    activation: 'Needs attention', analysis: 'Unavailable',
  }),
  full_ready: Object.freeze({
    tone: 'success', mode: 'Desktop connected', badge: 'Ready', title: 'Your analysis is ready',
    body: '',
    primary: 'Open analysis', desktop: 'Running', delivery: 'Connected',
    activation: 'Active', analysis: 'Ready',
  }),
});

export async function popupDocument() {
  const [html, css] = await Promise.all([
    readFile(path.join(ROOT, 'popup.html'), 'utf8'),
    readFile(path.join(ROOT, 'popup.css'), 'utf8'),
  ]);
  return html
    .replace('<link rel="stylesheet" href="popup.css">', `<style>${css}</style>`)
    .replace('<script src="popup.js"></script>', '');
}

export function applyPopupState(value, doc = document) {
    const byId = (id) => doc.getElementById(id);
    if (value.view) doc.querySelector('main').dataset.view = value.view;
    byId('mode-label').textContent = value.mode ?? 'Full setup in progress';
    byId('journey-card').dataset.tone = value.tone;
    byId('journey-badge').textContent = value.badge;
    byId('journey-title').textContent = value.title;
    byId('journey-body').textContent = value.body;
    byId('preview-metrics').classList.toggle('hidden', value.preview !== true);
    byId('open-connection').classList.toggle('hidden', value.desktop === undefined);
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
}

export async function renderPopupState(page, state) {
  await page.setViewportSize(state.view === 'pairing' ? { width: 400, height: 488 } : { width: 390, height: 600 });
  await page.setContent(await popupDocument());
  await page.evaluate(applyPopupState, state);
}
