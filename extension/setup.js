// Extension-owned setup presentation. Existing runtime controllers retain authority.
import { OBSERVER_REOPEN_TYPE } from './runtime/document-observer.mjs';
import { WORKSPACE_MESSAGE_TYPE } from './runtime/onboarding-entry.mjs';
import { WORKSPACE_STATE_KEY } from './runtime/onboarding-workspace.mjs';
import { FULL_REVIEW_INTENT_KEY, createFullReviewIntentConsumer, createFullReviewPersistence } from './runtime/onboarding-full-intent.mjs';
import { LEGAL_ACCEPT_TERMS_MESSAGE_TYPE, LEGAL_ACKNOWLEDGE_RISK_MESSAGE_TYPE,
  LEGAL_ACTIVATE_SOFTWARE_MESSAGE_TYPE, LEGAL_CHOOSE_MODE_MESSAGE_TYPE } from './runtime/legal-activation-controller.mjs';
import { createSurfaceClient, openSurface, send, secureExternalUrl, NoticeError } from './ui/surface-client.mjs';
import { customerJourney, needsAgreement, modeChoiceAvailable } from './ui/presentation.mjs';
import { element, show, text, renderLoading, renderJourney, renderLegalLinks, createPageActions } from './ui/dom.mjs';
import { chooseMode, transition, restoreAccess, openCreatorAccount } from './ui/actions.mjs';
import { bindSetupTransfer, renderReceivingContext } from './ui/setup-transfer.mjs';
import { SETUP_TRANSFER_MESSAGE } from './runtime/setup-transfer.mjs';
import { createDesktopLaunch, desktopLaunchPresentation } from './ui/desktop-launch.mjs';
import { BROWSER_TASK_ENTRY_KEY, BROWSER_TASK_ADOPTION_KEY } from './runtime/browser-task-entry.mjs';
import { createSetupChoice } from './ui/setup-choice.mjs';
import { previewCounts } from './ui/preview-counts.mjs';
import { setupRequirement } from './ui/setup-requirement.mjs';

let failed = false;
let dismissed = false;
let fullReviewRequested = false;
let browserTask = null, browserTaskExpiry = null, desktopAdoption = false;
let currentTask = null, taskHeading = null, taskLifetime = 0;
let presentationStarted = false, workspacePending = location.hash.startsWith('#journey=');
let receivingSetup = null, receivingTask = null, receivingFocusPending = false;
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
let workspaceRecord = null;
const choicePersistence = createSetupChoice();
let setupChoice = null;
const desktopLaunch = createDesktopLaunch({ workspace: () => workspaceRecord,
  prepare: () => send({ type: WORKSPACE_MESSAGE_TYPE, action: 'prepare_launch' }),
  dispatch: (url) => window.location.assign(url),
  changed: () => render(client.model),
});
window.addEventListener('pagehide', () => {
  taskLifetime++; receivingFocusPending = false;
  clearTimeout(browserTaskExpiry);
  desktopLaunch.stop(); receivingSetup?.close({ notify: false, focus: false });
});
let checkboxDraft = { terms_checked: false, risk_checked: false, full_checked: false };
let receivingRead = 0, receivingExpiry = null;
const consumeFullReviewIntent = createFullReviewIntentConsumer({ apply() {
  dismissed = false; setFullReview(true);
  setupChoice = { mode: 'full', stage: 'review' }; choicePersistence.write(workspaceRecord, setupChoice);
} });
async function readWorkspace() {
  if (!location.hash.startsWith('#journey=')) return;
  const generation = ++receivingRead;
  const lifetime = taskLifetime;
  workspaceRecord = null;
  browserTask = null; desktopAdoption = false; clearTimeout(browserTaskExpiry);
  setupChoice = null;
  fullReviewRequested = false;
  checkboxDraft = { terms_checked: false, risk_checked: false, full_checked: false };
  clearTimeout(receivingExpiry);
  renderReceivingContext(document, null);
  try {
    const next = await send({ type: WORKSPACE_MESSAGE_TYPE, action: 'read' });
    const intent = (await chrome.storage.session.get([FULL_REVIEW_INTENT_KEY]))[FULL_REVIEW_INTENT_KEY];
    if (generation !== receivingRead || lifetime !== taskLifetime) return;
    workspaceRecord = next;
    setupChoice = choicePersistence.read(workspaceRecord);
    fullReviewRequested = fullReviewPersistence.read(workspaceRecord) || workspaceRecord.draft.full_checked;
    checkboxDraft = { ...workspaceRecord.draft };
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
    const receiving = await send({ type: SETUP_TRANSFER_MESSAGE, action: 'context' });
    if (generation !== receivingRead || lifetime !== taskLifetime) return;
    if (receiving?.expires_at > Date.now()) {
      renderReceivingContext(document, receiving);
      receivingExpiry = setTimeout(() => renderReceivingContext(document, null), receiving.expires_at - Date.now());
    }
    render(client.model);
  } catch {}
  finally {
    if (generation === receivingRead && lifetime === taskLifetime) { workspacePending = false; render(client.model); }
  }
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
  if (area === 'session' && changes[BROWSER_TASK_ENTRY_KEY]
    || area === 'local' && changes[BROWSER_TASK_ADOPTION_KEY]) { void readWorkspace(); return; }
  if (area !== 'local' || !changes[WORKSPACE_STATE_KEY]) return;
  const next = changes[WORKSPACE_STATE_KEY].newValue?.record;
  if (!workspaceRecord || next?.journey_id !== workspaceRecord.journey_id
    || next?.draft_scope?.scope_id !== workspaceRecord.draft_scope.scope_id
    || next?.draft_scope?.disclosure_bundle_id !== workspaceRecord.draft_scope.disclosure_bundle_id) void readWorkspace();
});
const initialWorkspace = readWorkspace();

function setFullReview(value) {
  fullReviewRequested = value;
  fullReviewPersistence.write(workspaceRecord, value);
  if (workspaceRecord && workspaceRecord.draft.full_checked !== value) {
    workspaceRecord.draft.full_checked = value;
    void send({ type: WORKSPACE_MESSAGE_TYPE, action: 'draft', scope_id: workspaceRecord.draft_scope.scope_id,
      draft: { ...workspaceRecord.draft } }).catch(() => undefined);
  }
}
function render(model) {
  desktopLaunch.observe(desktopAdoption);
  const { status, legal } = model;
  const waitingForWorkspace = !presentationStarted && workspacePending;
  if (!presentationStarted && ((!waitingForWorkspace && status && legal) || failed)) {
    presentationStarted = true;
    document.querySelector('main').dataset.presentationReady = 'true';
  }
  for (const id of ['start-choice', 'desktop-entry', 'pre-mode', 'mode-choice', 'access-card', 'journey-card', 'legal-unavailable']) show(id, false);
  renderLoading(status && legal && !waitingForWorkspace ? status : null, failed);
  renderLegalLinks(legal);
  if (!status || !legal || waitingForWorkspace) {
    currentTask = failed ? 'unavailable' : null;
    if (currentTask) document.querySelector('main').dataset.task = currentTask;
    else delete document.querySelector('main').dataset.task;
    if (failed) document.querySelector('main').dataset.requirement = 'runtime-status';
    else delete document.querySelector('main').dataset.requirement;
    taskHeading = failed ? 'runtime-title' : null;
    renderSetupTransfer();
    delete document.querySelector('main').dataset.ready;
    return;
  }
  document.querySelector('main').dataset.ready = 'true';
  if (!legal.configured) {
    currentTask = 'legal-unavailable'; taskHeading = null;
    document.querySelector('main').dataset.task = currentTask;
    document.querySelector('main').dataset.requirement = 'legal-information';
    show('legal-unavailable', true); text('step-summary', 'Setup unavailable'); renderSetupTransfer(); return;
  }
  const mode = status.consent.mode;
  const agreement = needsAgreement(model);
  // A new legal review supersedes an earlier optional mode-choice dismissal.
  if (agreement) dismissed = false;
  const active = ['preview', 'full'].includes(mode) && !legal.requires_reauthorization;
  const choose = (modeChoiceAvailable(model) && !dismissed) || (mode === 'preview' && fullReviewRequested)
    || Boolean(browserTask) && mode !== 'full' && !(mode === 'paused' && status.consent.resume_mode === 'full');
  const access = status.phase === 'permission_required';
  const selected = browserTask ? 'full' : setupChoice?.mode ?? (fullReviewRequested ? 'full' : legal.requires_reauthorization
    ? status.consent.resume_mode ?? (['preview', 'full'].includes(mode) ? mode : null) : null);
  const pendingAccess = selected && setupChoice?.stage === 'access' && !agreement
    && legal.flow.stage === 'mode_selection';
  const full = selected === 'full' || mode === 'full' || mode === 'paused' && status.consent.resume_mode === 'full';
  const view = full && !browserTask ? 'desktop'
    : agreement ? selected ? 'agree' : 'start'
      : pendingAccess ? 'access' : choose ? 'mode' : access ? 'access' : 'journey';
  currentJourney = customerJourney(model);
  const browserAccessReady = full && browserTask && view === 'journey';
  currentTask = browserAccessReady ? 'browser-access-ready' : view === 'journey' ? `journey:${currentJourney.id}`
    : ['agree', 'mode'].includes(view) ? `${view}:${selected ?? fullReviewRequested}` : view;
  document.querySelector('main').dataset.task = currentTask;
  document.querySelector('main').dataset.requirement = browserAccessReady ? 'browser-access-ready' : setupRequirement({ model, view, selected,
    reviewFull: fullReviewRequested, journey: currentJourney });
  taskHeading = view === 'desktop' ? 'desktop-entry-title' : view === 'start' ? 'start-title' : view === 'agree' ? 'activation-title'
    : view === 'access' ? 'access-title' : view === 'mode'
      ? fullReviewRequested || browserTask ? 'full-choice-title' : 'preview-choice-title'
      : 'journey-title';
  const icon = currentJourney.id === 'full_ready' || (currentJourney.id === 'preview_available' && status.observer?.attachment === 'ready') ? '✓'
    : currentJourney.id === 'paused' ? 'Ⅱ'
      : currentJourney.tone === 'progress' ? '↻' : currentJourney.tone === 'warning' || currentJourney.tone === 'error' ? '!' : '…';
  text('journey-icon', icon);
  document.querySelector('main').dataset.step = view;
  const summary = view === 'desktop' ? 'Continue in the desktop app'
    : view === 'start' ? 'Choose Preview or Full analytics' : view === 'agree' ? 'Review terms and risk disclosure'
      : view === 'mode' ? 'Choose a mode' : view === 'access' ? 'Allow site access' : full ? 'Browser access' : currentJourney.title;
  if (element('step-summary').textContent !== summary) text('step-summary', summary);
  show('desktop-entry', view === 'desktop');
  const desktop = desktopLaunchPresentation(desktopLaunch.state);
  text('desktop-entry-title', desktop.title); text('desktop-entry-body', desktop.body);
  page.lock('open-desktop', desktopLaunch.state === 'opening' || !workspaceRecord);
  show('download-desktop', secureExternalUrl(model.config.desktop_app_download_url) !== null);
  show('desktop-back', mode !== 'full' && status.consent.resume_mode !== 'full');
  text('desktop-back', mode === 'preview' ? 'Keep Preview' : 'Back');
  show('pre-mode', view === 'agree'); show('mode-choice', view === 'mode');
  show('start-choice', view === 'start');
  show('start-back', view === 'agree' && Boolean(setupChoice) && !browserTask && !legal.requires_reauthorization);
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
  const reviewFull = fullReviewRequested || Boolean(browserTask);
  show('preview-disclosure', view === 'mode' && !reviewFull);
  show('full-disclosure', view === 'mode' && reviewFull);
  show('enable-preview', !active); show('enable-full', mode !== 'full' || legal.requires_reauthorization);
  text('full-secondary', mode === 'preview' ? 'Keep Preview' : 'Not now');
  text('access-title', 'Allow site access');
  text('access-body', 'Allow the extension to read activity from your creator account.');
  show('restore-access', Boolean(pendingAccess) || status.phase === 'permission_required');
  renderJourney(currentJourney, model);
  const preview = mode === 'preview' || (mode === 'paused' && status.consent.resume_mode === 'preview');
  element('journey-card').classList.toggle('preview-journey', preview && view === 'journey');
  show('preview-metrics', preview && view === 'journey');
  show('pause', mode === 'preview' && view === 'journey');
  show('background-tab-note', mode !== 'paused' && status.observer?.helper === 'open');
  show('reopen-background-tab', mode !== 'paused' && status.observer?.helper === 'closed' && status.observer?.attachment !== 'ready');
  const daily = previewCounts(status.preview);
  text('metrics-range', daily.label);
  for (const [id, key] of [['messages-count', 'message_observations'], ['inbound-count', 'inbound_observations'], ['outbound-count', 'outbound_observations']]) text(id, new Intl.NumberFormat().format(daily.counts[key] ?? 0));
  if (currentJourney.id === 'preview_available') {
    text('journey-title', status.observer?.attachment === 'ready' ? 'Preview is ready' : 'Waiting for OnlyFans activity');
    text('journey-badge', status.observer?.attachment === 'ready' ? 'Counting as you use OnlyFans' : '');
    text('journey-body', '');
  }
  for (const [id, action, label] of [['journey-primary', currentJourney.primaryAction,
    currentJourney.primaryAction === 'resume' ? 'Resume' : currentJourney.primaryLabel],
    ['journey-secondary', currentJourney.secondaryAction, currentJourney.secondaryLabel]]) {
    element(id).dataset.action = action ?? ''; text(id, label ?? '');
    show(id, Boolean(action) && !['pair', 'cancel_pairing'].includes(action));
  }
  if (browserAccessReady) {
    text('journey-title', 'Browser access is ready'); text('journey-badge', '');
    text('journey-body', 'Continue in the desktop app.'); text('journey-icon', '✓');
    text('journey-primary', 'Open desktop app'); show('journey-primary', true);
    element('journey-primary').dataset.action = 'open_desktop'; show('journey-secondary', false);
    show('background-tab-note', false); show('reopen-background-tab', false);
  }
  show('review-notice', legal.requires_reauthorization === true);
  renderSetupTransfer();
}
function focusTask() {
  const heading = taskHeading ? element(taskHeading) : document.querySelector('#legal-unavailable h2');
  if (heading?.getClientRects().length) {
    heading.setAttribute('tabindex', '-1'); heading.focus({ preventScroll: true }); return true;
  }
  return false;
}
function renderSetupTransfer() {
  const entry = element('setup-transfer-entry');
  const obsolete = receivingSetup?.opened && ((receivingTask !== null && receivingTask !== currentTask)
    || !receivingSetup.direct);
  const restore = obsolete && entry.contains(document.activeElement);
  if (restore) receivingFocusPending = true;
  if (obsolete) receivingSetup.close({ notify: false, focus: false });
  const open = receivingSetup?.opened === true;
  if (open && receivingTask === null) receivingTask = currentTask;
  if (!open) receivingTask = null;
  show('setup-transfer-entry', open);
  for (const id of ['extension-stage', 'surface-feedback']) {
    document.querySelector(`[data-reserved-region="${id}"]`).classList.toggle('hidden', open);
  }
  if (receivingFocusPending && focusTask()) receivingFocusPending = false;
}
function runJourneyAction(action) {
  if (action === 'review_full' || action === 'choose_mode') {
    dismissed = false; setFullReview(action === 'review_full'); render(client.model); return;
  }
  if (action === 'open_desktop') return desktopLaunch.launch(client.model);
  if (action === 'open_creator_account') return openCreatorAccount();
  if (action === 'retry_readiness') return client.sync();
  if (action === 'restore_access') return restoreAccess(client.model);
  if (action === 'resume') return transition('resume', client.model);
  if (action === 'install_desktop') {
    const url = secureExternalUrl(client.model.config.desktop_app_download_url);
    if (!url) throw new NoticeError('The desktop download is unavailable in this version.');
    return chrome.tabs.create({ url });
  }
}
async function enableMode(mode) {
  await chooseMode(mode);
  setupChoice = null; choicePersistence.write(workspaceRecord, null);
  setFullReview(false); dismissed = false;
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
    const chosen = browserTask ? 'full' : setupChoice?.mode ?? (fullReviewRequested ? 'full' : client.model.status.consent.resume_mode ?? null);
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
page.bind('open-desktop', () => desktopLaunch.launch());
page.bind('download-desktop', () => runJourneyAction('install_desktop'));
page.bind('desktop-back', () => {
  setupChoice = null; choicePersistence.write(workspaceRecord, null); setFullReview(false); render(client.model);
});
page.bind('enable-preview', () => enableMode('preview'));
page.bind('enable-full', () => enableMode('full'));
page.bind('review-full', () => { setFullReview(true); render(client.model); });
page.bind('not-now-preview', () => { dismissed = true; render(client.model); });
page.bind('full-secondary', async () => {
  if (browserTask) await send({ type: WORKSPACE_MESSAGE_TYPE, action: 'dismiss_browser_task' });
  browserTask = null; clearTimeout(browserTaskExpiry);
  setupChoice = null; choicePersistence.write(workspaceRecord, null);
  setFullReview(false); render(client.model);
});
page.bind('restore-access', () => setupChoice?.stage === 'access' ? enableMode(setupChoice.mode) : restoreAccess(client.model));
page.bind('reopen-background-tab', () => send({ type: OBSERVER_REOPEN_TYPE }));
page.bind('setup-transfer-switch', () => openCreatorAccount());
page.bind('pause', () => transition('pause', client.model));
page.bind('journey-primary', () => runJourneyAction(element('journey-primary').dataset.action));
page.bind('journey-secondary', () => runJourneyAction(element('journey-secondary').dataset.action));
page.bind('open-options', () => openSurface('options'));
page.bind('retry-runtime', () => client.sync());
for (const heading of document.querySelectorAll('h2')) heading.setAttribute('tabindex', '-1');
receivingSetup = bindSetupTransfer({ document, send,
  changed: () => render(client.model), restoreFocus: () => {
    receivingFocusPending = !focusTask();
  } });
const initialLifetime = taskLifetime;
void Promise.all([initialWorkspace, client.start(), chrome.storage.session.get(['onboarding_code_entry_v1'])]).then(async ([, , values]) => {
  if (initialLifetime !== taskLifetime) return;
  if (location.hash === `#journey=${values.onboarding_code_entry_v1}`) {
    receivingSetup?.open({ direct: true });
    await chrome.storage.session.remove('onboarding_code_entry_v1');
  }
}).catch(() => undefined);
