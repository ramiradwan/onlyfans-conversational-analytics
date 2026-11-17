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
  extension_unavailable: 'The extension is not available in that Chrome profile. Install or turn it on, then choose Try again.',
  profile_mismatch: 'Chrome opened a different profile. Switch to the profile that has the extension, then choose Try again.',
  account_mismatch: 'The selected creator account does not match the browser account.',
  browser_needs_account: 'Chrome does not recognise your creator account yet. Reload your OnlyFans tab, then choose Try again.',
  browser_needs_consent: 'Browser access is not turned on in the extension. Turn it on, then choose Try again.',
  browser_needs_permission: 'The extension cannot open OnlyFans. Allow its site access, then choose Try again.',
  operation_expired: 'This task expired.',
  activation_unavailable: 'Full analytics activation could not be prepared. Try again.',
};
const passkeyErrors = {
  passkey_status_unavailable: 'Dipsy can’t check access on this computer right now.',
  passkey_status_network: 'Dipsy can’t check access on this computer right now.',
  passkey_status_session: 'This setup session is no longer authorized.',
  passkey_status_forbidden: 'Access on this computer could not be verified.',
  passkey_status_invalid: 'Access on this computer could not be checked.',
  passkey_sign_in_required: 'Passkey created. Sign in to continue.',
  passkey_start_unavailable: 'Sign-in could not start.',
  passkey_unconfirmed: 'Verification could not be confirmed.',
};
// No action here can restore access. Pushed updates replace this copy.
const accessCopy = {
  renewing: ['Renewing access on this computer. This window updates when renewal finishes.', 'Renewal needs an internet connection.'],
  expired: ['Access on this computer expired. It could not be renewed in time.', 'Contact support to restore access.'],
  removed: ['Access was removed from this computer.', 'Contact support if you did not expect this.'],
};

// Avatars show the first character of a labelled account's name.
const initial = label => [...new Intl.Segmenter(undefined, { granularity: 'grapheme' })
  .segment((label?.display_name || label?.username || '').trim())][0]?.segment.toUpperCase() || '';

export function renderSetup(document, state) {
  const get = id => document.getElementById(id);
  const value = state.snapshot;
  const authentication = state.authentication;
  const needsAuthentication = Boolean(authentication && !authentication.authenticated);
  const lapsed = needsAuthentication && accessCopy[authentication.access] || null;
  get('local-access').hidden = !needsAuthentication;
  get('local-access').dataset.journeyState = 'desktop.setup_local_access';
  setStatus(get('local-access-description'), lapsed?.[0] || 'Use a passkey to sign in on this computer.');
  get('local-authenticate').hidden = !needsAuthentication || authentication.enrolled === null;
  get('local-authenticate').disabled = !state.connected || authentication?.busy;
  get('local-authenticate').textContent = authentication?.enrolled ? 'Sign in' : 'Set up access';
  setStatus(get('local-auth-status'), lapsed?.[1]
    || (authentication?.recoveringStatus ? 'Checking access automatically…' : null)
    || (authentication?.checking ? 'Checking access on this computer…' : null)
    || passkeyErrors[authentication?.error]
    || (authentication?.busy ? 'Waiting for verification…' : ''));
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
  // The clicked provider stays in place and busy until its sign-in is selected or fails.
  const signInActions = ['account.connect', 'auth.select', 'auth.switch'];
  // Brain reports no sign-in projection while the accepted command has not selected yet.
  const opening = Boolean(state.provider && !state.error && task === 'account-connection'
    && !['selected', 'admitted', 'reauthentication-required'].includes(signIn?.state)
    && (signInActions.includes(state.acting) || state.pending && value?.pending_operations.some(item =>
      item.operation_id === state.pending && signInActions.includes(item.action))));
  const showingProviders = choosingProvider || opening;
  const controlsDisabled = !state.connected || needsAuthentication || Boolean(state.acting);
  const [title, description, journeyState] = tasks[task] || (state.error
    ? ['Setup unavailable', '', 'setup_unavailable'] : ['Loading setup', 'Reading local setup status…', 'setup_loading']);
  document.querySelector('main').dataset.journeyState = 'desktop.' + (state.error === 'status_unavailable' || state.error === 'session_unavailable'
    ? 'setup_unavailable' : journeyState);
  get('task-title').textContent = title;
  get('task-description').textContent = choosingAccount ? 'Choose how to sign in with another account.'
    : showingProviders ? 'Choose how to sign in. You will confirm the account connection here.'
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
    : opening || state.acting && ['auth.select', 'auth.switch', 'account.connect', 'browser.open'].includes(state.acting) ? 'Opening Chrome…'
      : accepted ? 'Account connection accepted. Finishing setup…'
        : choosingProvider ? '' : signInMessage || (decisionStatus === 'awaiting-authentication' ? 'Finish signing in in Chrome.'
          : decisionStatus === 'unavailable' ? 'This sign-in request is unavailable.' : pending && !decision ? 'Waiting for the current action to finish.' : '');
  // The access section explains a lapse. Nothing progresses until access returns,
  // so the task line then names only errors unrelated to it.
  const unfinished = canSelect && signIn?.state === 'reauthentication-required' && !state.acting;
  setStatus(get('task-status'), (lapsed ? reason !== 'authority_unavailable' && errors[reason]
    : unfinished ? 'Sign-in did not finish.' : errors[reason] || progress) || '');
  get('sign-in-options').hidden = !showingProviders;
  for (const button of get('sign-in-options').querySelectorAll('[data-provider]')) {
    // Without a projection the opening choices keep the visibility they were clicked in.
    if (signIn || !opening) button.hidden = !signIn?.choices[button.dataset.provider];
    button.disabled = controlsDisabled || opening;
    if (opening && button.dataset.provider === state.provider) button.setAttribute('aria-busy', 'true');
    else button.removeAttribute('aria-busy');
    button.dataset.action = choosingAccount ? 'auth.switch' : selectAction;
  }
  const action = decision && allowed.includes('account.confirm') ? 'account.confirm'
    : allowed.find(item => actions[item] && (item !== 'browser.access' || task === 'browser-access')
      && (item !== 'account.connect' || signIn?.state === 'admitted'));
  get('task-action').hidden = !action || showingProviders || accepted;
  get('task-action').disabled = controlsDisabled || (decision ? Boolean(state.pending) : pending);
  get('task-action').textContent = value?.continuation_available ? 'Continue setup'
    : action === 'account.connect' && signIn?.state === 'admitted' ? 'Continue' : actions[action] || '';
  get('task-action').dataset.action = action || '';
  get('browser-reopen').hidden = accepted || showingProviders || Boolean(decision) || !state.connected
    || (signIn ? !signIn.actions.open_browser || !(signIn.state === 'admitted'
      || ['chrome_unavailable', 'browser_unavailable', 'extension_unavailable', 'profile_mismatch', 'browser_needs_account',
        'browser_needs_consent', 'browser_needs_permission'].includes(reason)) : !value?.pending_operations.some(item => ['browser.access', 'browser.pair'].includes(item.action))
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
