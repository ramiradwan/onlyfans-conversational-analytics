import { createSurfaceClient, openSurface, send } from './ui/surface-client.mjs';
import { customerJourney, desktopOwnsCapture, isPreview, phaseLabel, statusPresentation } from './ui/presentation.mjs';
import { element, show, text, renderLoading, renderJourney, renderReadiness, renderLegalLinks, createPageActions } from './ui/dom.mjs';
import { transition, openCreatorAccount } from './ui/actions.mjs';

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
  // While the desktop app can control this browser, it owns pause and resume.
  const desktopOwned = desktopOwnsCapture(model);
  show('pause', ['preview', 'full'].includes(status?.consent.mode) && !desktopOwned);
  // Explain the missing Pause where it would have been.
  show('desktop-control-note', desktopOwned && status?.consent.mode === 'full');
  show('open-connection', status?.consent.mode === 'full' || status?.consent.resume_mode === 'full');
  renderLegalLinks(legal);
  const showReadiness = status?.consent.mode === 'full' && model.pairing.state === 'paired';
  show('ready-details', showReadiness);
  renderReadiness(model);
  text('desktop-status', model.desktopRuntimeReachable || desktopOwned ? 'Connected' : 'Not connected');
  if (!status) { text('mode-label', statusPresentation(model).label); return; }
  document.querySelector('main').dataset.ready = 'true';
  text('mode-label', phaseLabel(status));
  for (const [id, key] of [['messages-count', 'message_observations'], ['chats-count', 'chat_observations'],
    ['inbound-count', 'inbound_observations'], ['outbound-count', 'outbound_observations']]) {
    text(id, (status.preview?.[key] ?? 0) > 9_999_999 ? '10M+' : new Intl.NumberFormat().format(status.preview?.[key] ?? 0));
  }
  const journey = customerJourney(model);
  renderJourney(journey, model);
  primaryAction = 'setup'; setupSection = '';
  let label = 'Continue setup';
  if (!legal?.configured || legal.requires_reauthorization) {
    label = legal?.requires_reauthorization ? 'Review changes' : 'Continue setup';
  } else if (status.phase === 'permission_required') {
    text('journey-title', 'Site access needs approval');
    text('journey-body', 'Continue in setup to allow access.');
  } else if (status.consent.mode === 'paused' && desktopOwned) {
    primaryAction = 'desktop'; label = 'Resume in the desktop app';
  } else if (status.consent.mode === 'paused') {
    primaryAction = 'resume'; label = 'Resume analytics';
  } else if (status.consent.mode === 'preview') {
    label = 'Review Full analytics'; setupSection = 'full';
  } else if (journey.primaryAction === 'open_creator_account') {
    primaryAction = 'creator'; label = journey.primaryLabel;
  } else if (journey.id === 'full_ready') {
    primaryAction = 'dashboard'; label = 'Open analysis';
    text('journey-body', 'Insights and stored messages are in the desktop app.');
  } else if (journey.id === 'pairing_in_progress') {
    text('journey-title', 'Connection in progress');
    text('journey-body', 'Continue in the setup tab. You can close this popup.');
  }
  element('journey-card').classList.toggle('popup-action-only', status.consent.mode === 'preview'
    && status.phase !== 'permission_required' && !legal?.requires_reauthorization);
  text('journey-primary', label); show('journey-primary', true);
  const summary = statusPresentation(model);
  text('journey-badge', summary.label);
  text('journey-title', summary.label);
  text('journey-body', summary.body);
}
page.bind('journey-primary', () => {
  if (primaryAction === 'resume') return transition('resume', client.model);
  if (primaryAction === 'dashboard') return chrome.tabs.create({ url: client.model.config.dashboard_url });
  if (primaryAction === 'desktop') return chrome.tabs.create({ url: client.model.config.history_settings_url });
  if (primaryAction === 'creator') return openCreatorAccount();
  return openSurface('setup', setupSection);
});
page.bind('pause', () => transition('pause', client.model));
page.bind('continue-setup', () => openSurface('setup'));
page.bind('open-manage', () => openSurface('options'));
page.bind('open-connection', () => openSurface('options', 'connection'));
page.bind('clear-preview', () => openSurface('options', 'data'));
page.bind('retry-runtime', () => client.sync());
void client.start();
