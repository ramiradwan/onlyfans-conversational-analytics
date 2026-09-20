import { UI_RELOAD_TABS_MESSAGE_TYPE } from './runtime/consent-controller.mjs';
import { createSurfaceClient, openSurface, send } from './ui/surface-client.mjs';
import { customerJourney, isPreview, phaseLabel } from './ui/presentation.mjs';
import { element, show, text, renderLoading, renderJourney, renderReadiness, renderLegalLinks, createPageActions } from './ui/dom.mjs';
import { transition } from './ui/actions.mjs';

let failed = false;
let primaryAction = 'setup';
let setupSection = '';
const client = createSurfaceClient((model) => { if (model.status) failed = false; render(model); }, () => { failed = true; render(client.model); });
const page = createPageActions(client, render);

function render(model) {
  const { status, legal } = model;
  renderLoading(status, failed);
  show('journey-card', status !== null);
  show('preview-metrics', isPreview(status));
  show('preview-limit', isPreview(status) && status?.preview?.limited === true);
  show('pause', ['preview', 'full'].includes(status?.consent.mode));
  show('open-connection', status?.consent.mode === 'full' || status?.consent.resume_mode === 'full');
  renderLegalLinks(legal);
  const showReadiness = status?.consent.mode === 'full' && model.pairing.state === 'paired';
  show('ready-details', showReadiness);
  renderReadiness(model);
  text('desktop-status', model.desktopRuntimeReachable ? 'Running' : 'Not running');
  if (!status) { text('mode-label', 'Checking status…'); return; }
  document.querySelector('main').dataset.ready = 'true';
  text('mode-label', phaseLabel(status));
  for (const [id, key] of [['messages-count', 'message_observations'], ['chats-count', 'chat_observations'],
    ['inbound-count', 'inbound_observations'], ['outbound-count', 'outbound_observations']]) {
    text(id, new Intl.NumberFormat().format(status.preview?.[key] ?? 0));
  }
  const journey = customerJourney(model);
  renderJourney(journey);
  primaryAction = 'setup'; setupSection = '';
  let label = 'Continue setup';
  if (!legal?.configured || legal.requires_reauthorization) {
    label = legal?.requires_reauthorization ? 'Review changes' : 'Continue setup';
  } else if (status.phase === 'permission_required') {
    text('journey-title', 'Site access needs approval');
    text('journey-body', 'Continue in setup to allow access.');
  } else if (status.reload_required) {
    primaryAction = 'reload'; label = 'Reload OnlyFans tabs';
    text('journey-body', 'Reload your open OnlyFans tabs to apply site access.');
  } else if (status.consent.mode === 'paused') {
    primaryAction = 'resume'; label = 'Resume analytics';
  } else if (status.consent.mode === 'preview') {
    label = 'Review Full analytics'; setupSection = 'full';
  } else if (journey.id === 'full_ready') {
    primaryAction = 'dashboard'; label = 'Open analysis';
    text('journey-body', 'Insights and stored messages are in the desktop app.');
  } else if (journey.id === 'pairing_in_progress') {
    text('journey-title', 'Connection in progress');
    text('journey-body', 'Continue in the setup tab. You can close this popup.');
  }
  element('journey-card').classList.toggle('popup-action-only', status.consent.mode === 'preview'
    && !status.reload_required && status.phase !== 'permission_required' && !legal?.requires_reauthorization);
  text('journey-primary', label); show('journey-primary', true);
}
page.bind('journey-primary', () => {
  if (primaryAction === 'resume') return transition('resume', client.model);
  if (primaryAction === 'reload') return send({ type: UI_RELOAD_TABS_MESSAGE_TYPE });
  if (primaryAction === 'dashboard') return chrome.tabs.create({ url: client.model.config.dashboard_url });
  return openSurface('setup', setupSection);
});
page.bind('pause', () => transition('pause', client.model));
page.bind('open-manage', () => openSurface('options'));
page.bind('open-connection', () => openSurface('options', 'connection'));
page.bind('clear-preview', () => openSurface('options', 'data'));
page.bind('retry-runtime', () => client.sync());
void client.start();
