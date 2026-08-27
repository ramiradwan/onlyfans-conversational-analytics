import {
  LOCAL_ANALYTICS_ORIGIN_PATTERN,
  ONLYFANS_ORIGIN_PATTERN,
  UI_CLEAR_PREVIEW_MESSAGE_TYPE,
  UI_DELETE_LOCAL_DATA_MESSAGE_TYPE,
  UI_STATUS_MESSAGE_TYPE,
  UI_TRANSITION_MESSAGE_TYPE,
} from './runtime/consent-controller.mjs';

const elements = Object.fromEntries([
  'mode-label',
  'messages-count',
  'chats-count',
  'inbound-count',
  'outbound-count',
  'brain-status',
  'delivery-status',
  'pending-count',
  'feedback',
  'enable-preview',
  'enable-full',
  'resume',
  'pause',
  'history',
  'open-dashboard',
  'revoke',
  'clear-preview',
  'delete-local-data',
  'privacy-link',
].map((id) => [id, document.getElementById(id)]));

let companionConfig = {
  dashboard_url: 'http://bridge.localhost:17871/',
  history_settings_url: 'http://bridge.localhost:17871/settings',
  privacy_policy_url: '',
};
let currentStatus = null;

function show(element, visible) {
  element.classList.toggle('hidden', !visible);
}

function setBusy(busy) {
  document.querySelectorAll('button').forEach((button) => { button.disabled = busy; });
}

function phaseLabel(status) {
  const labels = {
    off: 'Analytics off — no OnlyFans access',
    preview: 'Activity preview enabled',
    identity: 'Full consent saved — finish connecting the local service',
    full: 'Full local analytics enabled',
    paused: 'Analytics paused',
    revoked: 'Site access revoked',
    permission_required: 'Site access needs approval',
    transitioning: 'Applying your choice…',
    unavailable: 'Capture unavailable — reopen Chrome and retry',
  };
  return labels[status.phase] ?? 'Analytics inactive';
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
  show(
    elements['enable-preview'],
    (!active && mode !== 'paused') || (permissionRequired && mode === 'preview'),
  );
  show(
    elements['enable-full'],
    (mode !== 'full' && mode !== 'paused') || (permissionRequired && mode === 'full'),
  );
  show(elements.resume, mode === 'paused');
  show(elements.pause, active && !permissionRequired);
  show(elements.history, status.phase === 'full');
  show(elements.revoke, mode !== 'off' && mode !== 'revoked');
}

async function send(message) {
  const response = await chrome.runtime.sendMessage(message);
  if (response?.ok !== true) throw new Error(response?.code ?? 'request_failed');
  return response.status;
}

async function requestAnalyticsAccess(mode) {
  const origins = [ONLYFANS_ORIGIN_PATTERN];
  if (mode === 'full') origins.push(LOCAL_ANALYTICS_ORIGIN_PATTERN);
  const granted = await chrome.permissions.request({ origins });
  if (!granted) throw new Error('Required site access was not granted. Nothing was enabled.');
}

async function transition(mode) {
  setBusy(true);
  elements.feedback.textContent = '';
  try {
    if (['preview', 'full', 'resume'].includes(mode)) {
      const requestedMode = mode === 'resume'
        ? currentStatus?.consent?.resume_mode
        : mode;
      await requestAnalyticsAccess(requestedMode);
    }
    const status = await send({ type: UI_TRANSITION_MESSAGE_TYPE, mode });
    render(status);
    if (mode === 'full' && status.phase === 'identity') {
      elements.feedback.textContent = 'Open the local dashboard and finish connecting, then return here.';
      await chrome.tabs.create({ url: companionConfig.dashboard_url });
    }
  } catch (error) {
    elements.feedback.textContent = error.message ?? 'The change could not be applied.';
  } finally {
    setBusy(false);
  }
}

async function openUrl(url) {
  await chrome.tabs.create({ url });
}

elements['enable-preview'].addEventListener('click', () => { void transition('preview'); });
elements['enable-full'].addEventListener('click', () => { void transition('full'); });
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
    'Delete all data stored by this extension, disconnect the local service, revoke site access, and stop analytics?',
  )) return;
  setBusy(true);
  try {
    render(await send({ type: UI_DELETE_LOCAL_DATA_MESSAGE_TYPE }));
    elements.feedback.textContent = 'All local extension data was deleted.';
  } catch (_error) {
    elements.feedback.textContent = 'Local extension data could not be fully deleted.';
  } finally {
    setBusy(false);
  }
});
elements['open-dashboard'].addEventListener('click', () => { void openUrl(companionConfig.dashboard_url); });
elements.history.addEventListener('click', async () => {
  setBusy(true);
  elements.feedback.textContent = '';
  try {
    const granted = await chrome.permissions.request({
      permissions: ['webRequest'],
      origins: [ONLYFANS_ORIGIN_PATTERN],
    });
    if (!granted) throw new Error('History access was not granted. Live analytics is unchanged.');
    await openUrl(companionConfig.history_settings_url);
  } catch (error) {
    elements.feedback.textContent = error.message ?? 'History setup could not be opened.';
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
    render(await send({ type: UI_STATUS_MESSAGE_TYPE }));
  } catch (_error) {
    elements.feedback.textContent = 'Local extension status is temporarily unavailable.';
  } finally {
    setBusy(false);
  }
}

void initialize();
