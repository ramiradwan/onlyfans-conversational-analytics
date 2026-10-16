import { focusTransition, setStatus } from '../shared-setup/presentation.js';

const tasks = {
  'browser-access': ['Browser access', 'Allow the extension to use this Chrome profile and select the creator account.', 'setup_browser_access'],
  'account-connection': ['Connect your account', 'Sign in, then confirm the account connection here.', 'setup_account_connection'],
  'finish-setup': ['Finish setup', 'Set up access to this computer and connect the browser.', 'setup_finish'],
  complete: ['Setup complete', '', 'setup_complete'],
};
const actions = { 'browser.access': 'Set up browser access', 'account.connect': 'Sign in', 'account.confirm': 'Connect account', 'setup.finalize': 'Continue', 'browser.pair': 'Connect browser' };
const errors = {
  status_unavailable: 'The setup connection was interrupted. Reconnecting…',
  session_unavailable: 'The desktop session could not be restored. Try again.',
  authorization_unavailable: 'Access to setup status was refused. Sign in again.',
  operation_unconfirmed: 'The action’s outcome is not confirmed.',
  operation_refused: 'The action was refused.',
  authority_unavailable: 'Account authorization is unavailable.',
  browser_unavailable: 'The browser connection is unavailable.',
  chrome_unavailable: 'Chrome could not be found. Install Chrome, then try again.',
  browser_preparing: 'The browser task is being prepared.',
  account_mismatch: 'The selected creator account does not match the browser account.',
  operation_expired: 'This task expired.',
  activation_unavailable: 'Full analytics activation could not be prepared. Try again.',
};
const passkeyErrors = {
  passkey_status_unavailable: 'The local sign-in service could not report passkey status.',
  passkey_status_network: 'The local sign-in service could not be reached.',
  passkey_status_session: 'The sign-in status request was not authenticated.',
  passkey_status_forbidden: 'Access to sign-in status was refused.',
  passkey_status_invalid: 'The local sign-in service returned an invalid status response.',
  passkey_sign_in_required: 'Passkey created. Sign in to continue.',
  passkey_start_unavailable: 'Sign-in could not start. Try again.',
  passkey_unconfirmed: 'Verification could not be confirmed.',
};

// Avatars show the first character of a labelled account's name.
const initial = label => [...new Intl.Segmenter(undefined, { granularity: 'grapheme' })
  .segment((label?.display_name || label?.username || '').trim())][0]?.segment.toUpperCase() || '';

export function renderSetup(document, state) {
  const get = id => document.getElementById(id);
  const value = state.snapshot;
  const authentication = state.authentication;
  const needsAuthentication = Boolean(authentication && !authentication.authenticated);
  get('local-access').hidden = !needsAuthentication;
  get('local-access').dataset.journeyState = 'desktop.setup_local_access';
  get('local-authenticate').hidden = !needsAuthentication || authentication.enrolled === null;
  get('local-authenticate').disabled = !state.connected || authentication?.busy;
  get('local-authenticate').textContent = authentication?.enrolled ? 'Sign in' : 'Set up access';
  get('local-auth-retry').hidden = !needsAuthentication || authentication.enrolled !== null || authentication.busy || !authentication.checked;
  setStatus(get('local-auth-status'), passkeyErrors[authentication?.error]
    || (authentication?.busy ? authentication.checked ? 'Waiting for verification…' : 'Checking passkey status…' : ''));
  const task = value?.next_task;
  const decision = value?.decision?.action === 'connect-account' && value?.decision_status?.state !== 'accepted'
    ? value.decision : null;
  const decisionStatus = value?.decision_status?.state;
  const signIn = task === 'account-connection' ? value?.authentication : null;
  const allowed = value?.allowed_actions || [];
  const accepted = decisionStatus === 'accepted';
  // The Provider projection does not identify its authenticated customer. Do
  // not offer ambiguous account switching without that context.
  const canSwitch = !accepted && Boolean(decision) && signIn?.state === 'admitted'
    && signIn.actions.switch_account && allowed.includes('auth.switch');
  const choosingAccount = Boolean(state.choosingAccount && canSwitch);
  const selectAction = signIn?.operation_id ? 'auth.select' : 'account.connect';
  const canSelect = !accepted && signIn?.actions.select && allowed.includes(selectAction);
  const choosingProvider = Boolean(signIn && (canSelect || choosingAccount));
  const controlsDisabled = !state.connected || needsAuthentication || Boolean(state.acting);
  const [title, description, journeyState] = tasks[task] || (state.error
    ? ['Setup unavailable', '', 'setup_unavailable'] : ['Loading setup', 'Reading local setup status…', 'setup_loading']);
  document.querySelector('main').dataset.journeyState = 'desktop.' + (state.error === 'status_unavailable' || state.error === 'session_unavailable'
    ? 'setup_unavailable' : journeyState);
  get('task-title').textContent = title;
  get('task-description').textContent = choosingAccount ? 'Choose how to sign in with another account.'
    : choosingProvider ? 'Choose how to sign in. You will confirm the account connection here.'
      : decision ? 'Review the account and computer, then confirm the connection.' : description;
  get('task-description').hidden = !description;
  get('decision-details').hidden = !decision || choosingAccount;
  const label = decision && value?.creator_label?.creator_id === decision.creator_id ? value.creator_label : null;
  get('decision-account').textContent = decision ? label?.display_name || (label?.username ? '@' + label.username
    : 'OnlyFans account ' + decision.creator_id) : '';
  // Unlabelled accounts show a generic glyph.
  get('decision-account-avatar').textContent = initial(label);
  get('decision-account-avatar').hidden = !get('decision-account-avatar').textContent;
  get('decision-account-glyph').hidden = !get('decision-account-avatar').hidden;
  const account = value?.creator_label && (!value.decision || value.decision.creator_id === value.creator_label.creator_id)
    ? value.creator_label : null;
  get('header-account').hidden = !account;
  get('header-account-avatar').textContent = initial(account);
  get('header-account-name').textContent = account ? account.display_name || '@' + account.username : '';
  get('header-account-handle').textContent = account?.display_name && account.username ? '@' + account.username : '';
  get('header-account-handle').hidden = !get('header-account-handle').textContent;
  // A display name may be shared, so the handle or account number follows it.
  get('decision-account-identifier').textContent = !label?.display_name ? ''
    : label.username ? '@' + label.username : 'OnlyFans account ' + decision.creator_id;
  get('decision-account-identifier').hidden = !get('decision-account-identifier').textContent;
  get('decision-computer').textContent = decision?.computer_name || '';
  get('task-step').textContent = task === 'complete' ? 'Desktop setup' : task ? 'Step ' + (Object.keys(tasks).indexOf(task) + 1) + ' of 3' : 'Desktop setup';
  const pending = Boolean(state.pending || value?.pending_operations.length);
  get('setup-code-open').hidden = !value || pending || value.facts.installation === 'verified' || Boolean(authentication);
  get('setup-code-open').disabled = !state.connected || pending;
  const operationReason = value?.pending_operations.find(item => item.reason !== 'none')?.reason;
  const reason = state.error || (value?.reason === 'none' ? null : value?.reason) || operationReason;
  const signInMessage = { selected: 'Finish signing in in Chrome.', expired: 'This sign-in request expired.',
    retired: 'Sign-in was cancelled.', unavailable: 'The sign-in request could not be confirmed.' }[signIn?.state];
  const progress = task === 'complete' ? state.continuing ? 'Opening dashboard…' : ''
    : state.acting === 'auth.cancel' ? 'Cancelling sign-in…'
    : state.acting && ['auth.select', 'auth.switch', 'account.connect', 'browser.open'].includes(state.acting) ? 'Opening Chrome…'
      : accepted ? 'Account connection accepted. Finishing setup…'
        : choosingProvider ? '' : signInMessage || (decisionStatus === 'awaiting-authentication' ? 'Finish signing in in Chrome.'
          : decisionStatus === 'unavailable' ? 'This sign-in request is unavailable.' : pending && !decision ? 'Waiting for the current action to finish.' : '');
  setStatus(get('task-status'), errors[reason] || progress);
  get('sign-in-options').hidden = !choosingProvider;
  for (const button of get('sign-in-options').querySelectorAll('[data-provider]')) {
    button.hidden = !signIn?.choices[button.dataset.provider];
    button.disabled = controlsDisabled;
    button.dataset.action = choosingAccount ? 'auth.switch' : selectAction;
  }
  const action = decision && allowed.includes('account.confirm') ? 'account.confirm'
    : allowed.find(item => actions[item] && (item !== 'browser.access' || task === 'browser-access')
      && (item !== 'account.connect' || signIn?.state === 'admitted'));
  get('task-action').hidden = !action || choosingProvider || accepted;
  get('task-action').disabled = controlsDisabled || (decision ? Boolean(state.pending) : pending);
  get('task-action').textContent = value?.continuation_available ? 'Continue setup'
    : action === 'account.connect' && signIn?.state === 'admitted' ? 'Continue' : actions[action] || '';
  get('task-action').dataset.action = action || '';
  get('browser-reopen').hidden = accepted || choosingProvider || Boolean(decision) || !state.connected
    || (signIn ? !signIn.actions.open_browser || !(signIn.state === 'admitted'
      || ['chrome_unavailable', 'browser_unavailable'].includes(reason)) : !value?.pending_operations.some(item => ['browser.access', 'browser.pair'].includes(item.action))
      || reason === 'authority_unavailable' || !['browser-access', 'finish-setup'].includes(task));
  get('browser-reopen').disabled = controlsDisabled;
  get('browser-reopen').textContent = signIn ? 'Try again' : 'Continue connecting';
  get('sign-in-switch').hidden = !canSwitch || choosingAccount;
  get('sign-in-switch').textContent = 'Use a different sign-in account';
  get('sign-in-switch').disabled = controlsDisabled;
  get('sign-in-back').hidden = !choosingAccount;
  get('sign-in-back').disabled = controlsDisabled;
  get('task-reconnect').hidden = state.error !== 'session_unavailable';
  for (const group of document.querySelectorAll('.setup-actions')) {
    group.hidden = [...group.children].every(child => child.hidden);
  }
  for (const step of document.querySelectorAll('[data-step]')) {
    if (step.dataset.step === task) step.setAttribute('aria-current', 'step');
    else step.removeAttribute('aria-current');
    step.dataset.complete = String(Boolean(task) && Object.keys(tasks).indexOf(step.dataset.step) < Object.keys(tasks).indexOf(task));
  }
  focusTransition(get('task-title'), state.deliberate);
}
