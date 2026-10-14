import { setStatus } from '../shared-setup/presentation.js';
const journeyStates = { loading: 'loading', unavailable: 'unavailable', profile_mismatch: 'profile_mismatch',
  account_mismatch: 'account_mismatch', auth_idle: 'auth_idle', auth_opening: 'auth_opening', auth_checking: 'auth_checking',
  auth_unconfirmed: 'auth_unconfirmed', auth_access_required: 'auth_access_required', auth_closed: 'auth_closed',
  pairing_connecting: 'pairing_connecting', pairing_checking: 'pairing_checking',
  pairing_saved: 'pairing_saved',
  pairing_unconfirmed: 'pairing_unconfirmed', pairing_access_required: 'pairing_access_required',
  task_expired: 'task_expired', task_changed: 'task_changed', task_completed: 'task_completed', service_unavailable: 'service_unavailable',
  'needs-consent': 'needs_consent', 'needs-permission': 'needs_permission', 'needs-account': 'needs_account',
  ready: 'ready', paused: 'paused', reported: 'reported' };
const status = {
  loading: 'Reading the current task…',
  unavailable: 'The extension is unavailable in this Chrome profile. Enable or install it, then retry.',
  profile_mismatch: 'This task belongs to another Chrome profile.',
  account_mismatch: 'Chrome is signed in to a different creator account. Open the intended account, then retry.',
  auth_idle: 'Continue signing in to your account.',
  auth_opening: 'Waiting for sign-in to finish.',
  auth_checking: 'Checking the sign-in outcome…',
  auth_closed: 'Sign-in window closed. Continue in the desktop app.',
  auth_unconfirmed: 'The sign-in outcome has not been confirmed.',
  auth_access_required: 'Browser access is required for this request.',
  pairing_connecting: 'Connecting this browser to the desktop app…',
  pairing_checking: 'Checking the browser connection…',
  pairing_saved: 'This Chrome profile has a saved desktop connection. Replace it to connect to this desktop app.',
  pairing_unconfirmed: 'The browser connection has not been confirmed.',
  pairing_access_required: 'Browser access is required to connect.',
  task_expired: 'This browser task expired.',
  task_changed: 'This browser task is no longer current.',
  task_completed: 'Browser task complete.',
  service_unavailable: 'Browser task status could not be read. Retry.',
  'needs-consent': 'Review browser access in the extension.',
  'needs-permission': 'Allow OnlyFans site access in the extension.',
  'needs-account': 'Open your creator account in Chrome.',
  ready: 'Reporting browser access…', paused: 'Reporting the paused browser connection…',
  reported: 'Browser access confirmed.',
};
export function renderBrowserTask(document, value) {
  const get = id => document.getElementById(id), payload = value.payload;
  const authentication = payload?.task.purpose === 'browser.authentication';
  const pairing = payload?.task.purpose === 'browser.pairing-confirmation';
  const state = Object.hasOwn(status, value.state) ? value.state : 'unavailable';
  document.querySelector('main').dataset.journeyState = 'browser.' + journeyStates[state];
  setStatus(get('task-status'), status[state]);
  get('pairing-replace').hidden = !pairing || state !== 'pairing_saved';
  get('task-title').textContent = pairing ? 'Connect browser' : authentication ? 'Sign in' : 'Browser access';
  get('extension-open').hidden = authentication || pairing || !['needs-consent', 'needs-permission', 'needs-account'].includes(state);
  get('authentication-open').hidden = !authentication || !['auth_idle', 'auth_unconfirmed'].includes(state);
  get('authentication-browser').hidden = !authentication || state !== 'unavailable';
  get('extension-open').textContent = state === 'needs-account' ? 'Open creator account' : 'Open extension';
  get('task-retry').hidden = !['unavailable', 'service_unavailable', 'account_mismatch'].includes(state);
  get('extension-install').hidden = state !== 'unavailable' || !payload;
  if (payload) {
    get('extension-install').href = 'https://chromewebstore.google.com/detail/' + payload.extension_id;
    get('desktop-return').href = 'ofca://onboarding/?journey=' + payload.task.journey_id;
    if (authentication && payload.ceremony) get('authentication-browser').href = payload.ceremony.entry_url + '#system-browser';
    else get('authentication-browser').removeAttribute('href');
  }
  get('desktop-return').hidden = !payload || !(['reported', 'task_completed', 'task_expired', 'task_changed', 'profile_mismatch',
    'account_mismatch', 'auth_checking', 'auth_closed', 'auth_unconfirmed', 'auth_access_required', 'pairing_checking',
    'pairing_unconfirmed', 'pairing_access_required'].includes(state) || (authentication || pairing) && state === 'unavailable');
  get('desktop-return').textContent = ['task_expired', 'task_changed', 'profile_mismatch'].includes(state) ? 'Continue setup' : 'Continue in desktop';
}
