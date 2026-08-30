import {
  LOCAL_ANALYTICS_ORIGIN_PATTERN,
  ONLYFANS_ORIGIN_PATTERN,
  UI_CLEAR_PREVIEW_MESSAGE_TYPE,
  UI_DELETE_LOCAL_DATA_MESSAGE_TYPE,
  UI_STATUS_MESSAGE_TYPE,
  UI_TRANSITION_MESSAGE_TYPE,
} from './runtime/consent-controller.mjs';
import {
  LEGAL_ACTIVATION_STATUS_MESSAGE_TYPE,
  LEGAL_ACCEPT_TERMS_MESSAGE_TYPE,
  LEGAL_ACKNOWLEDGE_RISK_MESSAGE_TYPE,
  LEGAL_ACTIVATE_SOFTWARE_MESSAGE_TYPE,
  LEGAL_CHOOSE_MODE_MESSAGE_TYPE,
} from './runtime/legal-activation-controller.mjs';

const ids = [
  'mode-label',
  'messages-count',
  'chats-count',
  'inbound-count',
  'outbound-count',
  'brain-status',
  'delivery-status',
  'pending-count',
  'feedback',
  'legal-unavailable',
  'pre-mode',
  'terms-accepted',
  'risk-acknowledged',
  'terms-link',
  'risk-link',
  'activate-software',
  'mode-choice',
  'preview-disclosure',
  'full-disclosure',
  'enable-preview',
  'enable-full',
  'not-now-preview',
  'full-secondary',
  'review-full',
  'resume',
  'pause',
  'history',
  'open-dashboard',
  'revoke',
  'clear-preview',
  'delete-local-data',
  'privacy-link',
];
const elements = Object.fromEntries(ids.map((id) => [id, document.getElementById(id)]));

let companionConfig = {
  dashboard_url: 'http://bridge.localhost:17871/',
  history_settings_url: 'http://bridge.localhost:17871/settings',
  privacy_policy_url: '',
};
let currentStatus = null;
let legalStatus = null;
let fullReviewRequested = false;
let initialModeChoiceDismissed = false;

function show(element, visible) {
  element.classList.toggle('hidden', !visible);
}

function setBusy(busy) {
  document.querySelectorAll('button,input').forEach((control) => {
    control.disabled = busy || control.dataset.locked === 'true';
  });
}

function phaseLabel(status) {
  return ({
    off: 'Analytics off — no OnlyFans access',
    preview: 'Activity preview enabled',
    identity: 'Full authorization saved — finish connecting the local service',
    full: 'Full local analytics enabled',
    paused: 'Analytics paused',
    revoked: 'Site access revoked',
    permission_required: 'Site access needs approval',
    transitioning: 'Applying your choice…',
    unavailable: 'Capture unavailable — reopen Chrome and retry',
  })[status.phase] ?? 'Analytics inactive';
}

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
  const origins = [ONLYFANS_ORIGIN_PATTERN];
  if (mode === 'full') origins.push(LOCAL_ANALYTICS_ORIGIN_PATTERN);
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
    && (
      flow.terms_event_id === null
      || flow.risk_event_id === null
      || flow.stage === 'pre_mode'
    );

  show(elements['pre-mode'], status.configured && needsPreMode);
  elements['terms-accepted'].checked = flow.terms_event_id !== null;
  elements['risk-acknowledged'].checked = flow.risk_event_id !== null;
  elements['terms-accepted'].dataset.locked = flow.terms_event_id !== null ? 'true' : 'false';
  elements['risk-acknowledged'].dataset.locked = flow.risk_event_id !== null ? 'true' : 'false';
  elements['activate-software'].disabled = flow.terms_event_id === null || flow.risk_event_id === null;

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
}

function render(status) {
  currentStatus = status;
  elements['mode-label'].textContent = phaseLabel(status);
  elements['messages-count'].textContent = String(status.preview.message_observations);
  elements['chats-count'].textContent = String(status.preview.chat_observations);
  elements['inbound-count'].textContent = String(status.preview.inbound_observations);
  elements['outbound-count'].textContent = String(status.preview.outbound_observations);
  elements['brain-status'].textContent = status.brain_reachable ? 'Available locally' : 'Not detected';
  elements['delivery-status'].textContent = status.delivery.socket_open
    ? 'Connected locally'
    : status.delivery.runtime_ready ? 'Waiting locally' : 'Inactive';
  elements['pending-count'].textContent = String(status.delivery.pending_entries);
  const mode = status.consent.mode;
  const active = ['preview', 'full'].includes(mode);
  const permissionRequired = status.phase === 'permission_required';
  show(elements.pause, active && !permissionRequired);
  show(elements.history, status.phase === 'full');
  show(elements.revoke, mode !== 'off' && mode !== 'revoked');
  if (legalStatus !== null) renderLegal(legalStatus);
}

async function refresh() {
  const [status, legal] = await Promise.all([
    send({ type: UI_STATUS_MESSAGE_TYPE }),
    sendLegal({ type: LEGAL_ACTIVATION_STATUS_MESSAGE_TYPE }),
  ]);
  render(status);
  renderLegal(legal);
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
      elements.feedback.textContent = 'Open the local dashboard and finish connecting, then return here.';
      await chrome.tabs.create({ url: companionConfig.dashboard_url });
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
    if (mode === 'resume') {
      const requestedMode = currentStatus?.consent?.resume_mode;
      await requestAnalyticsAccess(requestedMode);
    }
    const status = await send({ type: UI_TRANSITION_MESSAGE_TYPE, mode });
    render(status);
  } catch (error) {
    elements.feedback.textContent = error.message ?? 'The change could not be applied.';
  } finally {
    setBusy(false);
  }
}

elements['terms-accepted'].addEventListener('change', () => {
  if (elements['terms-accepted'].checked) {
    void legalAction(LEGAL_ACCEPT_TERMS_MESSAGE_TYPE, elements['terms-accepted']);
  }
});
elements['risk-acknowledged'].addEventListener('change', () => {
  if (elements['risk-acknowledged'].checked) {
    void legalAction(LEGAL_ACKNOWLEDGE_RISK_MESSAGE_TYPE, elements['risk-acknowledged']);
  }
});
elements['activate-software'].addEventListener('click', () => {
  void legalAction(LEGAL_ACTIVATE_SOFTWARE_MESSAGE_TYPE);
});
elements['enable-preview'].addEventListener('click', () => { void chooseMode('preview'); });
elements['enable-full'].addEventListener('click', () => { void chooseMode('full'); });
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
  if (window.confirm('Revoke site access and stop all new observations? Existing local service data is retained.')) {
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
    'Delete all data stored by this extension, including activation evidence, disconnect the local service, revoke site access, and stop analytics?',
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
  companionConfig = { ...companionConfig, ...candidate };
  try {
    const privacy = new URL(companionConfig.privacy_policy_url);
    if (privacy.protocol === 'https:' && !privacy.hostname.endsWith('.invalid')) {
      elements['privacy-link'].href = privacy.href;
      elements['privacy-link'].textContent = 'Privacy policy';
      elements['privacy-link'].removeAttribute('aria-disabled');
      elements['privacy-link'].target = '_blank';
      elements['privacy-link'].rel = 'noreferrer';
    }
  } catch (_error) {
    // Invalid or absent URLs keep the link disabled.
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
  } catch (_error) {
    elements.feedback.textContent = 'Local extension status is temporarily unavailable.';
  } finally {
    setBusy(false);
  }
}

void initialize();
