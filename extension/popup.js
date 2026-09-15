import {
  ONLYFANS_ORIGIN_PATTERN,
  UI_CLEAR_PREVIEW_MESSAGE_TYPE,
  UI_DELETE_LOCAL_DATA_MESSAGE_TYPE,
  UI_STATUS_MESSAGE_TYPE,
  UI_RELOAD_TABS_MESSAGE_TYPE,
  UI_TRANSITION_MESSAGE_TYPE,
} from './runtime/consent-controller.mjs';
import {
  LEGAL_ACTIVATION_STATUS_MESSAGE_TYPE,
  LEGAL_ACCEPT_TERMS_MESSAGE_TYPE,
  LEGAL_ACKNOWLEDGE_RISK_MESSAGE_TYPE,
  LEGAL_ACTIVATE_SOFTWARE_MESSAGE_TYPE,
  LEGAL_CHOOSE_MODE_MESSAGE_TYPE,
} from './runtime/legal-activation-controller.mjs';
import { requiredOriginsForMode } from './runtime/permission-recovery.mjs';
import { deriveCustomerJourney, probeDesktopRuntime } from './runtime/customer-journey.mjs';
import { LOCAL_SERVICE_ORIGIN, assertLocalServiceUrl } from './transport/local-service-endpoints.mjs';

const ids = [
  'mode-label', 'messages-count', 'chats-count', 'inbound-count', 'outbound-count',
  'brain-status', 'delivery-status', 'pending-count', 'capture-health', 'history-health',
  'feedback', 'legal-unavailable', 'pre-mode', 'terms-accepted', 'risk-acknowledged',
  'terms-link', 'risk-link', 'activate-software', 'mode-choice', 'preview-disclosure',
  'full-disclosure', 'enable-preview', 'enable-full', 'not-now-preview', 'full-secondary',
  'restore-access', 'reload-tabs', 'review-full', 'resume', 'pause', 'history', 'open-dashboard', 'revoke',
  'clear-preview', 'delete-local-data', 'privacy-link',
  'companion-pairing', 'pairing-status', 'pairing-code', 'pair-companion', 'cancel-pairing', 'forget-companion',
  'journey-card', 'journey-badge', 'journey-title', 'journey-body', 'journey-primary', 'journey-secondary',
];
const elements = Object.fromEntries(ids.map((id) => [id, document.getElementById(id)]));

let companionConfig = {
  dashboard_url: `${LOCAL_SERVICE_ORIGIN}/`,
  history_settings_url: `${LOCAL_SERVICE_ORIGIN}/settings`,
  privacy_policy_url: '',
  desktop_app_download_url: '',
};
let currentStatus = null;
let legalStatus = null;
let fullReviewRequested = false;
let initialModeChoiceDismissed = false;
let busy = false;
let desktopRuntimeReachable = false;
let pairingStatus = { state: 'unpaired', comparison_code: null };
const isPairingWindow = window.location.hash === '#pairing';
const pairingPort = chrome.runtime.connect({ name: 'ofca.companion.pairing' });

function show(element, visible) {
  element.classList.toggle('hidden', !visible);
}

function setLocked(element, locked) {
  element.dataset.locked = locked ? 'true' : 'false';
  element.disabled = busy || locked;
}

function setBusy(value) {
  busy = value;
  document.querySelectorAll('button,input').forEach((control) => {
    control.disabled = busy || control.dataset.locked === 'true';
  });
}

function secureExternalUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username !== '' || url.password !== '' || url.hostname.endsWith('.invalid')) return null;
    return url.href;
  } catch {
    return null;
  }
}

function phaseLabel(status) {
  return ({
    off: 'Analytics off — no OnlyFans access',
    preview: 'Activity preview enabled',
    identity: 'Full setup in progress',
    full: status.delivery?.transport_state === 'authenticated' ? 'Full mode ready' : 'Full mode connecting',
    paused: 'Analytics paused',
    revoked: 'Site access revoked',
    permission_required: 'Site access needs approval',
    transitioning: 'Applying your choice…',
    unavailable: 'Analytics temporarily unavailable',
  })[status.phase] ?? 'Analytics inactive';
}

function journeyBadge(state) {
  return ({
    preview_available: 'Preview',
    desktop_app_needed: 'Next step',
    desktop_app_unavailable: 'Needs attention',
    setup_incomplete: 'Setup needed',
    pairing_required: 'Next step',
    pairing_in_progress: 'Connecting',
    pairing_failed: 'Try again',
    full_ready: 'Ready',
    full_unavailable: 'Needs attention',
  })[state] ?? 'Status';
}

function renderJourney() {
  if (currentStatus === null) return;
  const journey = deriveCustomerJourney({
    status: currentStatus,
    pairing: pairingStatus,
    desktopRuntimeReachable,
    desktopDownloadAvailable: secureExternalUrl(companionConfig.desktop_app_download_url) !== null,
  });
  elements['journey-card'].dataset.tone = journey.tone;
  elements['journey-badge'].textContent = journeyBadge(journey.id);
  elements['journey-title'].textContent = journey.title;
  elements['journey-body'].textContent = journey.body;
  elements['journey-primary'].dataset.action = journey.primaryAction ?? '';
  elements['journey-primary'].textContent = journey.primaryLabel ?? '';
  show(elements['journey-primary'], journey.primaryAction !== null);
  elements['journey-secondary'].dataset.action = journey.secondaryAction ?? '';
  elements['journey-secondary'].textContent = journey.secondaryLabel ?? '';
  show(elements['journey-secondary'], journey.secondaryAction !== null);
}

function renderPairing(value = pairingStatus) {
  pairingStatus = value;
  const fullSelected = currentStatus?.consent?.mode === 'full';
  const pending = ['pairing', 'compare'].includes(value.state);
  const paired = value.state === 'paired';
  const failed = value.state === 'pairing_failed';
  const pairingActionable = desktopRuntimeReachable
    && !pending
    && !paired
    && !['setup_incomplete', 'unavailable'].includes(value.state);
  show(elements['companion-pairing'], fullSelected && (desktopRuntimeReachable || paired || pending || failed));
  show(elements['pair-companion'], pairingActionable);
  show(elements['cancel-pairing'], pending);
  show(elements['forget-companion'], paired);
  const code = typeof value.comparison_code === 'string' && /^\d{6}$/u.test(value.comparison_code) ? value.comparison_code : null;
  show(elements['pairing-code'], code !== null);
  elements['pairing-code'].textContent = code === null ? '' : `${code.slice(0, 3)} ${code.slice(3)}`;
  elements['pairing-status'].textContent = ({
    paired: 'This extension is paired with the desktop app.',
    pairing: 'Connecting… Keep this window open.',
    compare: 'Compare this code with the desktop app. Confirm there only if both codes match.',
    pairing_failed: 'Connection did not complete. Open a new connection window in the desktop app and try again.',
    setup_incomplete: 'Open your creator account in OnlyFans, then return here to continue.',
    unavailable: 'Full analysis is not enabled yet.',
  })[value.state] ?? 'Open a connection window in the desktop app, then pair this device.';
  renderJourney();
}

pairingPort.onMessage.addListener((value) => renderPairing(value));
pairingPort.onDisconnect.addListener(() => renderPairing({ state: 'pairing_failed', comparison_code: null }));

async function send(message) {
  const response = await chrome.runtime.sendMessage(message);
  if (response?.ok !== true) throw new Error(response?.code ?? 'request_failed');
  return response.status;
}

async function sendLegal(message) {
  const response = await chrome.runtime.sendMessage(message);
  if (response?.ok !== true) throw new Error(response?.code ?? 'legal_activation_failed');
  return response.result;
}

async function requestAnalyticsAccess(mode) {
  const origins = requiredOriginsForMode(mode);
  if (origins.length === 0) throw new Error('There is no active analytics mode to authorize.');
  const granted = await chrome.permissions.request({ origins });
  if (!granted) throw new Error('Required site access was not granted. Nothing was enabled.');
}

function bindLink(element, path, binding) {
  if (binding === null) {
    element.href = '#';
    return;
  }
  element.href = new URL(path, binding.public_origin).href;
  element.target = '_blank';
  element.rel = 'noreferrer';
}

function renderLegal(status) {
  legalStatus = status;
  show(elements['legal-unavailable'], !status.configured);
  const flow = status.flow;
  const mode = currentStatus?.consent?.mode ?? status.consent_mode;
  const active = ['preview', 'full'].includes(mode);
  const normalPaused = mode === 'paused' && !status.requires_reauthorization;
  const needsPreMode = !active
    && !normalPaused
    && (flow.terms_event_id === null || flow.risk_event_id === null || flow.stage === 'pre_mode');

  show(elements['pre-mode'], status.configured && needsPreMode);
  elements['terms-accepted'].checked = flow.terms_event_id !== null;
  elements['risk-acknowledged'].checked = flow.risk_event_id !== null;
  setLocked(elements['terms-accepted'], flow.terms_event_id !== null);
  setLocked(elements['risk-acknowledged'], flow.risk_event_id !== null);
  setLocked(elements['activate-software'], flow.terms_event_id === null || flow.risk_event_id === null);

  const binding = status.bindings ?? null;
  if (binding !== null) {
    bindLink(elements['terms-link'], binding.instruments.terms_of_service.public_url, binding);
    bindLink(elements['risk-link'], binding.instruments.risk_disclosure.public_url, binding);
    document.querySelectorAll('.extension-privacy-link').forEach((link) => {
      bindLink(link, binding.instruments.extension_privacy_notice.public_url, binding);
    });
  }

  const chooseInitial = status.configured
    && flow.stage === 'mode_selection'
    && !active
    && !normalPaused
    && !initialModeChoiceDismissed;
  const chooseUpgrade = status.configured && mode === 'preview' && fullReviewRequested;
  show(elements['mode-choice'], chooseInitial || chooseUpgrade);
  show(elements['preview-disclosure'], chooseInitial);
  show(elements['full-disclosure'], chooseInitial || chooseUpgrade);
  elements['full-secondary'].textContent = mode === 'preview' ? 'Keep Preview' : 'Not now';
  show(elements['review-full'], mode === 'preview' && !fullReviewRequested);
  show(elements.resume, normalPaused);
  if (status.requires_reauthorization === true) {
    elements.feedback.textContent = 'Data-handling information changed. Review it before restarting analytics.';
  }
  renderJourney();
}
function render(status) {
  currentStatus = status;
  elements['mode-label'].textContent = phaseLabel(status);
  elements['messages-count'].textContent = String(status.preview.message_observations);
  elements['chats-count'].textContent = String(status.preview.chat_observations);
  elements['inbound-count'].textContent = String(status.preview.inbound_observations);
  elements['outbound-count'].textContent = String(status.preview.outbound_observations);
  elements['brain-status'].textContent = desktopRuntimeReachable
    ? 'Running'
    : pairingStatus.state === 'paired' ? 'Not running' : 'Not connected';
  elements['delivery-status'].textContent = status.delivery.transport_state === 'authenticated'
    ? 'Connected'
    : status.delivery.runtime_ready ? 'Connecting' : 'Inactive';
  elements['pending-count'].textContent = String(status.delivery.pending_entries);
  const dropCount = Object.values(status.delivery.capture_drop_counts ?? {})
    .reduce((total, value) => total + (Number.isSafeInteger(value) ? value : 0), 0);
  elements['capture-health'].textContent = status.delivery.startup_error_code === 'startup_failed'
    ? 'Full analysis could not start. Start the desktop app and retry.'
    : dropCount > 0 ? `${dropCount} capture observation${dropCount === 1 ? '' : 's'} dropped.` : '';
  elements['history-health'].textContent = !status.delivery.history_error_code
    ? ''
    : 'History sync needs attention in the desktop app.';
  const mode = status.consent.mode;
  const active = ['preview', 'full'].includes(mode);
  const permissionRequired = status.phase === 'permission_required';
  show(elements['restore-access'], permissionRequired && active);
  show(elements['reload-tabs'], status.reload_required === true);
  show(elements.pause, active && !permissionRequired);
  show(elements.history, status.phase === 'full');
  show(elements['open-dashboard'], desktopRuntimeReachable);
  show(elements.revoke, mode !== 'off' && mode !== 'revoked');
  renderPairing();
  if (legalStatus !== null) renderLegal(legalStatus);
}

async function probeDesktop() {
  if (currentStatus?.consent?.mode !== 'full') {
    desktopRuntimeReachable = false;
    renderJourney();
    return false;
  }
  desktopRuntimeReachable = await probeDesktopRuntime();
  if (currentStatus !== null) render(currentStatus);
  return desktopRuntimeReachable;
}

async function refresh() {
  const [status, legal] = await Promise.all([
    send({ type: UI_STATUS_MESSAGE_TYPE }),
    sendLegal({ type: LEGAL_ACTIVATION_STATUS_MESSAGE_TYPE }),
  ]);
  render(status);
  renderLegal(legal);
  await probeDesktop();
}

async function legalAction(type, checkbox = null) {
  setBusy(true);
  elements.feedback.textContent = '';
  try {
    const result = await sendLegal({ type });
    initialModeChoiceDismissed = false;
    renderLegal(result);
  } catch (error) {
    if (checkbox !== null) checkbox.checked = false;
    elements.feedback.textContent = error.message;
  } finally {
    setBusy(false);
  }
}

async function chooseMode(mode) {
  setBusy(true);
  elements.feedback.textContent = '';
  try {
    await requestAnalyticsAccess(mode);
    const result = await sendLegal({ type: LEGAL_CHOOSE_MODE_MESSAGE_TYPE, mode });
    render(result.status);
    legalStatus = await sendLegal({ type: LEGAL_ACTIVATION_STATUS_MESSAGE_TYPE });
    fullReviewRequested = false;
    initialModeChoiceDismissed = false;
    renderLegal(legalStatus);
    if (mode === 'full' && result.status.phase === 'identity') {
      await probeDesktop();
      elements.feedback.textContent = 'Full setup started. Follow the next step above.';
      elements['journey-card'].scrollIntoView({ block: 'nearest' });
    }
  } catch (error) {
    elements.feedback.textContent = error.message ?? 'The change could not be applied.';
  } finally {
    setBusy(false);
  }
}

async function transition(mode) {
  setBusy(true);
  elements.feedback.textContent = '';
  try {
    if (mode === 'resume') await requestAnalyticsAccess(currentStatus?.consent?.resume_mode);
    const status = await send({ type: UI_TRANSITION_MESSAGE_TYPE, mode });
    render(status);
    await probeDesktop();
  } catch (error) {
    elements.feedback.textContent = error.message ?? 'The change could not be applied.';
  } finally {
    setBusy(false);
  }
}

function openPairingWindow() {
  if (isPairingWindow) {
    pairingPort.postMessage({ type: 'pair' });
    return;
  }
  void chrome.windows.create({
    url: chrome.runtime.getURL('popup.html#pairing'),
    type: 'popup',
    width: 440,
    height: 640,
  }).catch(() => { elements.feedback.textContent = 'The connection window could not be opened.'; });
}

async function runJourneyAction(action) {
  if (!action) return;
  if (action === 'review_full') {
    fullReviewRequested = true;
    renderLegal(legalStatus);
    elements['mode-choice'].scrollIntoView({ block: 'nearest' });
    return;
  }
  if (action === 'install_desktop') {
    const download = secureExternalUrl(companionConfig.desktop_app_download_url);
    if (download === null) {
      elements.feedback.textContent = 'The desktop app download is not configured in this build.';
      return;
    }
    await chrome.tabs.create({ url: download });
    return;
  }
  if (action === 'open_creator_account') {
    await chrome.tabs.create({ url: 'https://onlyfans.com/' });
    return;
  }
  if (action === 'pair') {
    openPairingWindow();
    return;
  }
  if (action === 'cancel_pairing') {
    pairingPort.postMessage({ type: 'cancel' });
    return;
  }
  if (action === 'open_dashboard') {
    await chrome.tabs.create({ url: companionConfig.dashboard_url });
    return;
  }
  if (action === 'retry_full' && currentStatus?.consent?.mode === 'full') {
    await transition(currentStatus.consent.mode);
  }
}

elements['journey-primary'].addEventListener('click', () => { void runJourneyAction(elements['journey-primary'].dataset.action); });
elements['journey-secondary'].addEventListener('click', () => { void runJourneyAction(elements['journey-secondary'].dataset.action); });
elements['terms-accepted'].addEventListener('change', () => {
  if (elements['terms-accepted'].checked) void legalAction(LEGAL_ACCEPT_TERMS_MESSAGE_TYPE, elements['terms-accepted']);
});
elements['risk-acknowledged'].addEventListener('change', () => {
  if (elements['risk-acknowledged'].checked) void legalAction(LEGAL_ACKNOWLEDGE_RISK_MESSAGE_TYPE, elements['risk-acknowledged']);
});
elements['activate-software'].addEventListener('click', () => { void legalAction(LEGAL_ACTIVATE_SOFTWARE_MESSAGE_TYPE); });
elements['enable-preview'].addEventListener('click', () => { void chooseMode('preview'); });
elements['enable-full'].addEventListener('click', () => { void chooseMode('full'); });
elements['restore-access'].addEventListener('click', async () => {
  const mode = currentStatus?.consent?.mode;
  if (!['preview', 'full'].includes(mode)) return;
  setBusy(true);
  elements.feedback.textContent = '';
  try {
    await requestAnalyticsAccess(mode);
    render(await send({ type: UI_TRANSITION_MESSAGE_TYPE, mode }));
    elements.feedback.textContent = 'Required site access was restored.';
  } catch (error) {
    elements.feedback.textContent = error.message ?? 'Site access could not be restored.';
  } finally {
    setBusy(false);
  }
});
elements['reload-tabs'].addEventListener('click', async () => {
  setBusy(true);
  try {
    render(await send({ type: UI_RELOAD_TABS_MESSAGE_TYPE }));
  } catch (_error) {
    elements.feedback.textContent = 'OnlyFans tabs could not be reloaded.';
  } finally {
    setBusy(false);
  }
});
elements['review-full'].addEventListener('click', () => {
  fullReviewRequested = true;
  renderLegal(legalStatus);
});
elements['not-now-preview'].addEventListener('click', () => {
  initialModeChoiceDismissed = true;
  renderLegal(legalStatus);
});
elements['full-secondary'].addEventListener('click', () => {
  if ((currentStatus?.consent?.mode ?? null) === 'preview') fullReviewRequested = false;
  else initialModeChoiceDismissed = true;
  renderLegal(legalStatus);
});
elements.resume.addEventListener('click', () => { void transition('resume'); });
elements.pause.addEventListener('click', () => { void transition('pause'); });
elements.revoke.addEventListener('click', () => {
  if (window.confirm('Revoke site access and stop all new observations? Existing desktop-app data is retained.')) {
    void transition('revoked');
  }
});
elements['clear-preview'].addEventListener('click', async () => {
  setBusy(true);
  try {
    render(await send({ type: UI_CLEAR_PREVIEW_MESSAGE_TYPE }));
    elements.feedback.textContent = 'Seven-day preview counts cleared.';
  } catch (_error) {
    elements.feedback.textContent = 'Preview counts could not be cleared.';
  } finally {
    setBusy(false);
  }
});
elements['delete-local-data'].addEventListener('click', async () => {
  if (!window.confirm(
    'Delete all data stored by this extension, including activation evidence, disconnect the desktop app, revoke site access, and stop analytics?',
  )) return;
  setBusy(true);
  try {
    render(await send({ type: UI_DELETE_LOCAL_DATA_MESSAGE_TYPE }));
    legalStatus = await sendLegal({ type: LEGAL_ACTIVATION_STATUS_MESSAGE_TYPE });
    renderLegal(legalStatus);
    elements.feedback.textContent = 'All local extension data was deleted.';
  } catch (_error) {
    elements.feedback.textContent = 'Local extension data could not be fully deleted.';
  } finally {
    setBusy(false);
  }
});
elements['open-dashboard'].addEventListener('click', () => {
  void chrome.tabs.create({ url: companionConfig.dashboard_url });
});
elements['pair-companion'].addEventListener('click', openPairingWindow);
elements['cancel-pairing'].addEventListener('click', () => pairingPort.postMessage({ type: 'cancel' }));
elements['forget-companion'].addEventListener('click', () => {
  if (window.confirm('Forget the desktop app and stop this connection? You will need to pair again to use Full analysis.')) {
    pairingPort.postMessage({ type: 'forget' });
  }
});
elements.history.addEventListener('click', async () => {
  setBusy(true);
  try {
    const granted = await chrome.permissions.request({
      permissions: ['webRequest'],
      origins: [ONLYFANS_ORIGIN_PATTERN],
    });
    if (!granted) throw new Error('History access was not granted. Live analytics is unchanged.');
    await chrome.tabs.create({ url: companionConfig.history_settings_url });
  } catch (error) {
    elements.feedback.textContent = error.message;
  } finally {
    setBusy(false);
  }
});

async function loadCompanionConfig() {
  const response = await fetch(chrome.runtime.getURL('extension-config.json'));
  const candidate = await response.json();
  if (candidate?.schema !== 'ofca-extension-config/v1') return;
  companionConfig = {
    ...companionConfig,
    privacy_policy_url: candidate.privacy_policy_url,
    dashboard_url: assertLocalServiceUrl(candidate.dashboard_url).href,
    history_settings_url: assertLocalServiceUrl(candidate.history_settings_url).href,
    desktop_app_download_url: secureExternalUrl(candidate.desktop_app_download_url) ?? '',
  };
  const privacyUrl = secureExternalUrl(companionConfig.privacy_policy_url);
  if (privacyUrl !== null) {
    elements['privacy-link'].href = privacyUrl;
    elements['privacy-link'].textContent = 'Privacy policy';
    elements['privacy-link'].removeAttribute('aria-disabled');
    elements['privacy-link'].target = '_blank';
    elements['privacy-link'].rel = 'noreferrer';
  }
}

elements['privacy-link'].addEventListener('click', (event) => {
  if (elements['privacy-link'].getAttribute('aria-disabled') === 'true') event.preventDefault();
});

async function initialize() {
  setBusy(true);
  try {
    await loadCompanionConfig();
    await refresh();
    if (isPairingWindow && currentStatus?.consent?.mode === 'full') {
      pairingPort.postMessage({ type: 'pair' });
      elements['companion-pairing'].scrollIntoView({ block: 'center' });
    }
  } catch (_error) {
    elements.feedback.textContent = 'Extension status is temporarily unavailable. Close this popup and try again.';
  } finally {
    setBusy(false);
  }
}

void initialize();