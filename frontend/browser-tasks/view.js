import { setStatus } from '../shared-setup/presentation.js';
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
const status = {
  loading: 'Reading the current task…',
  unavailable: 'Check that the extension is turned on in this Chrome profile, then return to the desktop app.',
  extension_disconnected: 'Dipsy couldn’t connect to the extension. Continue setup in the desktop app.',
  profile_mismatch: 'Switch to the Chrome profile you used for setup, then return to the desktop app.',
  account_mismatch: 'Switch to the creator account you want to connect, then return to the desktop app.',
  auth_idle: 'Continue signing in to your account.',
  auth_opening: 'Waiting for sign-in to finish.',
  auth_checking: 'Checking the sign-in outcome…',
  auth_closed: 'Sign-in window closed. Continue in the desktop app.',
  auth_unconfirmed: 'Checking whether sign-in finished…',
  auth_access_required: 'Browser access is required for this request.',
  pairing_connecting: 'Connecting this browser to the desktop app…',
  pairing_checking: 'Checking the browser connection…',
  pairing_saved: 'This Chrome profile has a saved desktop connection. Replace it to connect to this desktop app.',
  pairing_unconfirmed: 'The browser connection has not been confirmed.',
  pairing_access_required: 'Browser access is required to connect.',
  task_expired: 'This browser task expired.',
  task_changed: 'This browser task is no longer current.',
  task_completed: 'Browser task complete.',
  service_unavailable: 'Dipsy can’t confirm this step. Continue setup in the desktop app.',
  status_reconnecting: 'Waiting for the desktop app. Checking again automatically.',
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
  const state = Object.hasOwn(status, value.state) ? value.state : 'service_unavailable';
  document.querySelector('main').dataset.journeyState = 'browser.' + journeyStates[state];
  const classification = value.recovery ?? recoveryClass(browserRecovery, value.state);
  const failure = state === 'service_unavailable' && classification === 'invariant_failed'
    ? "This browser task couldn't continue."
    : state === 'service_unavailable' && classification === 'authorization_refused'
      ? 'This browser task is not authorized.' : null;
  setStatus(get('task-status'), failure || (!payload && state === 'service_unavailable'
    ? 'Dipsy can’t connect right now. Open the desktop app to continue setup.' : status[state]));
  get('pairing-replace').hidden = !pairing || state !== 'pairing_saved';
  get('task-title').textContent = pairing ? 'Connect browser' : authentication ? 'Sign in' : 'Browser access';
  get('extension-open').hidden = authentication || pairing || !['needs-consent', 'needs-permission', 'needs-account'].includes(state);
  get('authentication-open').hidden = !authentication || state !== 'auth_idle';
  get('extension-open').textContent = state === 'needs-account' ? 'Open creator account' : 'Open extension';
  get('extension-install').hidden = state !== 'unavailable' || !payload;
  if (payload) {
    get('extension-install').href = 'https://chromewebstore.google.com/detail/' + payload.extension_id;
    get('desktop-return').href = 'ofca://onboarding/?journey=' + payload.task.journey_id;
  }
  get('desktop-return').hidden = !payload || !(['reported', 'task_completed', 'task_expired', 'task_changed', 'profile_mismatch',
    'account_mismatch', 'unavailable', 'extension_disconnected', 'service_unavailable', 'auth_checking', 'auth_closed', 'auth_unconfirmed', 'auth_access_required', 'pairing_checking',
    'pairing_unconfirmed', 'pairing_access_required'].includes(state));
  get('desktop-return').textContent = ['task_expired', 'task_changed', 'profile_mismatch'].includes(state) ? 'Continue setup' : 'Continue in desktop';
}
