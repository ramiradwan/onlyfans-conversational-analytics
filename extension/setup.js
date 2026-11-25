// Extension-owned setup presentation. Existing runtime controllers retain authority.
import { OBSERVER_REOPEN_TYPE } from './runtime/document-observer.mjs';
import { WORKSPACE_MESSAGE_TYPE } from './runtime/onboarding-entry.mjs';
import { WORKSPACE_STATE_KEY } from './runtime/onboarding-workspace.mjs';
import { FULL_REVIEW_INTENT_KEY, createFullReviewIntentConsumer, createFullReviewPersistence } from './runtime/onboarding-full-intent.mjs';
import { LEGAL_ACCEPT_TERMS_MESSAGE_TYPE, LEGAL_ACKNOWLEDGE_RISK_MESSAGE_TYPE,
  LEGAL_ACTIVATE_SOFTWARE_MESSAGE_TYPE, LEGAL_CHOOSE_MODE_MESSAGE_TYPE } from './runtime/legal-activation-controller.mjs';
import { createSurfaceClient, openSurface, send, secureExternalUrl, NoticeError } from './ui/surface-client.mjs';
import { supportChannelLabels } from './ui/support-channels.mjs';
import { customerJourney, hasModeAccess, manualPairingAction, modeChoiceAvailable, needsAgreement } from './ui/presentation.mjs';
import { element, show, text, renderLoading, renderJourney, renderLegalLinks, createPageActions } from './ui/dom.mjs';
import { requestAnalyticsAccess, transition, restoreAccess, openCreatorAccount } from './ui/actions.mjs';
import { createDesktopLaunch } from './ui/desktop-launch.mjs';
import { BROWSER_TASK_ENTRY_KEY, BROWSER_TASK_ADOPTION_KEY } from './runtime/browser-task-entry.mjs';
import { createSetupChoice } from './ui/setup-choice.mjs';
import { previewCounts } from './ui/preview-counts.mjs';
import { setupRequirement } from './ui/setup-requirement.mjs';

let failed = false;
let dismissed = false;
let revokedChoice = false;
let fullReviewRequested = false;
let browserTask = null, browserTaskExpiry = null, desktopAdoption = false;
let currentTask = null, taskHeading = null, taskLifetime = 0;
let presentationStarted = false, workspacePending = location.hash.startsWith('#journey=');
const fullReviewPersistence = createFullReviewPersistence();
const client = createSurfaceClient((model) => { if (model.status) failed = false; render(model); }, () => { failed = true; render(client.model); });
const page = createPageActions(client, render, {
  beforeAction: () => ({ task: currentTask, lifetime: taskLifetime }),
  async afterAction(previous, focused) {
    if (previous.lifetime !== taskLifetime || previous.task === currentTask || currentTask === null) return true;
    const next = currentTask;
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    if (next !== currentTask || previous.lifetime !== taskLifetime) return true;
    const active = document.activeElement;
    if (active !== focused && active !== document.body && active?.getClientRects().length) return true;
    document.querySelector('.onboarding-layout').scrollIntoView({ block: 'start', behavior: 'instant' });
    focusTask();
    return true;
  },
});
let currentJourney = null;
let previewValueShown = false;
let workspaceRecord = null;
const choicePersistence = createSetupChoice();
let setupChoice = null;
const desktopLaunch = createDesktopLaunch({ workspace: () => workspaceRecord,
  prepare: () => send({ type: WORKSPACE_MESSAGE_TYPE, action: 'prepare_launch' }),
  dispatch: (url) => window.location.assign(url),
  changed: () => render(client.model),
});
window.addEventListener('pagehide', () => {
  taskLifetime++;
  clearTimeout(browserTaskExpiry);
  desktopLaunch.stop();
});
const acknowledgments = [
  { id: 'terms-accepted', status: 'terms-recorded', field: 'terms_event_id', type: LEGAL_ACCEPT_TERMS_MESSAGE_TYPE },
  { id: 'risk-acknowledged', status: 'risk-recorded', field: 'risk_event_id', type: LEGAL_ACKNOWLEDGE_RISK_MESSAGE_TYPE },
];
const recording = new Set();
const VALIDATION_FAILURE = 'The setup check didn’t finish.';
let validationPending = null, validationFailed = null;
let receivingRead = 0;
const consumeFullReviewIntent = createFullReviewIntentConsumer({ apply: () => saveReviewChoice('full') });
async function readWorkspace() {
  if (!location.hash.startsWith('#journey=')) return;
  const generation = ++receivingRead;
  const lifetime = taskLifetime;
  workspacePending = true;
  for (const { id } of acknowledgments) element(id).setAttribute('aria-disabled', 'true');
  page.lock('enable-preview', true);
  page.lock('enable-full', true);
  workspaceRecord = null;
  browserTask = null; desktopAdoption = false; clearTimeout(browserTaskExpiry);
  setupChoice = null;
  fullReviewRequested = false;
  try {
    const next = await send({ type: WORKSPACE_MESSAGE_TYPE, action: 'read' });
    const intent = (await chrome.storage.session.get([FULL_REVIEW_INTENT_KEY]))[FULL_REVIEW_INTENT_KEY];
    if (generation !== receivingRead || lifetime !== taskLifetime) return;
    workspaceRecord = next;
    setupChoice = choicePersistence.read(workspaceRecord);
    fullReviewRequested = fullReviewPersistence.read(workspaceRecord) || workspaceRecord.draft.full_checked;
    consumeFullReviewIntent(intent, workspaceRecord);
    const [task, adoption] = await Promise.all([
      send({ type: WORKSPACE_MESSAGE_TYPE, action: 'browser_task' }),
      send({ type: WORKSPACE_MESSAGE_TYPE, action: 'desktop_adoption' }),
    ]);
    if (generation !== receivingRead || lifetime !== taskLifetime) return;
    browserTask = task;
    desktopAdoption = adoption?.adopted === true;
    if (browserTask) browserTaskExpiry = setTimeout(() => {
      browserTask = null; render(client.model);
    }, Math.max(0, browserTask.task.expires_at * 1000 - Date.now()));
    render(client.model);
  } catch {}
  finally {
    if (generation === receivingRead && lifetime === taskLifetime) { workspacePending = false; render(client.model); }
  }
}
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'session' && changes[FULL_REVIEW_INTENT_KEY]?.newValue) {
    void readWorkspace(); return;
  }
  if (area === 'session' && changes[BROWSER_TASK_ENTRY_KEY]
    || area === 'local' && changes[BROWSER_TASK_ADOPTION_KEY]) { void readWorkspace(); return; }
  if (area !== 'local' || !changes[WORKSPACE_STATE_KEY]) return;
  const next = changes[WORKSPACE_STATE_KEY].newValue?.record;
  if (!workspaceRecord || next?.journey_id !== workspaceRecord.journey_id
    || next?.draft_scope?.scope_id !== workspaceRecord.draft_scope.scope_id
    || next?.draft_scope?.disclosure_bundle_id !== workspaceRecord.draft_scope.disclosure_bundle_id) void readWorkspace();
});
const initialWorkspace = readWorkspace();

async function validateReview(transactionId) {
  validationPending = transactionId;
  validationFailed = null;
  try {
    await send({ type: LEGAL_ACTIVATE_SOFTWARE_MESSAGE_TYPE, transactionId });
  } catch {
    // Read status even if the response was lost.
  } finally {
    await client.sync();
    if (client.model.legal?.flow.stage !== 'mode_selection') validationFailed = transactionId;
    validationPending = null;
    render(client.model);
  }
}

function renderModeReview(model, mode) {
  const { legal: { flow }, status } = model;
  const transactionId = flow.transaction_id;
  const acknowledged = acknowledgments.every(({ field }) => Boolean(flow[field]));
  const ready = acknowledged && flow.stage === 'mode_selection';
  const full = mode === 'full';
  text('activation-title', full ? 'Turn on Full analytics' : 'Turn on Preview');
  show('preview-disclosure', !full);
  show('full-disclosure', full);
  show('enable-preview', !full);
  show('enable-full', full);
  show('not-now-preview', !full);
  show('full-secondary', full);
  show('review-full', !full);
  text('full-secondary', status.consent.mode === 'preview' ? 'Keep Preview' : 'Not now');
  show('mode-permission-note', !hasModeAccess(status, mode));
  const validationRejected = !ready && acknowledged && validationFailed === transactionId;
  show('retry-validation', validationRejected);
  if (validationRejected && !element('feedback').textContent) page.feedback(VALIDATION_FAILURE, true);
  if (!validationRejected && element('feedback').textContent === VALIDATION_FAILURE) page.feedback('');
  page.lock('enable-preview', !ready || workspacePending);
  page.lock('enable-full', !ready || workspacePending);
  for (const { id, field, status: statusId } of acknowledgments) {
    const recorded = Boolean(flow[field]);
    const saving = recording.has(id);
    const checkbox = element(id);
    checkbox.checked = recorded || saving;
    checkbox.setAttribute('aria-disabled', String(recorded || saving || workspacePending));
    text(statusId, recorded ? 'Recorded' : saving ? 'Saving…' : '');
  }
  if (acknowledged && !ready && !workspacePending && !validationPending && validationFailed !== transactionId) void validateReview(transactionId);
}

// This is not the draft value: automatic helper navigation is never an
// explicit request for a commercial offer.
async function recordFullChoice(requested) {
  if (!workspaceRecord) return;
  await send({ type: WORKSPACE_MESSAGE_TYPE, action: 'full_presentation_intent',
    scope_id: workspaceRecord.draft_scope.scope_id, requested });
}
function setFullReview(value) {
  fullReviewRequested = value;
  fullReviewPersistence.write(workspaceRecord, value);
  if (workspaceRecord && workspaceRecord.draft.full_checked !== value) {
    workspaceRecord.draft.full_checked = value;
    void send({ type: WORKSPACE_MESSAGE_TYPE, action: 'draft', scope_id: workspaceRecord.draft_scope.scope_id,
      draft: { ...workspaceRecord.draft } }).catch(() => undefined);
  }
}
function saveReviewChoice(mode) {
  dismissed = false;
  setupChoice = { mode, stage: 'review' };
  choicePersistence.write(workspaceRecord, setupChoice);
  setFullReview(mode === 'full');
}
async function selectModeReview(mode) {
  if (workspacePending) return;
  const generation = receivingRead, lifetime = taskLifetime;
  await recordFullChoice(mode === 'full');
  if (generation !== receivingRead || lifetime !== taskLifetime) return;
  saveReviewChoice(mode);
  render(client.model);
}
function standalonePairingAction(model) {
  if (browserTask || workspacePending || location.hash.startsWith('#journey=')) return null;
  return manualPairingAction(model);
}

function renderDesktop(model, journey) {
  const launch = desktopLaunch.state;
  const opening = launch === 'opening';
  const manualAction = standalonePairingAction(model);
  const comparison = model.pairing?.state === 'compare' && model.pairing.owns_attempt && model.pairing.comparison_code;
  const compare = journey?.id === 'pairing_in_progress' && comparison;
  const passiveConnection = journey?.id === 'setup_incomplete' && model.pairing?.state === 'unavailable';
  const launchUnconfirmed = launch === 'unconfirmed' && (!journey || journey.id === 'desktop_app_needed');
  const fullReady = journey?.id === 'full_ready';
  const openOnlyFans = journey?.primaryAction === 'open_creator_account';
  const status = passiveConnection ? '' : opening ? 'Waiting for the desktop app…'
    : launch === 'adopted' && !journey ? 'The desktop app has this browser’s setup request. Continue there.'
      : launchUnconfirmed ? 'Desktop connection not confirmed' : '';
  const tone = journey?.tone ?? 'info';
  const icon = compare ? 'link' : fullReady || journey?.id === 'activation_required' ? 'insights'
    : openOnlyFans ? journey.id === 'full_unavailable' ? 'pause' : 'browser'
      : journey?.id === 'activation_unavailable' || passiveConnection && tone === 'warning' ? 'offline'
        : ({ progress: opening && status ? 'app' : 'pending', success: 'done', error: 'error', warning: 'attention' })[tone] ?? 'app';
  element('desktop-entry').dataset.tone = tone;
  element('desktop-entry').dataset.journeyState = journey?.id ?? 'extension.full_handoff';
  element('desktop-entry').dataset.launchState = launch;
  element('desktop-entry').dataset.composition = compare ? 'decision' : 'guided';
  element('desktop-icon-mark').dataset.icon = icon;
  text('desktop-entry-title', journey?.title ?? 'Full analytics uses the desktop app');
  text('desktop-entry-body', manualAction === 'pair'
    ? 'Open code comparison in desktop settings first.'
    : journey?.body ?? 'Your conversation insights are stored on this computer. Open the desktop app to continue setup.');
  text('desktop-launch-message', status);
  element('desktop-launch-mark').dataset.icon = opening ? 'pending' : launchUnconfirmed ? 'attention' : 'done';
  show('desktop-launch-status', Boolean(status));
  const desktopResources = !passiveConnection && !fullReady && !openOnlyFans && !compare && !model.desktopRuntimeReachable;
  show('desktop-setup-code', desktopResources && !manualAction && !model.pairing?.desktop_control);
  text('desktop-comparison-code', compare ? `${comparison.slice(0, 3)} ${comparison.slice(3)}` : '');
  show('desktop-comparison-code', Boolean(compare));
  const desktopLabel = fullReady || openOnlyFans ? journey.primaryLabel : 'Open desktop app';
  const manualLabels = { pair: 'Compare connection codes', cancel: 'Cancel comparison' };
  element('open-desktop').dataset.action = manualAction ?? (openOnlyFans ? 'open_creator_account' : 'open_desktop');
  text('open-desktop', manualLabels[manualAction] ?? desktopLabel);
  show('open-desktop', !passiveConnection);
  page.lock('open-desktop', opening || (!manualAction && !workspaceRecord));
  show('download-desktop', desktopResources && secureExternalUrl(model.config.desktop_app_download_url) !== null);
}
function renderRuntimeHelp(channels, eligible) {
  const values = Object.entries(channels ?? {});
  if (!values.length) return;
  const slot = element('runtime-unavailable');
  const current = slot.querySelector('[data-support-help]');
  if (!eligible) {
    current?.remove();
    return;
  }
  if (current) return;
  const help = element('runtime-help-template').content.cloneNode(true);
  const list = help.querySelector('ul');
  for (const [channel, address] of values) {
    const row = document.createElement('li');
    const value = document.createElement('span');
    value.textContent = address;
    row.append(supportChannelLabels[channel] + ' ', value);
    list.append(row);
  }
  slot.append(help);
}

function render(model) {
  desktopLaunch.observe(desktopAdoption);
  const { status, legal } = model;
  const waitingForWorkspace = !presentationStarted && workspacePending;
  if (!presentationStarted && ((!waitingForWorkspace && status && legal) || failed)) {
    presentationStarted = true;
    document.querySelector('main').dataset.presentationReady = 'true';
  }
  for (const id of ['start-choice', 'desktop-entry', 'mode-choice', 'access-card', 'journey-card', 'legal-unavailable']) show(id, false);
  renderLoading(status && legal && !waitingForWorkspace ? status : null, failed, model.connectionRecovery, { setup: true });
  renderLegalLinks(legal);
  renderRuntimeHelp(model.config.support_channels,
    failed && !waitingForWorkspace && (!status || !legal) && model.connectionRecovery === 'exhausted');
  if (!status || !legal || waitingForWorkspace) {
    currentTask = failed ? 'unavailable' : null;
    if (currentTask) document.querySelector('main').dataset.task = currentTask;
    else delete document.querySelector('main').dataset.task;
    if (failed) document.querySelector('main').dataset.requirement = 'runtime-status';
    else delete document.querySelector('main').dataset.requirement;
    taskHeading = failed ? 'runtime-title' : null;
    delete document.querySelector('main').dataset.ready;
    return;
  }
  document.querySelector('main').dataset.ready = 'true';
  if (!legal.configured) {
    currentTask = 'legal-unavailable'; taskHeading = null;
    document.querySelector('main').dataset.task = currentTask;
    document.querySelector('main').dataset.requirement = 'legal-information';
    show('legal-unavailable', true); text('step-summary', 'Setup unavailable'); return;
  }
  const mode = status.consent.mode;
  const agreement = needsAgreement(model);
  const revoked = mode === 'revoked' || status.phase === 'revoked';
  if (!revoked) revokedChoice = false;
  const choose = (modeChoiceAvailable(model) && !dismissed && (!revoked || revokedChoice)) || (mode === 'preview' && fullReviewRequested)
    || Boolean(browserTask) && mode !== 'full' && !(mode === 'paused' && status.consent.resume_mode === 'full');
  const access = status.phase === 'permission_required' && !revoked;
  const selected = browserTask ? 'full' : setupChoice?.mode ?? (fullReviewRequested ? 'full' : legal.requires_reauthorization
    ? status.consent.resume_mode ?? (['preview', 'full'].includes(mode) ? mode : null) : null);
  const full = (!revoked || revokedChoice || Boolean(browserTask)) && (selected === 'full' || mode === 'full' || mode === 'paused' && status.consent.resume_mode === 'full');
  const reviewMode = selected ?? 'preview';
  const view = full && !browserTask ? 'desktop'
    : !dismissed && (choose || selected && agreement) ? 'consent'
      : agreement && (!dismissed || mode === 'off') ? 'start' : access ? 'access' : 'journey';
  if (view !== 'consent' && element('feedback').textContent === VALIDATION_FAILURE) page.feedback('');
  currentJourney = customerJourney(model, { setup: true });
  const browserAccessReady = full && browserTask && view === 'journey';
  currentTask = browserAccessReady ? 'browser-access-ready' : view === 'journey' ? `journey:${currentJourney.id}`
    : view === 'consent' ? `consent:${reviewMode}` : view;
  document.querySelector('main').dataset.task = currentTask;
  document.querySelector('main').dataset.requirement = browserAccessReady ? 'browser-access-ready' : setupRequirement({ model, view, selected,
    journey: currentJourney });
  taskHeading = view === 'desktop' ? 'desktop-entry-title' : view === 'start' ? 'start-title' : view === 'consent' ? 'activation-title'
    : view === 'access' ? 'access-title' : 'journey-title';
  const icon = ['paused', 'analytics_off'].includes(currentJourney.id) ? 'pause' :
    ({ success: 'done', progress: 'pending', warning: 'attention', error: 'error' })[currentJourney.tone] ?? 'info';
  element('journey-icon-mark').dataset.icon = icon;
  document.querySelector('main').dataset.step = view;
  const summary = view === 'desktop' ? 'Continue in the desktop app'
    : view === 'start' ? 'Choose Preview or Full analytics' : view === 'consent' ? 'Review data handling and separate choices'
      : view === 'access' ? 'Allow site access' : full ? 'Browser access' : currentJourney.title;
  if (element('step-summary').textContent !== summary) text('step-summary', summary);
  show('desktop-entry', view === 'desktop');
  const fullWorkspace = mode === 'full' || mode === 'paused' && status.consent.resume_mode === 'full';
  const desktopJourney = fullWorkspace && !legal.requires_reauthorization ? currentJourney : null;
  renderDesktop(model, desktopJourney);
  show('desktop-back', mode !== 'full' && status.consent.resume_mode !== 'full');
  text('desktop-back', mode === 'preview' ? 'Keep Preview' : 'Back');
  show('mode-choice', view === 'consent');
  if (view === 'consent') renderModeReview(model, reviewMode);
  show('start-choice', view === 'start');
  show('access-card', view === 'access'); show('journey-card', view === 'journey');
  text('access-title', 'Site access needed');
  text('access-body', 'Allow site access to receive activity from your creator account.');
  element('access-icon-mark').dataset.icon = 'attention';
  show('restore-access', status.phase === 'permission_required');
  renderJourney(currentJourney, model);
  const preview = mode === 'preview' || (mode === 'paused' && status.consent.resume_mode === 'preview');
  const stopped = mode !== 'paused' && status.observer?.helper === 'closed' && status.observer?.attachment !== 'ready';
  const counting = status.observer?.attachment === 'ready' && !status.reload_required;
  const previewValue = currentJourney.id === 'preview_available' && view === 'journey';
  element('journey-card').dataset.composition = previewValue ? 'value' : 'guided';
  show('preview-metrics', preview && view === 'journey');
  show('pause', mode === 'preview' && view === 'journey');
  show('background-tab-note', mode !== 'paused' && status.observer?.helper === 'open');
  show('reopen-background-tab', stopped);
  const daily = previewCounts(status.preview);
  text('metrics-range', daily.label);
  for (const [id, key] of [['messages-count', 'message_observations'], ['inbound-count', 'inbound_observations'], ['outbound-count', 'outbound_observations']]) text(id, new Intl.NumberFormat().format(daily.counts[key] ?? 0));
  if (['analytics_off', 'paused'].includes(currentJourney.id)) {
    text('journey-badge', '');
  }
  if (currentJourney.id === 'preview_available') {
    const ready = counting || stopped;
    element('journey-icon-mark').dataset.icon = ready ? 'insights' : 'pending';
    text('journey-title', ready ? currentJourney.title : 'Waiting for messages');
    text('journey-badge', stopped ? 'Updates stopped' : counting ? 'Counting messages' : '');
    text('journey-body', ready ? currentJourney.body : '');
  }
  element('journey-badge').dataset.tone = previewValue && counting ? 'success' : 'neutral';
  if (previewValue && counting && !previewValueShown) {
    previewValueShown = true;
    element('preview-metrics').dataset.arrive = 'true';
  } else if (!previewValue) delete element('preview-metrics').dataset.arrive;
  for (const [id, action, label] of [['journey-primary', currentJourney.primaryAction,
    currentJourney.primaryAction === 'resume' ? 'Resume' : currentJourney.primaryLabel],
    ['journey-secondary', currentJourney.secondaryAction, currentJourney.secondaryLabel]]) {
    element(id).dataset.action = action ?? ''; text(id, label ?? '');
    show(id, Boolean(action) && !['pair', 'cancel_pairing'].includes(action));
  }
  if (browserAccessReady) {
    text('journey-title', 'Browser access is ready'); text('journey-badge', '');
    text('journey-body', 'Continue in the desktop app.'); element('journey-icon-mark').dataset.icon = 'done';
    text('journey-primary', 'Open desktop app'); show('journey-primary', true);
    element('journey-primary').dataset.action = 'open_desktop'; show('journey-secondary', false);
    show('background-tab-note', false); show('reopen-background-tab', false);
  }
  show('review-notice', legal.requires_reauthorization === true);
}
function focusTask() {
  const heading = taskHeading ? element(taskHeading) : document.querySelector('#legal-unavailable h2');
  if (heading?.getClientRects().length) {
    heading.setAttribute('tabindex', '-1'); heading.focus({ preventScroll: true }); return true;
  }
  return false;
}
async function runJourneyAction(action) {
  if (action === 'review_full') return selectModeReview('full');
  if (action === 'choose_mode') {
    dismissed = false;
    revokedChoice = client.model.status.consent.mode === 'revoked';
    await recordFullChoice(false);
    setFullReview(false); render(client.model); return;
  }
  if (action === 'pair' || action === 'cancel') {
    if (standalonePairingAction(client.model) === action) client.post(action);
    return;
  }
  if (action === 'open_desktop') return desktopLaunch.launch(client.model);
  if (action === 'open_creator_account') return openCreatorAccount();
  if (action === 'restore_access') return restoreAccess(client.model);
  if (action === 'resume') return transition('resume', client.model);
  if (action === 'install_desktop') {
    const url = secureExternalUrl(client.model.config.desktop_app_download_url);
    if (!url) throw new NoticeError('The desktop download is unavailable in this version.');
    return chrome.tabs.create({ url });
  }
}
async function enableMode(mode) {
  if (workspacePending) return;
  const { legal, status } = client.model;
  const transactionId = legal.flow.transaction_id;
  if (legal.flow.stage !== 'mode_selection' || !acknowledgments.every(({ field }) => legal.flow[field])) return;
  const occurredAt = new Date().toISOString();
  const generation = receivingRead, lifetime = taskLifetime, task = browserTask;
  const assertReviewCurrent = () => {
    if (generation !== receivingRead || lifetime !== taskLifetime || workspacePending
      || transactionId !== client.model.legal?.flow.transaction_id) {
      throw new NoticeError('Setup changed. Review the current choices before continuing.');
    }
  };
  if (!hasModeAccess(status, mode)) await requestAnalyticsAccess(mode);
  assertReviewCurrent();
  if (mode === 'preview') await recordFullChoice(false);
  else {
    const admitted = await send({ type: WORKSPACE_MESSAGE_TYPE, action: 'browser_task' });
    if (!task || !admitted || admitted.task.journey_id !== task.task.journey_id
      || admitted.draft_scope.scope_id !== task.draft_scope.scope_id
      || admitted.draft_scope.disclosure_bundle_id !== task.draft_scope.disclosure_bundle_id
      || admitted.task.task_id !== task.task.task_id
      || admitted.task.admission_generation !== task.task.admission_generation) {
      throw new NoticeError('Open the browser setup step again to continue.');
    }
  }
  assertReviewCurrent();
  try {
    await send({ type: LEGAL_CHOOSE_MODE_MESSAGE_TYPE, mode, transactionId, occurredAt,
      consentEpoch: status.consent.consent_epoch });
  } catch (error) {
    await client.sync();
    const current = client.model;
    const receipt = current.status?.consent.authorization_event_id;
    if (!receipt || receipt === status.consent.authorization_event_id || current.status.consent.mode !== mode
      || current.legal?.flow.completed_event_id !== receipt
      || current.legal?.requires_reauthorization !== false) throw error;
  }
  setupChoice = null; choicePersistence.write(workspaceRecord, null);
  setFullReview(false); dismissed = false;
}
for (const { id, field, type } of acknowledgments) {
  const checkbox = element(id);
  checkbox.addEventListener('click', (event) => {
    if (workspacePending || checkbox.getAttribute('aria-disabled') === 'true') event.preventDefault();
  });
  checkbox.addEventListener('change', async () => {
    const flow = client.model.legal?.flow;
    if (workspacePending) {
      checkbox.checked = Boolean(flow?.[field]) || recording.has(id);
      return;
    }
    if (!checkbox.checked || !flow || flow[field] || recording.has(id)) return;
    const transactionId = flow.transaction_id;
    const occurredAt = new Date().toISOString();
    recording.add(id);
    page.feedback('');
    render(client.model);
    try { await send({ type, transactionId, occurredAt }); } catch {}
    await client.sync();
    recording.delete(id);
    const currentFlow = client.model.legal?.flow;
    if (currentFlow?.transaction_id === transactionId && !currentFlow[field]) {
      page.feedback('That choice could not be confirmed. Try again.', true);
    }
    render(client.model);
  });
}
page.bind('retry-validation', async () => {
  if (workspacePending || validationPending) return;
  await validateReview(client.model.legal.flow.transaction_id);
});
for (const mode of ['preview', 'full']) page.bind(`start-${mode}`, () => selectModeReview(mode));
page.bind('open-desktop', () => runJourneyAction(element('open-desktop').dataset.action));
page.bind('download-desktop', () => runJourneyAction('install_desktop'));
page.bind('desktop-back', async () => {
  await recordFullChoice(false);
  setupChoice = null; choicePersistence.write(workspaceRecord, null); setFullReview(false); render(client.model);
});
page.bind('enable-preview', () => enableMode('preview'));
page.bind('enable-full', () => enableMode('full'));
page.bind('review-full', () => selectModeReview('full'));
page.bind('not-now-preview', async () => {
  await recordFullChoice(false);
  setupChoice = null; choicePersistence.write(workspaceRecord, null);
  dismissed = true; revokedChoice = false; render(client.model);
});
page.bind('full-secondary', async () => {
  await recordFullChoice(false);
  setupChoice = null; choicePersistence.write(workspaceRecord, null);
  setFullReview(false);
  if (browserTask) await send({ type: WORKSPACE_MESSAGE_TYPE, action: 'dismiss_browser_task' });
  browserTask = null; clearTimeout(browserTaskExpiry);
  render(client.model);
});
page.bind('restore-access', () => restoreAccess(client.model));
page.bind('reopen-background-tab', () => send({ type: OBSERVER_REOPEN_TYPE }));
page.bind('pause', () => transition('pause', client.model));
page.bind('journey-primary', () => runJourneyAction(element('journey-primary').dataset.action));
page.bind('journey-secondary', () => runJourneyAction(element('journey-secondary').dataset.action));
page.bind('open-options', () => openSurface('options'));
element('preview-metrics').addEventListener('animationend', ({ animationName }) => {
  if (animationName === 'onboarding-arrive') delete element('preview-metrics').dataset.arrive;
});
for (const heading of document.querySelectorAll('h2')) heading.setAttribute('tabindex', '-1');
void Promise.all([initialWorkspace, client.start()]).catch(() => undefined);
