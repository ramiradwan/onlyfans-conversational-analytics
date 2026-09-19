// Extension-owned setup presentation. Existing runtime controllers retain authority.
import { UI_RELOAD_TABS_MESSAGE_TYPE } from './runtime/consent-controller.mjs';
import { LEGAL_ACCEPT_TERMS_MESSAGE_TYPE, LEGAL_ACKNOWLEDGE_RISK_MESSAGE_TYPE,
  LEGAL_ACTIVATE_SOFTWARE_MESSAGE_TYPE } from './runtime/legal-activation-controller.mjs';
import { createSurfaceClient, openSurface, send, secureExternalUrl, NoticeError } from './ui/surface-client.mjs';
import { customerJourney, needsAgreement, modeChoiceAvailable } from './ui/presentation.mjs';
import { element, show, text, renderLoading, renderJourney, renderReadiness, renderLegalLinks, createPageActions } from './ui/dom.mjs';
import { chooseMode, transition, restoreAccess } from './ui/actions.mjs';

let failed = false;
let dismissed = false;
let reviewStep = null;
let fullReviewRequested = location.hash === '#full';
try { fullReviewRequested ||= sessionStorage.getItem('full-review') === 'true'; } catch {}
const client = createSurfaceClient((model) => { if (model.status) failed = false; render(model); }, () => { failed = true; render(client.model); });
const page = createPageActions(client, render);
const steps = ['agree', 'mode', 'connect', 'activate'];
let currentJourney = null;

function setFullReview(value) {
  fullReviewRequested = value;
  try { if (value) sessionStorage.setItem('full-review', 'true'); else sessionStorage.removeItem('full-review'); } catch {}
}
function renderProgress(model, view, journey) {
  const full = model.status.consent.mode === 'full' || fullReviewRequested;
  const count = full ? 4 : 2;
  const connected = model.pairing.state === 'paired' && model.desktopRuntimeReachable
    && model.status.delivery?.transport_state === 'authenticated';
  const complete = view === 'journey' && ['preview_available', 'full_ready'].includes(journey.id);
  const index = view === 'agree' ? 0 : ['mode', 'access'].includes(view) ? 1 : connected ? 3 : 2;
  show('setup-progress', ['agree', 'mode', 'access'].includes(view) || ['preview', 'full'].includes(model.status.consent.mode));
  steps.forEach((step, position) => {
    const item = document.querySelector(`[data-step="${step}"]`);
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
  const { status, legal } = model;
  for (const id of ['pre-mode', 'mode-choice', 'access-card', 'journey-card', 'legal-unavailable', 'setup-progress', 'back-current']) show(id, false);
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
  const access = status.phase === 'permission_required' || status.reload_required === true;
  const view = agreement || reviewStep === 'agree' ? 'agree' : choose ? 'mode' : access ? 'access' : 'journey';
  currentJourney = customerJourney(model);
  document.querySelector('main').dataset.step = view;
  renderProgress(model, view, currentJourney);
  show('back-current', reviewStep !== null);
  show('pre-mode', view === 'agree'); show('mode-choice', view === 'mode');
  show('access-card', view === 'access'); show('journey-card', view === 'journey');
  element('terms-accepted').checked = Boolean(legal.flow.terms_event_id);
  element('risk-acknowledged').checked = Boolean(legal.flow.risk_event_id);
  page.lock('terms-accepted', Boolean(legal.flow.terms_event_id) || active);
  page.lock('risk-acknowledged', Boolean(legal.flow.risk_event_id) || active);
  page.lock('activate-software', !legal.flow.terms_event_id || !legal.flow.risk_event_id || active);
  show('activate-software', !active);
  const reviewFull = fullReviewRequested || (reviewStep === 'mode' && mode === 'full');
  show('preview-disclosure', view === 'mode' && !reviewFull);
  show('full-disclosure', view === 'mode' && reviewFull);
  show('enable-preview', !active); show('enable-full', mode !== 'full' || legal.requires_reauthorization);
  text('full-secondary', mode === 'preview' ? 'Keep Preview' : 'Not now');
  text('access-title', status.phase === 'permission_required' ? 'Allow site access' : 'Apply site access');
  text('access-body', status.phase === 'permission_required'
    ? 'Allow access to OnlyFans to restart the analytics mode you chose.'
    : 'Reload your open OnlyFans tabs to apply the access you allowed.');
  show('restore-access', status.phase === 'permission_required');
  show('reload-tabs', status.phase !== 'permission_required' && status.reload_required === true);
  renderJourney(currentJourney);
  if (currentJourney.id === 'preview_available') text('journey-body', 'Your daily counts are available in the extension. Preview does not need the desktop app.');
  if (currentJourney.id === 'full_ready') text('journey-body', 'Setup is finished. Insights and stored messages are in the desktop app.');
  renderPairing(model, currentJourney);
  renderReadiness(model); show('ready-details', currentJourney.id === 'full_ready');
  for (const [id, action, label] of [['journey-primary', currentJourney.primaryAction, currentJourney.primaryLabel],
    ['journey-secondary', currentJourney.secondaryAction, currentJourney.secondaryLabel]]) {
    element(id).dataset.action = action ?? ''; text(id, label ?? '');
    show(id, Boolean(action) && !['pair', 'cancel_pairing'].includes(action));
  }
  show('review-notice', legal.requires_reauthorization === true);
}
function renderPairing(model, journey) {
  const pending = ['pairing', 'compare'].includes(model.pairing.state);
  const pairAction = journey.primaryAction === 'pair' || journey.secondaryAction === 'pair';
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
    : 'This connection was started in another setup tab. Continue there to finish or cancel it.');
}
function runJourneyAction(action) {
  if (action === 'review_full' || action === 'choose_mode') {
    reviewStep = null; dismissed = false; setFullReview(action === 'review_full'); render(client.model); return;
  }
  if (action === 'pair') return client.post('pair');
  if (action === 'cancel_pairing') return client.post('cancel');
  if (action === 'open_dashboard') return chrome.tabs.create({ url: client.model.config.dashboard_url });
  if (action === 'open_desktop_settings') return chrome.tabs.create({ url: client.model.config.history_settings_url });
  if (action === 'open_creator_account') return chrome.tabs.create({ url: 'https://onlyfans.com/' });
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
  setFullReview(false); reviewStep = null; dismissed = false;
}
for (const [id, type] of [['terms-accepted', LEGAL_ACCEPT_TERMS_MESSAGE_TYPE], ['risk-acknowledged', LEGAL_ACKNOWLEDGE_RISK_MESSAGE_TYPE]]) {
  element(id).addEventListener('change', () => {
    if (element(id).checked) void page.run(() => send({ type }));
  });
}
page.bind('activate-software', () => send({ type: LEGAL_ACTIVATE_SOFTWARE_MESSAGE_TYPE }));
page.bind('enable-preview', () => enableMode('preview'));
page.bind('enable-full', () => enableMode('full'));
page.bind('review-full', () => { setFullReview(true); reviewStep = null; render(client.model); });
page.bind('not-now-preview', () => { dismissed = true; reviewStep = null; render(client.model); });
page.bind('full-secondary', () => { setFullReview(false); reviewStep = null; render(client.model); });
page.bind('restore-access', () => restoreAccess(client.model));
page.bind('reload-tabs', () => send({ type: UI_RELOAD_TABS_MESSAGE_TYPE }));
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
void client.start();

window.addEventListener('hashchange', () => {
  if (location.hash !== '#full' || ['pairing', 'compare'].includes(client.model.pairing.state)) return;
  setFullReview(true); reviewStep = null; dismissed = false; render(client.model);
});
