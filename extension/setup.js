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
import { createDesktopLaunch } from './ui/desktop-launch.mjs';
import { BROWSER_TASK_ENTRY_KEY, BROWSER_TASK_ADOPTION_KEY } from './runtime/browser-task-entry.mjs';
import { createSetupChoice } from './ui/setup-choice.mjs';
import { previewCounts } from './ui/preview-counts.mjs';
import { setupRequirement } from './ui/setup-requirement.mjs';

// Keep the approved section bytes intact in HTML; focus identifiers are presentation state.
for (const mode of ['preview', 'full']) {
  const heading = document.querySelector(`#${mode}-disclosure .points h2`);
  heading.id = `${mode}-choice-title`;
  heading.tabIndex = -1;
}

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
let checkboxDraft = { terms_checked: false, risk_checked: false, full_checked: false };
let receivingRead = 0;
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
function renderDesktop(model, journey) {
  const launch = desktopLaunch.state;
  const opening = launch === 'opening';
  const unconfirmed = launch === 'unconfirmed';
  const adopted = launch === 'adopted';
  const comparison = model.pairing?.state === 'compare' && model.pairing.owns_attempt && model.pairing.comparison_code;
  const compare = journey?.id === 'pairing_in_progress' && comparison;
  const launchUnconfirmed = unconfirmed && (!journey || ['desktop_app_needed', 'desktop_app_unavailable'].includes(journey.id));
  const fullReady = journey?.id === 'full_ready';
  const openOnlyFans = journey?.primaryAction === 'open_creator_account';
  const status = opening ? 'Waiting for the desktop app…'
    : adopted && !journey ? 'The desktop app has this browser’s setup request. Continue there.' : '';
  const tone = launchUnconfirmed ? 'warning' : journey?.tone ?? (opening ? 'progress' : adopted ? 'success' : 'info');
  const icon = tone === 'progress' ? 'pending' : tone === 'success' ? 'done' : tone === 'error' ? 'error'
    : tone === 'warning' ? 'attention' : 'app';
  element('desktop-entry').dataset.tone = tone;
  element('desktop-entry').dataset.journeyState = launchUnconfirmed ? 'desktop_not_confirmed' : journey?.id ?? 'extension.full_handoff';
  element('desktop-entry').dataset.composition = compare ? 'decision' : 'guided';
  element('desktop-icon-mark').dataset.icon = compare ? 'link' : openOnlyFans ? 'browser' : icon;
  text('desktop-entry-title', launchUnconfirmed ? 'Desktop connection not confirmed' : journey?.title ?? 'Full analytics uses the desktop app');
  text('desktop-entry-body', launchUnconfirmed ? 'Open the desktop app to finish connecting.' : journey?.body
    ?? (status ? '' : 'Your conversation insights are stored on this computer. Open the desktop app to continue setup.'));
  text('desktop-launch-message', status);
  element('desktop-launch-mark').dataset.icon = opening ? 'pending' : 'done';
  show('desktop-launch-status', Boolean(status));
  show('desktop-setup-code', !fullReady && !openOnlyFans && !compare && !model.pairing?.desktop_control && !model.desktopRuntimeReachable);
  text('desktop-comparison-code', compare ? `${comparison.slice(0, 3)} ${comparison.slice(3)}` : '');
  show('desktop-comparison-code', Boolean(compare));
  element('open-desktop').dataset.action = openOnlyFans ? 'open_creator_account' : 'open_desktop';
  text('open-desktop', fullReady || openOnlyFans ? journey.primaryLabel : 'Open desktop app');
  page.lock('open-desktop', opening || !workspaceRecord);
  show('download-desktop', !fullReady && !openOnlyFans && !compare && !model.desktopRuntimeReachable
    && secureExternalUrl(model.config.desktop_app_download_url) !== null);
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
  renderLoading(status && legal && !waitingForWorkspace ? status : null, failed, model.connectionRecovery, { setup: true });
  renderLegalLinks(legal);
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
  // A new legal review supersedes an earlier optional mode-choice dismissal.
  if (agreement) dismissed = false;
  const active = ['preview', 'full'].includes(mode) && !legal.requires_reauthorization;
  const revoked = mode === 'revoked' || status.phase === 'revoked';
  if (!revoked) revokedChoice = false;
  const choose = (modeChoiceAvailable(model) && !dismissed && (!revoked || revokedChoice)) || (mode === 'preview' && fullReviewRequested)
    || Boolean(browserTask) && mode !== 'full' && !(mode === 'paused' && status.consent.resume_mode === 'full');
  const access = status.phase === 'permission_required' && !revoked;
  const selected = browserTask ? 'full' : setupChoice?.mode ?? (fullReviewRequested ? 'full' : legal.requires_reauthorization
    ? status.consent.resume_mode ?? (['preview', 'full'].includes(mode) ? mode : null) : null);
  const pendingAccess = selected && setupChoice?.stage === 'access' && !agreement
    && legal.flow.stage === 'mode_selection';
  const full = (!revoked || revokedChoice || Boolean(browserTask)) && (selected === 'full' || mode === 'full' || mode === 'paused' && status.consent.resume_mode === 'full');
  const view = full && !browserTask ? 'desktop'
    : agreement ? selected ? 'agree' : 'start'
      : pendingAccess ? 'access' : choose ? 'mode' : access ? 'access' : 'journey';
  currentJourney = customerJourney(model, { setup: true });
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
  const icon = ['paused', 'analytics_off'].includes(currentJourney.id) ? 'pause' :
    ({ success: 'done', progress: 'pending', warning: 'attention', error: 'error' })[currentJourney.tone] ?? 'info';
  element('journey-icon-mark').dataset.icon = icon;
  document.querySelector('main').dataset.step = view;
  const summary = view === 'desktop' ? 'Continue in the desktop app'
    : view === 'start' ? 'Choose Preview or Full analytics' : view === 'agree' ? 'Review terms and risk disclosure'
      : view === 'mode' ? 'Choose a mode' : view === 'access' ? 'Allow site access' : full ? 'Browser access' : currentJourney.title;
  if (element('step-summary').textContent !== summary) text('step-summary', summary);
  show('desktop-entry', view === 'desktop');
  const fullWorkspace = mode === 'full' || mode === 'paused' && status.consent.resume_mode === 'full';
  const desktopJourney = fullWorkspace && !legal.requires_reauthorization ? currentJourney : null;
  renderDesktop(model, desktopJourney);
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
      const copy = original.querySelector('.points').cloneNode(true);
      copy.querySelector('h2').removeAttribute('id');
      copy.querySelector('h2').removeAttribute('tabindex');
      const separateChoices = element('separate-choices-disclosure').cloneNode(true);
      separateChoices.removeAttribute('id');
      disclosure.replaceChildren(copy, separateChoices);
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
  if (view === 'mode') {
    element(reviewFull ? 'full-disclosure' : 'preview-disclosure').insertBefore(
      element('separate-choices-disclosure'), element(reviewFull ? 'enable-full' : 'enable-preview'));
  }
  show('enable-preview', !active); show('enable-full', mode !== 'full' || legal.requires_reauthorization);
  text('full-secondary', mode === 'preview' ? 'Keep Preview' : 'Not now');
  text('access-title', pendingAccess ? 'Allow site access' : 'Site access needed');
  text('access-body', pendingAccess ? 'Allow the extension to read activity from your OnlyFans account.'
    : 'Allow site access to receive activity from OnlyFans.');
  show('restore-access', Boolean(pendingAccess) || status.phase === 'permission_required');
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
  if (currentJourney.id === 'analytics_off') {
    text('journey-badge', '');
  }
  if (currentJourney.id === 'preview_available') {
    const ready = counting || stopped;
    element('journey-icon-mark').dataset.icon = ready ? 'insights' : 'pending';
    text('journey-title', ready ? currentJourney.title : 'Waiting for OnlyFans messages');
    text('journey-badge', stopped ? 'Updates stopped' : counting ? 'Counting as you use OnlyFans' : '');
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
  if (action === 'review_full' || action === 'choose_mode') {
    dismissed = false;
    revokedChoice = action === 'choose_mode' && client.model.status.consent.mode === 'revoked';
    if (action === 'review_full') await recordFullChoice(true);
    else await recordFullChoice(false);
    setFullReview(action === 'review_full'); render(client.model); return;
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
  if (mode === 'preview') await recordFullChoice(false);
  else if (!browserTask) await recordFullChoice(true);
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
for (const mode of ['preview', 'full']) page.bind(`start-${mode}`, async () => {
  await recordFullChoice(mode === 'full');
  setupChoice = { mode, stage: 'review' }; choicePersistence.write(workspaceRecord, setupChoice);
  setFullReview(mode === 'full'); render(client.model);
});
page.bind('start-back', async () => {
  await recordFullChoice(false);
  setupChoice = null; choicePersistence.write(workspaceRecord, null); setFullReview(false); render(client.model);
});
page.bind('open-desktop', () => runJourneyAction(element('open-desktop').dataset.action));
page.bind('download-desktop', () => runJourneyAction('install_desktop'));
page.bind('desktop-back', async () => {
  await recordFullChoice(false);
  setupChoice = null; choicePersistence.write(workspaceRecord, null); setFullReview(false); render(client.model);
});
page.bind('enable-preview', () => enableMode('preview'));
page.bind('enable-full', () => enableMode('full'));
page.bind('review-full', async () => { await recordFullChoice(true); setFullReview(true); render(client.model); });
page.bind('not-now-preview', async () => { await recordFullChoice(false); dismissed = true; revokedChoice = false; render(client.model); });
page.bind('full-secondary', async () => {
  await recordFullChoice(false);
  if (browserTask) await send({ type: WORKSPACE_MESSAGE_TYPE, action: 'dismiss_browser_task' });
  browserTask = null; clearTimeout(browserTaskExpiry);
  setupChoice = null; choicePersistence.write(workspaceRecord, null);
  setFullReview(false); render(client.model);
});
page.bind('restore-access', () => setupChoice?.stage === 'access' ? enableMode(setupChoice.mode) : restoreAccess(client.model));
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
