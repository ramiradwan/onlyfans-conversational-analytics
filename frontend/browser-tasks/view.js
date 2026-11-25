import { recoveryTone, setStatus } from '../shared-setup/presentation.js';
import { browserRecovery, recoveryClass } from '../shared-setup/recovery.js';
const journeyStates = { loading: 'loading', unavailable: 'unavailable', profile_mismatch: 'profile_mismatch',
  account_mismatch: 'account_mismatch', auth_idle: 'auth_idle', auth_opening: 'auth_opening', auth_checking: 'auth_checking',
  auth_unconfirmed: 'auth_unconfirmed', auth_access_required: 'auth_access_required', auth_closed: 'auth_closed',
  pairing_connecting: 'pairing_connecting', pairing_checking: 'pairing_checking',
  pairing_saved: 'pairing_saved',
  pairing_unconfirmed: 'pairing_unconfirmed', pairing_access_required: 'pairing_access_required',
  task_expired: 'task_expired', task_changed: 'task_changed', task_completed: 'task_completed', service_unavailable: 'service_unavailable',
  status_reconnecting: 'status_reconnecting', extension_disconnected: 'extension_disconnected',
  'needs-consent': 'needs_consent', 'needs-permission': 'needs_permission', 'needs-account': 'needs_account',
  ready: 'ready', paused: 'paused', reported: 'reported' };
const handoff = 'Continue in the desktop app';
const status = {
  loading: ['Loading browser task', ''],
  unavailable: ['Set up the extension', 'Install or turn on the extension in this browser profile.'],
  extension_disconnected: [handoff, 'Dipsy couldn’t connect to the extension.', 'desktop-handoff'],
  profile_mismatch: ['Switch browser profile', 'This isn’t the profile you used for setup.'],
  account_mismatch: ['Switch OnlyFans account', 'Sign in to the OnlyFans account you want to connect.'],
  auth_idle: ['Finish signing in', '', 'sign-in'],
  auth_opening: ['Signing in…', 'Finish in the sign-in window.', 'sign-in'],
  auth_checking: [null, 'Checking the sign-in outcome…', 'sign-in-check'],
  auth_closed: [null, 'Sign-in window closed. Continue in the desktop app.'],
  auth_unconfirmed: [null, 'Checking whether sign-in finished…', 'sign-in-check'],
  auth_access_required: [null, 'Browser access is required for this request.'],
  pairing_connecting: [null, 'Connecting this browser to the desktop app…'],
  pairing_checking: [null, 'Checking the browser connection…'],
  pairing_saved: ['Replace the saved connection?', 'This browser profile is already connected to a desktop app.'],
  pairing_unconfirmed: [null, 'The browser connection has not been confirmed.'],
  pairing_access_required: [null, 'Browser access is required to connect.'],
  task_expired: [handoff, 'This step expired.', 'desktop-handoff'],
  task_changed: [handoff, 'Setup moved on.', 'desktop-handoff'],
  task_completed: [handoff, 'This step is done.', 'desktop-handoff'],
  service_unavailable: [null, 'Dipsy can’t confirm this step. Continue setup in the desktop app.'],
  status_reconnecting: ['Waiting for the desktop app', 'This page updates by itself.'],
  'needs-consent': ['Review the extension', 'See what it collects before you turn it on.'],
  'needs-permission': ['Allow site access', 'The extension needs it to read activity from your creator account.'],
  'needs-account': ['Sign in to your creator account', 'Use your creator account in this browser profile.'],
  ready: ['Confirming browser access…', ''],
  paused: ['Message collection is paused', 'Letting the desktop app know…'],
  reported: ['Browser access confirmed', 'This tab closes by itself.'],
};

const serviceFailures = {
  invariant_failed: [handoff, 'This step couldn’t finish.', 'desktop-handoff'],
  authorization_refused: [handoff, 'This page can’t continue setup.', 'desktop-handoff'],
};
const extensionPrompts = ['needs-consent', 'needs-permission', 'needs-account'];
const quietStatuses = [...extensionPrompts, 'pairing_saved', 'task_expired', 'task_changed'];
const taskSpots = {
  unavailable: 'extension', extension_disconnected: 'app', 'needs-consent': 'extension', 'needs-permission': 'extension',
  'needs-account': 'account', account_mismatch: 'account', pairing_saved: 'link', auth_idle: 'account',
  auth_opening: 'account', paused: 'pause',
};
const desktopHandoffs = ['reported', 'task_completed', 'task_expired', 'task_changed', 'profile_mismatch',
  'account_mismatch', 'unavailable', 'extension_disconnected', 'service_unavailable', 'auth_checking', 'auth_closed',
  'auth_unconfirmed', 'auth_access_required', 'pairing_checking', 'pairing_unconfirmed', 'pairing_access_required'];

function replaceCard(card, family) {
  const next = card.cloneNode(false);
  next.dataset.taskFamily = family;
  for (const child of card.children) {
    if (!child.matches('.task-actions-slot')) next.append(child.cloneNode(true));
  }
  // These controls already have listeners from the browser-task entry point.
  next.append(card.querySelector('.task-actions-slot'));
  card.replaceWith(next);
  return next;
}

export function renderBrowserTask(document, value) {
  const get = id => document.getElementById(id);
  const payload = value.payload;
  const authentication = payload?.task.purpose === 'browser.authentication';
  const pairing = payload?.task.purpose === 'browser.pairing-confirmation';
  const state = Object.hasOwn(journeyStates, value.state) ? value.state : 'service_unavailable';
  const classification = value.recovery ?? recoveryClass(browserRecovery, value.state);
  const failure = state === 'service_unavailable' && Object.hasOwn(serviceFailures, classification);
  const [title, message, family = state] = failure ? serviceFailures[classification] : status[state];
  const actions = {
    'pairing-replace': pairing && state === 'pairing_saved',
    'extension-open': Boolean(payload) && !authentication && !pairing && extensionPrompts.includes(state),
    'authentication-open': authentication && state === 'auth_idle',
    'extension-install': Boolean(payload) && state === 'unavailable',
    'desktop-return': Boolean(payload) && desktopHandoffs.includes(state),
  };
  const main = document.querySelector('main');
  const previous = main.querySelector('.setup-task-card');
  const focused = document.activeElement;
  const hasAction = Object.values(actions).some(Boolean);
  const loading = state === 'loading';
  const compact = document.defaultView?.matchMedia?.('(min-width: 55em)').matches === false;
  const layout = compact ? state : family;
  const geometry = hasAction || loading || state === 'auth_opening' ? layout : layout + '-no-action';
  const card = previous.dataset.taskFamily === geometry ? previous : replaceCard(previous, geometry);
  main.dataset.journeyState = 'browser.' + journeyStates[state];
  if (loading) card.setAttribute('aria-busy', 'true');
  else card.removeAttribute('aria-busy');
  const header = card.querySelector('.browser-task-header');
  if (loading) header.setAttribute('aria-hidden', 'true');
  else header.removeAttribute('aria-hidden');
  const spot = taskSpots[state] ?? 'browser';
  for (const icon of header.querySelectorAll('[data-spot]')) {
    icon.toggleAttribute('hidden', icon.dataset.spot !== spot);
  }
  const heading = get('task-title-text');
  const nextTitle = title ?? (pairing ? 'Connect browser' : authentication ? 'Sign in' : 'Browser access');
  if (heading.textContent !== nextTitle) heading.textContent = nextTitle;

  card.querySelector('.setup-actions').hidden = !hasAction;
  for (const [id, visible] of Object.entries(actions)) get(id).hidden = !visible;
  get('extension-open').textContent = state === 'needs-account' ? 'Open creator account' : 'Open extension';
  const install = get('extension-install');
  const desktop = get('desktop-return');
  if (actions['extension-install']) install.href = 'https://chromewebstore.google.com/detail/' + payload.extension_id;
  else install.removeAttribute('href');
  if (actions['desktop-return']) desktop.href = 'ofca://onboarding/?journey=' + payload.task.journey_id;
  else desktop.removeAttribute('href');

  const text = !payload && state === 'service_unavailable' && !failure
    ? 'Dipsy can’t connect right now. Open the desktop app to continue setup.' : message;
  const tone = ['task_completed', 'reported'].includes(state) ? 'success'
    : ['auth_unconfirmed', 'paused'].includes(state) ? 'progress' : recoveryTone(classification);
  get('task-status').dataset.tone = text && !quietStatuses.includes(state) ? tone : 'none';
  setStatus(get('task-message'), text);
  if (card !== previous && actions[focused?.id]) {
    focused.focus({ preventScroll: true });
  }
}
