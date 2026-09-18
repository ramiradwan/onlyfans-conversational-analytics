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
import { customerReleaseConfig } from './runtime/customer-release-config.mjs';
import { requiredOriginsForMode } from './runtime/permission-recovery.mjs';
import { deriveCustomerJourney, probeDesktopRuntime } from './runtime/customer-journey.mjs';
import { loadPopupContext, savePopupContext } from './runtime/popup-context.mjs';
import { LOCAL_SERVICE_ORIGIN, assertLocalServiceUrl } from './transport/local-service-endpoints.mjs';

const ids = [
  'mode-label', 'messages-count', 'chats-count', 'inbound-count', 'outbound-count',
  'brain-status', 'delivery-status', 'activation-status', 'analysis-status', 'pending-count',
  'capture-health', 'history-health',
  'feedback', 'legal-unavailable', 'pre-mode', 'terms-accepted', 'risk-acknowledged',
  'terms-link', 'risk-link', 'activate-software', 'mode-choice', 'preview-disclosure',
  'full-disclosure', 'enable-preview', 'enable-full', 'not-now-preview', 'full-secondary',
  'restore-access', 'reload-tabs', 'pause', 'history', 'history-prompt', 'open-dashboard', 'revoke',
  'clear-preview', 'delete-local-data', 'privacy-link', 'preview-metrics',
  'open-connection', 'open-manage', 'connection-back', 'manage-back', 'connection-title', 'manage-title',
  'companion-pairing', 'pairing-status', 'pairing-code', 'pair-companion', 'cancel-pairing', 'forget-companion',
  'journey-card', 'journey-badge', 'journey-title', 'journey-body', 'journey-primary', 'journey-secondary',
];
const elements = Object.fromEntries(ids.map((id) => [id, document.getElementById(id)]));
const main = document.querySelector('main');

let companionConfig = {
  dashboard_url: `${LOCAL_SERVICE_ORIGIN}/`,
  history_settings_url: `${LOCAL_SERVICE_ORIGIN}/settings`,
  privacy_policy_url: '',
  desktop_app_download_url: customerReleaseConfig.desktop_app_download_url,
};
let currentStatus = null;
let legalStatus = null;
let fullReviewRequested = false;
let initialModeChoiceDismissed = false;
let busy = false;
let desktopRuntimeReachable = false;
let readinessRequestInFlight = false;
let analysisReadiness = { commercial_authority: 'unknown', analysis_admission: 'blocked' };
let pairingStatus = { state: 'unpaired', comparison_code: null };
const isPairingWindow = window.location.hash === '#pairing';
const pairingPort = chrome.runtime.connect({ name: 'ofca.companion.pairing' });
let contextRestored = false;
let pairRequested = false;
let pairingWindowClosing = false;

// The pairing window is a single-task surface: it shows only the pairing card.
const PAIRING_WINDOW_CONNECTED = Object.freeze({
  id: 'pairing_window_connected',
  tone: 'success',
  title: 'Connected to the desktop app',
  body: 'This window closes automatically.',
  primaryAction: null,
  primaryLabel: null,
  secondaryAction: null,
  secondaryLabel: null,
});
if (isPairingWindow) {
  main.dataset.view = 'pairing';
  document.title = 'Pair with the desktop app';
}

function show(element, visible) {
  element.classList.toggle('hidden', !visible);
}

function isShown(element) {
  return !element.classList.contains('hidden');
}

// Stores only where the customer was in the popup; product state is read fresh on every open.
function persistPopupContext() {
  if (isPairingWindow || !contextRestored) return;
  void savePopupContext(chrome.storage.session, {
    view: main.dataset.view,
    full_review_requested: fullReviewRequested,
    initial_choice_dismissed: initialModeChoiceDismissed,
  });
}

function showView(view) {
  if (isPairingWindow) return;
  main.dataset.view = view;
  window.scrollTo(0, 0);
  persistPopupContext();
}

function openView(view) {
  showView(view);
  elements[`${view}-title`].focus();
}

function closeView(view) {
  showView('home');
  elements[`open-${view}`].focus();
}

// Focuses the journey card, which shows the outcome of an action.
function returnHome() {
  showView('home');
  const target = isShown(elements['pre-mode']) ? document.getElementById('activation-title')
    : isShown(elements['mode-choice']) ? document.getElementById('mode-choice-title')
      : elements['journey-title'];
  target.setAttribute('tabindex', '-1');
  target.focus();
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
    full: status.delivery?.transport_state === 'authenticated' ? 'Desktop connected' : 'Full setup in progress',
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
    paused: 'Paused',
    desktop_app_needed: 'Next step',
    desktop_app_unavailable: 'Needs attention',
    setup_incomplete: 'Setup needed',
    pairing_required: 'Next step',
    pairing_in_progress: 'Connecting',
    pairing_failed: 'Try again',
    pairing_not_ready: 'Next step',
    pairing_window_connected: 'Connected',
    activation_checking: 'Checking',
    activation_required: 'Activation required',
    activation_active: 'Needs attention',
    activation_unavailable: 'Needs attention',
    full_ready: 'Ready',
    full_unavailable: 'Needs attention',
  })[state] ?? 'Status';
}

function resetAnalysisReadiness() {
  readinessRequestInFlight = false;
  analysisReadiness = { commercial_authority: 'unknown', analysis_admission: 'blocked' };
}

function renderReadinessStatus() {
  const full = currentStatus?.consent?.mode === 'full';
  if (!full) {
    elements['activation-status'].textContent = 'Not needed for Preview';
    elements['analysis-status'].textContent = 'Preview only';
    return;
  }
  elements['activation-status'].textContent = ({
    required: 'Required',
    active: 'Active',
    unavailable: 'Needs attention',
    unknown: 'Checking…',
  })[analysisReadiness.commercial_authority] ?? 'Checking…';
  elements['analysis-status'].textContent = analysisReadiness.analysis_admission === 'admitted'
    ? 'Ready'
    : analysisReadiness.commercial_authority === 'required'
      ? 'Waiting for activation'
      : analysisReadiness.commercial_authority === 'unavailable'
        ? 'Unavailable'
        : 'Not ready';
}

function renderJourney() {
  if (currentStatus === null) return;
  const journey = isPairingWindow && pairingStatus.state === 'paired' ? PAIRING_WINDOW_CONNECTED : deriveCustomerJourney({
    status: currentStatus,
    pairing: pairingStatus,
    desktopRuntimeReachable,
    desktopDownloadAvailable: secureExternalUrl(companionConfig.desktop_app_download_url) !== null,
    analysisReadiness,
    resumeAvailable: legalStatus !== null && legalStatus.requires_reauthorization !== true,
  });
  elements['journey-card'].dataset.tone = journey.tone;
  elements['journey-badge'].textContent = journeyBadge(journey.id);
  elements['journey-title'].textContent = journey.title;
  elements['journey-body'].textContent = journey.body;
  show(elements['journey-body'], Boolean(journey.body));
  // The required review owns this screen. Do not repeat its task in another card.
  const reviewing = ['pre-mode', 'mode-choice', 'legal-unavailable'].some((id) => isShown(elements[id]));
  show(elements['journey-card'], isPairingWindow || !reviewing);
  show(elements['preview-metrics'], !reviewing && (currentStatus.consent.mode === 'preview'
    || (currentStatus.consent.mode === 'paused' && currentStatus.consent.resume_mode === 'preview')));
  show(elements['history-prompt'], !reviewing && currentStatus.phase === 'full' && currentStatus.history_permission === false);
  // The pairing controls inside the card own pair and cancel, so the journey buttons do not repeat them.
  const pairingShown = isShown(elements['companion-pairing']);
  const pairOwned = journey.primaryAction === 'pair' && pairingShown && isShown(elements['pair-companion']);
  const cancelOwned = journey.secondaryAction === 'cancel_pairing' && pairingShown && isShown(elements['cancel-pairing']);
  if (pairOwned) elements['pair-companion'].textContent = journey.primaryLabel;
  elements['journey-primary'].dataset.action = journey.primaryAction ?? '';
  elements['journey-primary'].textContent = journey.primaryLabel ?? '';
  show(elements['journey-primary'], journey.primaryAction !== null && !pairOwned);
  elements['journey-secondary'].dataset.action = journey.secondaryAction ?? '';
  elements['journey-secondary'].textContent = journey.secondaryLabel ?? '';
  show(elements['journey-secondary'], journey.secondaryAction !== null && !cancelOwned);
  show(elements['open-dashboard'], desktopRuntimeReachable);
}

function renderPairing(value = pairingStatus) {
  pairingStatus = value;
  const pairingActionable = currentStatus?.consent?.mode === 'full'
    && desktopRuntimeReachable
    && !['setup_incomplete', 'unavailable'].includes(value.state);
  show(elements['companion-pairing'], pairingActionable);
  const pending = ['pairing', 'compare'].includes(value.state);
  const paired = value.state === 'paired';
  show(elements['pair-companion'], pairingActionable && !pending && !paired && value.state !== 'desktop_not_ready');
  show(elements['cancel-pairing'], pending);
  show(elements['forget-companion'], paired);
  const code = typeof value.comparison_code === 'string' && /^\d{6}$/u.test(value.comparison_code) ? value.comparison_code : null;
  show(elements['pairing-code'], code !== null);
  elements['pairing-code'].textContent = code === null ? '' : `${code.slice(0, 3)} ${code.slice(3)}`;
  elements['pairing-status'].textContent = ({
    paired: 'This extension is securely paired with the desktop app.',
    pairing: 'Connecting… Keep this window open.',
    compare: 'Compare this code with the desktop app. Confirm there only if both codes match.',
    pairing_failed: 'Connection did not complete. In the desktop app, choose Connect extension again, then try again.',
    desktop_not_ready: 'Open Settings in the desktop app and choose Connect extension. Then choose Pair device here.',
    setup_incomplete: 'Open your creator account in OnlyFans, then return here to continue.',
    unavailable: 'Open your creator account in OnlyFans, then return here to continue.',
  })[value.state] ?? 'Connect this extension to the desktop app to continue Full setup.';
  if (!paired && !pending) resetAnalysisReadiness();
  renderReadinessStatus();
  renderJourney();
  if (isPairingWindow && paired) closePairingWindow(1500);
}

function closePairingWindow(delayMs = 0) {
  if (!isPairingWindow || pairingWindowClosing) return;
  pairingWindowClosing = true;
  setTimeout(() => {
    void chrome.tabs.getCurrent()
      .then((tab) => chrome.tabs.remove(tab.id))
      .catch(() => window.close());
  }, delayMs);
}

function cancelPairing() {
  pairingPort.postMessage({ type: 'cancel' });
  closePairingWindow();
}

function requestAnalysisReadiness() {
  if (
    readinessRequestInFlight
    || currentStatus?.consent?.mode !== 'full'
    || !desktopRuntimeReachable
    || pairingStatus.state !== 'paired'
  ) return;
  readinessRequestInFlight = true;
  pairingPort.postMessage({ type: 'readiness' });
}

pairingPort.onMessage.addListener((value) => {
  if (value?.type === 'analysis_readiness') {
    readinessRequestInFlight = false;
    const commercial = value.commercial_authority;
    const admission = value.analysis_admission;
    analysisReadiness = (
      ['required', 'active', 'unavailable'].includes(commercial)
      && ['blocked', 'admitted'].includes(admission)
      && !(admission === 'admitted' && commercial !== 'active')
    ) ? { commercial_authority: commercial, analysis_admission: admission }
      : { commercial_authority: 'unavailable', analysis_admission: 'blocked' };
    renderReadinessStatus();
    renderJourney();
    return;
  }
  // A result left by an earlier attempt is not this window's outcome.
  const stale = isPairingWindow && !pairRequested && ['desktop_not_ready', 'pairing_failed'].includes(value?.state);
  renderPairing(stale ? { state: 'unpaired', comparison_code: null } : value);
  requestAnalysisReadiness();
});
pairingPort.onDisconnect.addListener(() => {
  readinessRequestInFlight = false;
  analysisReadiness = { commercial_authority: 'unavailable', analysis_admission: 'blocked' };
  renderPairing({ state: 'pairing_failed', comparison_code: null });
});

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
  if (status.requires_reauthorization === true) {
    elements.feedback.textContent = 'Data-handling information changed. Review it before restarting analytics.';
  }
  renderJourney();
  persistPopupContext();
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
    : status.delivery.runtime_ready ? 'Connecting' : 'Off';
  elements['pending-count'].textContent = String(status.delivery.pending_entries);
  const dropCount = Object.values(status.delivery.capture_drop_counts ?? {})
    .reduce((total, value) => total + (Number.isSafeInteger(value) ? value : 0), 0);
  elements['capture-health'].textContent = status.delivery.startup_error_code === 'startup_failed'
    ? 'Full analysis could not start. Start the desktop app and retry.'
    : dropCount > 0 ? `${dropCount} update${dropCount === 1 ? '' : 's'} could not be recorded.` : '';
  elements['history-health'].textContent = !status.delivery.history_error_code
    ? ''
    : 'Message history needs attention in the desktop app.';
  const mode = status.consent.mode;
  const active = ['preview', 'full'].includes(mode);
  const pausedFrom = mode === 'paused' ? status.consent.resume_mode : null;
  const permissionRequired = status.phase === 'permission_required';
  show(elements['restore-access'], permissionRequired && active);
  show(elements['reload-tabs'], status.reload_required === true);
  show(elements['preview-metrics'], mode === 'preview' || pausedFrom === 'preview');
  show(elements['open-connection'], mode === 'full' || pausedFrom === 'full');
  if (main.dataset.view === 'connection' && !isShown(elements['open-connection'])) returnHome();
  show(elements['history-prompt'], status.phase === 'full' && status.history_permission === false);
  show(elements.pause, active && !permissionRequired);
  show(elements.revoke, mode !== 'off' && mode !== 'revoked');
  renderPairing();
  renderReadinessStatus();
  if (legalStatus !== null) renderLegal(legalStatus);
}

async function probeDesktop() {
  if (currentStatus?.consent?.mode !== 'full') {
    desktopRuntimeReachable = false;
    resetAnalysisReadiness();
    renderReadinessStatus();
    renderJourney();
    return false;
  }
  desktopRuntimeReachable = await probeDesktopRuntime();
  if (!desktopRuntimeReachable) resetAnalysisReadiness();
  if (currentStatus !== null) render(currentStatus);
  requestAnalysisReadiness();
  return desktopRuntimeReachable;
}

function restorePopupContext(context) {
  initialModeChoiceDismissed = context.initial_choice_dismissed;
  fullReviewRequested = context.full_review_requested && currentStatus?.consent?.mode === 'preview';
  if (context.view === 'manage' || (context.view === 'connection' && isShown(elements['open-connection']))) {
    openView(context.view);
  }
  contextRestored = true;
  renderLegal(legalStatus);
  if (fullReviewRequested && main.dataset.view === 'home') elements['mode-choice'].scrollIntoView({ block: 'nearest' });
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
    resetAnalysisReadiness();
    render(result.status);
    legalStatus = await sendLegal({ type: LEGAL_ACTIVATION_STATUS_MESSAGE_TYPE });
    fullReviewRequested = false;
    initialModeChoiceDismissed = false;
    renderLegal(legalStatus);
    if (mode === 'full' && result.status.phase === 'identity') {
      await probeDesktop();
      elements.feedback.textContent = ''; // The journey card already describes the next task.
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
    resetAnalysisReadiness();
    const status = await send({ type: UI_TRANSITION_MESSAGE_TYPE, mode });
    render(status);
    await probeDesktop();
  } catch (error) {
    elements.feedback.textContent = error.message ?? 'The change could not be applied.';
  } finally {
    setBusy(false);
  }
}

// The toolbar popup closes when focus moves, so pairing runs in its own small window.
async function openPairingWindow() {
  if (isPairingWindow) {
    pairRequested = true;
    pairingPort.postMessage({ type: 'pair' });
    return;
  }
  const url = chrome.runtime.getURL('popup.html#pairing');
  try {
    const existing = (await chrome.runtime.getContexts({ contextTypes: ['TAB'] }))
      .find((context) => context.documentUrl === url);
    if (existing) {
      await chrome.tabs.reload(existing.tabId);
      await chrome.windows.update(existing.windowId, { focused: true });
      return;
    }
    await chrome.windows.create({ url, type: 'popup', width: 400, height: 520, focused: true });
  } catch {
    elements.feedback.textContent = 'The pairing window could not be opened.';
  }
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
      elements.feedback.textContent = 'The desktop app download is unavailable.';
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
    await openPairingWindow();
    return;
  }
  if (action === 'cancel_pairing') {
    cancelPairing();
    return;
  }
  if (action === 'open_dashboard') {
    await chrome.tabs.create({ url: companionConfig.dashboard_url });
    return;
  }
  if (action === 'open_desktop_settings') {
    await chrome.tabs.create({ url: companionConfig.history_settings_url });
    return;
  }
  if (action === 'retry_readiness') {
    resetAnalysisReadiness();
    renderReadinessStatus();
    renderJourney();
    requestAnalysisReadiness();
    return;
  }
  if (action === 'resume' && currentStatus?.consent?.mode === 'paused') {
    await transition('resume');
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
    resetAnalysisReadiness();
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
elements['not-now-preview'].addEventListener('click', () => {
  initialModeChoiceDismissed = true;
  renderLegal(legalStatus);
});
elements['full-secondary'].addEventListener('click', () => {
  if ((currentStatus?.consent?.mode ?? null) === 'preview') fullReviewRequested = false;
  else initialModeChoiceDismissed = true;
  renderLegal(legalStatus);
});
elements['open-connection'].addEventListener('click', () => openView('connection'));
elements['open-manage'].addEventListener('click', () => openView('manage'));
elements['connection-back'].addEventListener('click', () => closeView('connection'));
elements['manage-back'].addEventListener('click', () => closeView('manage'));
elements.pause.addEventListener('click', () => {
  returnHome();
  void transition('pause');
});
elements.revoke.addEventListener('click', () => {
  if (window.confirm('Revoke site access and stop all new observations? Existing desktop-app data is retained.')) {
    returnHome();
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
  returnHome();
  setBusy(true);
  try {
    resetAnalysisReadiness();
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
elements['pair-companion'].addEventListener('click', () => { void openPairingWindow(); });
elements['cancel-pairing'].addEventListener('click', cancelPairing);
elements['forget-companion'].addEventListener('click', () => {
  if (window.confirm('Forget the desktop app and stop this connection? You will need to pair again to use Full analysis.')) {
    returnHome();
    resetAnalysisReadiness();
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
  const savedContext = isPairingWindow ? null : loadPopupContext(chrome.storage.session);
  try {
    await loadCompanionConfig();
    const [status, legal, context] = await Promise.all([
      send({ type: UI_STATUS_MESSAGE_TYPE }),
      sendLegal({ type: LEGAL_ACTIVATION_STATUS_MESSAGE_TYPE }),
      savedContext,
    ]);
    render(status);
    renderLegal(legal);
    if (context !== null) restorePopupContext(context);
    await probeDesktop();
    if (isPairingWindow && currentStatus?.consent?.mode === 'full') await openPairingWindow();
  } catch (_error) {
    elements.feedback.textContent = 'Extension status is temporarily unavailable. Close this popup and try again.';
  } finally {
    setBusy(false);
  }
}

void initialize();
