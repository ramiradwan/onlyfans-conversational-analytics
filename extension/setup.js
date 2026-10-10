// Extension-owned setup presentation. Existing runtime controllers retain authority.
import { OBSERVER_REOPEN_TYPE } from './runtime/document-observer.mjs';
import { WORKSPACE_MESSAGE_TYPE } from './runtime/onboarding-entry.mjs';
import { WORKSPACE_RECORD_KEY } from './runtime/onboarding-workspace.mjs';
import { FULL_REVIEW_INTENT_KEY, createFullReviewIntentConsumer, createFullReviewPersistence } from './runtime/onboarding-full-intent.mjs';
import { LEGAL_ACCEPT_TERMS_MESSAGE_TYPE, LEGAL_ACKNOWLEDGE_RISK_MESSAGE_TYPE,
  LEGAL_ACTIVATE_SOFTWARE_MESSAGE_TYPE, LEGAL_CHOOSE_MODE_MESSAGE_TYPE } from './runtime/legal-activation-controller.mjs';
import { createSurfaceClient, openSurface, send, secureExternalUrl, NoticeError } from './ui/surface-client.mjs';
import { customerJourney, needsAgreement, modeChoiceAvailable } from './ui/presentation.mjs';
import { element, show, text, renderLoading, renderJourney, renderReadiness, renderLegalLinks, createPageActions } from './ui/dom.mjs';
import { chooseMode, transition, restoreAccess, openCreatorAccount } from './ui/actions.mjs';
import { createHandoffFinisher, returnToDesktop } from './ui/handoff.mjs';
import { bindSetupTransfer, renderReceivingContext } from './ui/setup-transfer.mjs';
import { SETUP_TRANSFER_MESSAGE } from './runtime/setup-transfer.mjs';
import { createDesktopLaunch, desktopLaunchJourney } from './ui/desktop-launch.mjs';
import { createWorkspaceNavigation } from './ui/workspace-navigation.mjs';
import { LOCAL_SERVICE_ORIGIN } from './transport/local-service-endpoints.mjs';
import { desktopStage } from './runtime/desktop-port.mjs';
import { createSetupChoice } from './ui/setup-choice.mjs';
import { previewCounts } from './ui/preview-counts.mjs';

let failed = false;
let dismissed = false;
let reviewStep = null;
// '#desktop' is the compact window the desktop app opens. It is presentation
// only: every action on this page keeps its usual authorization.
let handoff = location.hash === '#desktop';
let fullReviewRequested = location.hash === '#full' || handoff;
const fullReviewPersistence = createFullReviewPersistence();
const client = createSurfaceClient((model) => { if (model.status) failed = false; render(model); }, () => { failed = true; render(client.model); });
const page = createPageActions(client, render);
const steps = ['agree', 'mode', 'connect', 'activate'];
let currentJourney = null;
let workspaceRecord = null;
const choicePersistence = createSetupChoice();
let setupChoice = null;
let persistentHandoff = false, handoffReturnAttempted = false;
createWorkspaceNavigation({ workspace: () => workspaceRecord });
const desktopLaunch = createDesktopLaunch({ workspace: () => workspaceRecord,
  prepare: () => send({ type: WORKSPACE_MESSAGE_TYPE, action: 'prepare_launch' }),
  dispatch: (url) => window.location.assign(url),
  navigate: () => {
    if (!workspaceRecord || location.href !== `${chrome.runtime.getURL('setup.html')}#journey=${workspaceRecord.journey_id}`) throw Error('workspace_changed');
    location.replace(`${LOCAL_SERVICE_ORIGIN}/#journey=${workspaceRecord.journey_id}`);
  },
  changed: () => render(client.model),
});
window.addEventListener('pagehide', () => desktopLaunch.stop());
let checkboxDraft = { terms_checked: false, risk_checked: false, full_checked: false };
let receivingRead = 0, receivingExpiry = null;
const consumeFullReviewIntent = createFullReviewIntentConsumer({ apply() {
  reviewStep = null; dismissed = false; setFullReview(true);
  setupChoice = { mode: 'full', stage: 'review' }; choicePersistence.write(workspaceRecord, setupChoice);
} });
async function readWorkspace() {
  if (!location.hash.startsWith('#journey=')) return;
  const generation = ++receivingRead;
  workspaceRecord = null;
  setupChoice = null;
  fullReviewRequested = false;
  checkboxDraft = { terms_checked: false, risk_checked: false, full_checked: false };
  clearTimeout(receivingExpiry);
  renderReceivingContext(document, null);
  try {
    const next = await send({ type: WORKSPACE_MESSAGE_TYPE, action: 'read' });
    const intent = (await chrome.storage.session.get([FULL_REVIEW_INTENT_KEY]))[FULL_REVIEW_INTENT_KEY];
    if (generation !== receivingRead) return;
    workspaceRecord = next;
    setupChoice = choicePersistence.read(workspaceRecord);
    fullReviewRequested = fullReviewPersistence.read(workspaceRecord);
    checkboxDraft = { ...workspaceRecord.draft };
    consumeFullReviewIntent(intent, workspaceRecord);
    const handoffContext = await send({ type: WORKSPACE_MESSAGE_TYPE, action: 'desktop_handoff' });
    if (generation !== receivingRead) return;
    persistentHandoff = handoffContext?.expires_at > Date.now();
    render(client.model);
    const receiving = await send({ type: SETUP_TRANSFER_MESSAGE, action: 'context' });
    if (generation !== receivingRead) return;
    if (receiving?.expires_at > Date.now()) {
      renderReceivingContext(document, receiving);
      receivingExpiry = setTimeout(() => renderReceivingContext(document, null), receiving.expires_at - Date.now());
    }
    render(client.model);
  } catch {}
}
async function saveDraft() {
  checkboxDraft = { terms_checked: element('terms-accepted').checked, risk_checked: element('risk-acknowledged').checked, full_checked: fullReviewRequested };
  if (!workspaceRecord) return;
  workspaceRecord.draft = { ...checkboxDraft };
  await send({ type: WORKSPACE_MESSAGE_TYPE, action: 'draft', scope_id: workspaceRecord.draft_scope.scope_id, draft: workspaceRecord.draft });
}
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'session' && changes[FULL_REVIEW_INTENT_KEY]?.newValue) {
    void readWorkspace(); return;
  }
  if (area !== 'local' || !changes[WORKSPACE_RECORD_KEY]) return;
  const next = changes[WORKSPACE_RECORD_KEY].newValue;
  if (!workspaceRecord || next?.journey_id !== workspaceRecord.journey_id
    || next?.draft_scope?.scope_id !== workspaceRecord.draft_scope.scope_id
    || next?.draft_scope?.disclosure_bundle_id !== workspaceRecord.draft_scope.disclosure_bundle_id) void readWorkspace();
});
void readWorkspace();

function setFullReview(value) {
  fullReviewRequested = value;
  fullReviewPersistence.write(workspaceRecord, value);
}
function renderProgress(model, view, journey) {
  const full = model.status.consent.mode === 'full' || fullReviewRequested;
  const count = full ? 4 : 2;
  const connected = model.pairing.state === 'paired' && model.desktopRuntimeReachable
    && model.status.delivery?.transport_state === 'authenticated';
  const complete = view === 'journey' && ['preview_available', 'full_ready'].includes(journey.id);
  const index = view === 'agree' ? 0 : ['mode', 'access'].includes(view) ? 1 : connected ? 3 : 2;
  show('setup-progress', full && !complete);
  document.querySelector('.onboarding-layout').dataset.progress = full && !complete ? 'full' : 'none';
  steps.forEach((step, position) => {
    const item = document.querySelector(`#setup-progress [data-step="${step}"]`);
    item.classList.toggle('hidden', position >= count);
    item.dataset.complete = String(complete || position < index);
    if (!complete && position === index) item.setAttribute('aria-current', 'step'); else item.removeAttribute('aria-current');
    page.lock(`step-${step}`, !complete && position > index);
  });
  text('step-summary', complete ? (full ? 'Setup complete' : 'Preview is ready')
    : view === 'agree' ? 'Review terms' : view === 'mode' ? 'Choose a mode'
      : view === 'access' ? 'Browser site access' : full ? (connected ? 'Full activation' : 'Connect the desktop app') : journey.title);
}
function render(model) {
  desktopLaunch.observe(model);
  const { status, legal } = model;
  for (const id of ['start-choice', 'pre-mode', 'mode-choice', 'access-card', 'journey-card', 'legal-unavailable', 'setup-progress', 'back-current']) show(id, false);
  renderLoading(status, failed);
  renderLegalLinks(legal);
  if (!status || !legal) {
    delete document.querySelector('main').dataset.ready;
    text('pairing-code', ''); show('pairing-code', false);
    return;
  }
  document.querySelector('main').dataset.ready = 'true';
  if (!legal.configured) { show('legal-unavailable', true); text('step-summary', 'Setup unavailable'); return; }
  const mode = status.consent.mode;
  const agreement = needsAgreement(model);
  // A new legal review supersedes an earlier optional mode-choice dismissal.
  if (agreement) dismissed = false;
  const active = ['preview', 'full'].includes(mode) && !legal.requires_reauthorization;
  const choose = (modeChoiceAvailable(model) && !dismissed) || (mode === 'preview' && fullReviewRequested) || reviewStep === 'mode';
  const access = status.phase === 'permission_required';
  const selected = setupChoice?.mode ?? (fullReviewRequested ? 'full' : legal.requires_reauthorization
    ? status.consent.resume_mode ?? (['preview', 'full'].includes(mode) ? mode : null) : null);
  const pendingAccess = selected && setupChoice?.stage === 'access' && !agreement
    && legal.flow.stage === 'mode_selection';
  const view = agreement || reviewStep === 'agree' ? selected || reviewStep === 'agree' ? 'agree' : 'start'
    : pendingAccess ? 'access' : choose ? 'mode' : access ? 'access' : 'journey';
  currentJourney = desktopLaunchJourney(customerJourney(model), desktopLaunch.state);
  const icon = currentJourney.id === 'full_ready' || (currentJourney.id === 'preview_available' && status.observer?.attachment === 'ready') ? '✓'
    : currentJourney.id === 'paused' ? 'Ⅱ'
      : currentJourney.tone === 'progress' ? '↻' : currentJourney.tone === 'warning' || currentJourney.tone === 'error' ? '!' : '…';
  text('journey-icon', icon);
  document.querySelector('main').dataset.step = view;
  renderProgress(model, view, currentJourney);
  text('step-summary', '');
  show('back-current', reviewStep !== null);
  show('pre-mode', view === 'agree'); show('mode-choice', view === 'mode');
  show('start-choice', view === 'start');
  show('start-back', view === 'agree' && Boolean(setupChoice) && !legal.requires_reauthorization);
  if (view === 'agree' && selected) {
    text('activation-title', selected === 'full' ? 'Turn on Full analytics' : 'Turn on Preview');
    text('activate-software', selected === 'full' ? 'Continue with Full analytics' : 'Continue with Preview');
    const disclosure = element('start-disclosure');
    if (disclosure.dataset.mode !== selected) {
      const original = element(selected === 'full' ? 'full-disclosure' : 'preview-disclosure');
      disclosure.replaceChildren(original.querySelector('.points').cloneNode(true));
      if (selected === 'preview') disclosure.append(original.querySelector('.fine-print').cloneNode(true));
      disclosure.dataset.mode = selected;
    }
  }
  show('access-card', view === 'access'); show('journey-card', view === 'journey');
  if (!page.busy) {
    element('terms-accepted').checked = Boolean(legal.flow.terms_event_id || checkboxDraft.terms_checked);
    element('risk-acknowledged').checked = Boolean(legal.flow.risk_event_id || checkboxDraft.risk_checked);
  }
  page.lock('terms-accepted', Boolean(legal.flow.terms_event_id) || active);
  page.lock('risk-acknowledged', Boolean(legal.flow.risk_event_id) || active);
  page.lock('activate-software', active);
  show('activate-software', !active);
  const reviewFull = fullReviewRequested || (reviewStep === 'mode' && mode === 'full');
  show('preview-disclosure', view === 'mode' && !reviewFull);
  show('full-disclosure', view === 'mode' && reviewFull);
  show('enable-preview', !active); show('enable-full', mode !== 'full' || legal.requires_reauthorization);
  text('full-secondary', mode === 'preview' ? 'Keep Preview' : 'Not now');
  text('access-title', 'Allow site access');
  text('access-body', 'Allow the extension to read activity from your creator account.');
  show('restore-access', Boolean(pendingAccess) || status.phase === 'permission_required');
  renderJourney(currentJourney, model);
  const preview = mode === 'preview' || (mode === 'paused' && status.consent.resume_mode === 'preview');
  show('preview-metrics', preview && view === 'journey');
  show('pause', ['preview', 'full'].includes(mode) && model.pairing.desktop_control !== true);
  show('background-tab-note', status.observer?.helper === 'open');
  show('reopen-background-tab', status.observer?.helper === 'closed');
  const daily = previewCounts(status.preview);
  text('metrics-range', daily.label);
  for (const [id, key] of [['messages-count', 'message_observations'], ['inbound-count', 'inbound_observations'], ['outbound-count', 'outbound_observations']]) text(id, new Intl.NumberFormat().format(daily.counts[key] ?? 0));
  if (currentJourney.id === 'preview_available') {
    text('journey-title', status.observer?.attachment === 'ready' ? 'Preview is ready' : 'Waiting for OnlyFans activity');
    text('journey-badge', status.observer?.attachment === 'ready' ? 'Counting as you use OnlyFans' : '');
    text('journey-body', '');
  }
  if (currentJourney.id === 'full_ready') text('journey-body', 'Open analytics to see your conversations.');
  renderPairing(model, currentJourney);
  renderReadiness(model); show('ready-details', currentJourney.id === 'full_ready');
  for (const [id, action, label] of [['journey-primary', currentJourney.primaryAction, currentJourney.primaryLabel],
    ['journey-secondary', currentJourney.secondaryAction, currentJourney.secondaryLabel]]) {
    element(id).dataset.action = action ?? ''; text(id, label ?? '');
    show(id, Boolean(action) && !['pair', 'cancel_pairing'].includes(action));
  }
  if (model.desktopLinked && currentJourney.id === 'pairing_required') {
    text('journey-body', 'Choose Connect extension in the desktop app to finish.');
  }
  renderHandoff(model, view, currentJourney);
  show('review-notice', legal.requires_reauthorization === true);
}
// Extension-side steps for the desktop app end once Full mode, site access and
// the creator account are in place. Pairing continues in the desktop app.
const HANDOFF_PENDING = new Set(['analytics_off', 'preview_available', 'paused', 'setup_incomplete', 'desktop_app_needed']);
function renderHandoff(model, view, journey) {
  const stage = desktopStage({ consent: model.status, legal: model.legal, pairing: model.pairing });
  if (persistentHandoff && view === 'journey' && model.status.consent.mode === 'full'
    && ['ready_to_pair', 'paired'].includes(stage)) {
    text('journey-title', 'Continue setup');
    text('journey-body', 'Returning to setup…');
    show('journey-primary', false); show('journey-secondary', false); show('companion-pairing', false);
    if (!handoffReturnAttempted) {
      handoffReturnAttempted = true;
      void send({ type: WORKSPACE_MESSAGE_TYPE, action: 'return_desktop_handoff' }).catch(() => {
        persistentHandoff = false;
        render(client.model);
        text('journey-body', 'Couldn’t return to setup.');
      });
    }
    return;
  }
  const done = handoff && view === 'journey' && model.status.consent.mode === 'full' && !HANDOFF_PENDING.has(journey.id);
  document.querySelector('main').dataset.handoff = handoff ? (done ? 'complete' : 'active') : '';
  if (!done) return;
  if (!['pairing', 'compare'].includes(model.pairing.state)) show('companion-pairing', false);
  text('journey-title', 'Continue in the desktop app');
  text('journey-body', model.desktopLinked
    ? 'This browser is ready. Returning you to the desktop app…'
    : 'This browser is ready. Open the desktop app to connect it.');
  element('journey-primary').dataset.action = 'return_to_desktop';
  text('journey-primary', 'Return to the desktop app'); show('journey-primary', true);
  show('journey-secondary', false);
  if (model.desktopLinked) void finishHandoff();
}
const finishHandoff = createHandoffFinisher(() => client.model.config.dashboard_url);
function renderPairing(model, journey) {
  const pending = ['pairing', 'compare'].includes(model.pairing.state);
  // While the desktop app is open in this browser, it owns starting a pairing.
  const pairAction = (journey.primaryAction === 'pair' || journey.secondaryAction === 'pair') && !model.desktopLinked;
  show('companion-pairing', pending || pairAction);
  show('pair-companion', pairAction && !pending);
  element('pair-companion').className = (journey.primaryAction === 'pair' ? 'primary' : 'secondary') + (pairAction && !pending ? '' : ' hidden');
  text('pair-companion', journey.primaryAction === 'pair' ? journey.primaryLabel : journey.secondaryLabel ?? 'Pair device');
  const code = pending ? model.pairing.comparison_code : null;
  show('pairing-code', code !== null); show('pairing-label', code !== null);
  text('pairing-code', code ? `${code.slice(0, 3)} ${code.slice(3)}` : '');
  show('cancel-pairing', pending && model.pairing.owns_attempt);
  show('open-pairing-desktop', pending); show('pairing-note', pending);
  text('pairing-note', model.pairing.owns_attempt
    ? 'Closing this page cancels this attempt. Switching to the desktop app does not.'
    : model.pairing.desktop_attempt
      ? 'This connection was started in the desktop app. Continue there to finish or cancel it.'
      : 'This connection was started in another setup tab. Continue there to finish or cancel it.');
}
function runJourneyAction(action) {
  if (action === 'review_full' || action === 'choose_mode') {
    reviewStep = null; dismissed = false; setFullReview(action === 'review_full'); render(client.model); return;
  }
  if (action === 'pair') return client.post('pair');
  if (action === 'cancel_pairing') return client.post('cancel');
  if (action === 'return_to_desktop') return returnToDesktop(client.model.config.dashboard_url);
  if (action === 'open_desktop') return desktopLaunch.launch(client.model);
  if (action === 'open_dashboard') return send({ type: WORKSPACE_MESSAGE_TYPE, action: 'navigate', route: 'bridge' });
  if (action === 'open_desktop_settings') return chrome.tabs.create({ url: client.model.config.history_settings_url });
  if (action === 'open_creator_account') return openCreatorAccount();
  if (action === 'retry_readiness') return client.sync();
  if (action === 'resume') return transition('resume', client.model);
  if (action === 'retry_full') return transition('full', client.model);
  if (action === 'install_desktop') {
    const url = secureExternalUrl(client.model.config.desktop_app_download_url);
    if (!url) throw new NoticeError('The desktop download is unavailable in this version.');
    return chrome.tabs.create({ url });
  }
}
async function enableMode(mode) {
  await chooseMode(mode);
  setupChoice = null; choicePersistence.write(workspaceRecord, null);
  setFullReview(false); reviewStep = null; dismissed = false;
}
for (const id of ['terms-accepted', 'risk-acknowledged']) {
  element(id).addEventListener('change', () => { void saveDraft().catch(() => undefined); });
}
element('activate-software').addEventListener('click', () => {
  const missing = ['terms-accepted', 'risk-acknowledged'].find((id) => !element(id).checked);
  if (missing) {
    page.feedback('Check both boxes to continue.', true);
    element(missing).focus();
    return;
  }
  void page.run(async () => {
    const chosen = setupChoice?.mode ?? (fullReviewRequested ? 'full' : client.model.status.consent.resume_mode ?? null);
    const chosenScope = workspaceRecord?.draft_scope?.scope_id ?? null;
    if (!client.model.legal.flow.terms_event_id) await send({ type: LEGAL_ACCEPT_TERMS_MESSAGE_TYPE });
    if (!client.model.legal.flow.risk_event_id) await send({ type: LEGAL_ACKNOWLEDGE_RISK_MESSAGE_TYPE });
    if (chosen) { setupChoice = { mode: chosen, stage: 'access' }; choicePersistence.write(workspaceRecord, setupChoice); }
    await send({ type: LEGAL_ACTIVATE_SOFTWARE_MESSAGE_TYPE });
    if (chosen && chosenScope === (workspaceRecord?.draft_scope?.scope_id ?? null)
      && client.model.status.onlyfans_permission === true
      && (chosen === 'preview' || client.model.status.local_service_permission === true)) {
      await send({ type: LEGAL_CHOOSE_MODE_MESSAGE_TYPE, mode: chosen });
      setupChoice = null; choicePersistence.write(workspaceRecord, null); setFullReview(false);
    }
  });
});
for (const mode of ['preview', 'full']) page.bind(`start-${mode}`, () => {
  setupChoice = { mode, stage: 'review' }; choicePersistence.write(workspaceRecord, setupChoice);
  setFullReview(mode === 'full'); render(client.model);
});
page.bind('start-back', () => {
  setupChoice = null; choicePersistence.write(workspaceRecord, null); setFullReview(false); render(client.model);
});
page.bind('enable-preview', () => enableMode('preview'));
page.bind('enable-full', () => enableMode('full'));
page.bind('review-full', () => { setFullReview(true); reviewStep = null; render(client.model); });
page.bind('not-now-preview', () => { dismissed = true; reviewStep = null; render(client.model); });
page.bind('full-secondary', () => { setFullReview(false); reviewStep = null; render(client.model); });
page.bind('restore-access', () => setupChoice?.stage === 'access' ? enableMode(setupChoice.mode) : restoreAccess(client.model));
page.bind('reopen-background-tab', () => send({ type: OBSERVER_REOPEN_TYPE }));
page.bind('setup-transfer-switch', () => openCreatorAccount());
page.bind('pause', () => transition('pause', client.model));
page.bind('journey-primary', () => runJourneyAction(element('journey-primary').dataset.action));
page.bind('journey-secondary', () => runJourneyAction(element('journey-secondary').dataset.action));
page.bind('pair-companion', () => client.post('pair'));
page.bind('cancel-pairing', () => client.post('cancel'));
page.bind('open-pairing-desktop', () => chrome.tabs.create({ url: client.model.config.history_settings_url }));
page.bind('open-options', () => openSurface('options'));
page.bind('retry-runtime', () => client.sync());
page.bind('back-current', () => { reviewStep = null; setFullReview(false); render(client.model); });
for (const step of steps) page.bind(`step-${step}`, () => {
  reviewStep = ['agree', 'mode'].includes(step) ? step : null;
  render(client.model);
});
for (const heading of document.querySelectorAll('h2')) heading.setAttribute('tabindex', '-1');
const receivingSetup = bindSetupTransfer({ document, send });
void chrome.storage.session.get(['onboarding_code_entry_v1']).then(async (values) => {
  if (location.hash === `#journey=${values.onboarding_code_entry_v1}`) {
    receivingSetup?.open();
    await chrome.storage.session.remove('onboarding_code_entry_v1');
  }
}).catch(() => undefined);
void client.start();

window.addEventListener('hashchange', () => {
  if (!['#full', '#desktop'].includes(location.hash) || ['pairing', 'compare'].includes(client.model.pairing.state)) return;
  if (location.hash === '#desktop') handoff = true;
  setFullReview(true); reviewStep = null; dismissed = false; render(client.model);
});
