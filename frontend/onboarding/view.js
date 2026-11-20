import { focusTransition, recoveryTone, setStatus } from '../shared-setup/presentation.js';
import { browserRecoveryTarget } from './browser-task.js';
import { authorityRecoveryView } from '../shared-setup/authority-recovery.js';
import { desktopRecovery, passkeyRecovery, recoveryClass } from '../shared-setup/recovery.js';

const tasks = {
  'browser-access': ['Browser access', 'Allow the extension to use this Chrome profile and select the creator account.', 'setup_browser_access'],
  'account-connection': ['Connect your account', 'Sign in, then confirm the account connection here.', 'setup_account_connection'],
  'finish-setup': ['Finish setup', 'Set up access to this computer and connect the browser.', 'setup_finish'],
  complete: ['Setup complete', '', 'setup_complete'],
};
const actions = { 'browser.access': 'Set up browser access', 'account.connect': 'Sign in', 'account.confirm': 'Connect account', 'browser.pair': 'Connect extension' };
const errors = {
  status_unavailable: 'The setup connection was interrupted. Reconnecting…',
  session_unavailable: 'Dipsy couldn’t restore your session.',
  authorization_unavailable: 'Access to setup status was refused. Sign in again.',
  operation_unconfirmed: 'The action’s outcome is not confirmed.',
  operation_refused: 'The action was refused.',
  authority_unavailable: 'Account authorization is unavailable.',
  browser_unavailable: 'The browser connection is unavailable.',
  chrome_unavailable: 'Install Chrome to continue setup.',
  browser_preparing: 'The browser task is being prepared.',
  extension_unavailable: 'Install or turn on the extension in Chrome to continue setup.',
  profile_mismatch: 'Switch to the Chrome profile that has the extension.',
  account_mismatch: 'Switch to the creator account you want to connect in Chrome.',
  browser_needs_account: 'Chrome does not recognise your creator account yet. Open your creator account in Chrome.',
  browser_needs_consent: 'Browser access is not turned on in the extension. Turn it on in Chrome.',
  browser_needs_permission: 'The extension cannot open OnlyFans. Allow its site access in Chrome.',
  operation_expired: 'This task expired.',
  activation_unavailable: 'Dipsy couldn’t prepare your activation.',
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
  const recovery = authorityRecoveryView(state, state.acting);
  const main = document.querySelector('main');
  main.querySelector('nav').hidden = Boolean(recovery);
  get('protected-cancel').hidden = true;
  get('task-action').dataset.protectedRecovery = '';
  if (recovery) {
    main.dataset.journeyState = 'desktop.' + 'setup_authority_recovery';
    main.dataset.task = 'authority-recovery';
    get('task-step').textContent = 'Account access';
    get('task-title').textContent = recovery.title;
    get('task-description').textContent = recovery.description;
    get('task-description').hidden = !recovery.description;
    setStatus(get('task-status'), recovery.status, recovery.tone);
    for (const id of ['local-access', 'local-authenticate', 'decision-details', 'header-account', 'sign-in-options',
      'browser-reopen', 'sign-in-switch', 'sign-in-back', 'task-reconnect', 'setup-code-open']) get(id).hidden = true;
    const primary = get('task-action'), cancel = get('protected-cancel');
    primary.hidden = !recovery.action;
    primary.disabled = recovery.disabled;
    primary.textContent = recovery.label || '';
    primary.dataset.action = recovery.action || '';
    primary.dataset.protectedRecovery = 'true';
    primary.dataset.replaceCeremony = 'false';
    cancel.hidden = !recovery.cancel;
    cancel.disabled = recovery.disabled;
    for (const group of document.querySelectorAll('.setup-actions')) group.hidden = [...group.children].every(child => child.hidden);
    focusTransition(get('task-title'), state.deliberate);
    return;
  }
  const authentication = state.authentication;
  const needsAuthentication = Boolean(authentication && !authentication.authenticated);
  const localAccessReady = !authentication || (authentication.checked && authentication.authenticated
    && !authentication.busy && !authentication.error);
  const lapsed = needsAuthentication && accessCopy[authentication.access] || null;
  get('local-access').hidden = !needsAuthentication;
  get('local-access').dataset.journeyState = 'desktop.setup_local_access';
  setStatus(get('local-access-description'), lapsed?.[0] || 'Use a passkey to sign in on this computer.');
  get('local-authenticate').hidden = !needsAuthentication || authentication.enrolled === null;
  get('local-authenticate').disabled = !state.connected || authentication?.busy;
  get('local-authenticate').textContent = authentication?.enrolled ? 'Sign in with passkey' : 'Set up passkey';
  setStatus(get('local-auth-status'), lapsed?.[1]
    || (authentication?.recoveringStatus ? 'Checking access automatically…' : null)
    || (authentication?.checking ? 'Checking access on this computer…' : null)
    || (authentication?.error ? recoveryClass(passkeyRecovery, authentication.error) === 'invariant_failed'
      ? 'Access on this computer could not be checked.' : passkeyErrors[authentication.error] : null)
    || (authentication?.busy ? 'Waiting for verification…' : ''),
  authentication?.recoveringStatus || authentication?.checking || authentication?.busy || authentication?.access === 'renewing'
    ? 'progress' : lapsed ? 'warning' : recoveryTone(recoveryClass(passkeyRecovery, authentication?.error)));
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
  const canReplace = !accepted && signIn?.state === 'replacement-required' && signIn.actions.select
    && allowed.includes('auth.select') && (!state.pending || state.pending === signIn.operation_id);
  const canSelect = signIn?.state !== 'replacement-required' && !accepted && signIn?.actions.select && allowed.includes(selectAction)
    && (!state.pending || signIn.state === 'selection-required' && state.pending === signIn.operation_id && !state.provider
      || signIn.state === 'reauthentication-required' && state.pending === signIn.browser_operation_id);
  const sourceContinuation = Boolean(value?.continuation_available && !signIn && allowed.includes('account.connect'));
  const choosingProvider = Boolean(signIn && (canSelect || choosingAccount));
  // The clicked provider stays in place and busy until its sign-in is selected or fails.
  const signInActions = ['account.connect', 'auth.select', 'auth.switch'];
  // Brain reports no sign-in projection while the accepted command has not selected yet.
  const opening = Boolean(state.provider && !state.error && task === 'account-connection'
    && !['selected', 'admitted', 'reauthentication-required', 'replacement-required', 'replacement-pending'].includes(signIn?.state)
    && (signInActions.includes(state.acting) || state.pending && value?.pending_operations.some(item =>
      item.operation_id === state.pending && signInActions.includes(item.action))));
  const showingProviders = choosingProvider || opening;
  const controlsDisabled = !state.connected || !localAccessReady || Boolean(state.acting);
  const pairingPending = task === 'finish-setup' && (state.acting === 'browser.pair'
    || value?.pending_operations.some(item => item.action === 'browser.pair'));
  const pairingAvailable = task === 'finish-setup' && state.connected && localAccessReady && !state.error
    && allowed.includes('browser.pair') && !pairingPending;
  const [title, description, journeyState] = tasks[task] || (state.error
    ? ['Setup unavailable', '', 'setup_unavailable'] : ['Loading setup', 'Reading local setup status…', 'setup_loading']);
  document.querySelector('main').dataset.journeyState = 'desktop.' + (state.error === 'status_unavailable' || state.error === 'session_unavailable'
    ? 'setup_unavailable' : journeyState);
  const securing = task === 'finish-setup' && needsAuthentication;
  const waitingForAccess = task === 'finish-setup' && authentication && !needsAuthentication
    && (!localAccessReady || !state.connected);
  const connecting = pairingPending && state.connected && localAccessReady;
  get('task-title').textContent = securing ? 'Secure your app'
    : waitingForAccess ? 'Getting ready' : connecting ? 'Connecting the extension'
      : pairingAvailable ? 'Connect the browser extension' : title;
  const taskDescription = securing ? '' : waitingForAccess ? 'Checking access on this computer…'
    : connecting ? 'Keep Chrome open while Dipsy connects.'
      : pairingAvailable ? 'Connect Chrome to Dipsy so they can work together.'
        : choosingAccount ? 'Choose how to sign in with another account.'
          : showingProviders ? 'Choose how to sign in. You will confirm the account connection here.'
            : canReplace ? 'Sign in to connect your account.'
              : decision ? 'Review the account and computer, then confirm the connection.' : description;
  get('task-description').textContent = taskDescription;
  get('task-description').hidden = !taskDescription;
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
  const reportedReason = state.error || (value?.reason === 'none' ? null : value?.reason) || operationReason;
  const reason = reportedReason === 'chrome_unavailable' && state.chromeAvailable === true ? 'browser_unavailable' : reportedReason;
  const reasonClass = recoveryClass(desktopRecovery, reason);
  const reasonMessage = reasonClass === 'invariant_failed' ? "Setup couldn't continue." : reasonClass ? errors[reason] : null;
  if (reasonClass === 'invariant_failed' || reason === 'chrome_unavailable') get('setup-code-open').hidden = true;
  if (task === 'browser-access' && reasonMessage) get('task-description').hidden = true;
  const signInMessage = { selected: 'Finish signing in in Chrome.', expired: 'This sign-in request expired.',
    'replacement-required': 'Sign-in ended.',
    retired: 'Sign-in was cancelled.', unavailable: 'The sign-in request could not be confirmed.' }[signIn?.state];
  const finalizing = task === 'finish-setup' && (allowed.includes('setup.finalize')
    || value.pending_operations.some(item => item.action === 'setup.finalize'));
  const replacing = task === 'account-connection' && (state.replacingCeremony || signIn?.state === 'replacement-pending');
  const replacementUnknown = value?.pending_operations.some(item => ['auth.select', 'account.connect'].includes(item.action)
    && item.state === 'outcome_unknown');
  const progress = task === 'complete' ? state.continuing ? 'Opening dashboard…' : ''
    : replacing ? (replacementUnknown ? 'Checking sign-in\u2026' : 'Preparing sign-in\u2026')
    : state.acting === 'auth.cancel' ? 'Cancelling sign-in…'
    : opening || state.acting && ['auth.select', 'auth.switch', 'account.connect', 'browser.open', 'browser.pair'].includes(state.acting) ? 'Opening Chrome…'
      : finalizing ? 'Preparing Dipsy on this computer…'
        : accepted ? 'Account connection accepted. Finishing setup…'
        : choosingProvider ? '' : signInMessage || (decisionStatus === 'awaiting-authentication' ? 'Finish signing in in Chrome.'
          : decisionStatus === 'unavailable' ? 'This sign-in request is unavailable.' : pending && !decision && !connecting ? 'Waiting for the current action to finish.' : '');
  // The access section explains a lapse. Nothing progresses until access returns,
  // so the task line then names only errors unrelated to it.
  const unfinished = canSelect && signIn?.state === 'reauthentication-required' && !state.acting;
  const taskTone = reasonClass === 'invariant_failed' ? recoveryTone(reasonClass)
    : replacing ? 'progress' : canReplace || unfinished ? 'warning' : reasonClass ? recoveryTone(reasonClass)
    : progress && (state.continuing || opening || state.acting || finalizing || accepted
      || signIn?.state === 'selected' || pending && !decision) ? 'progress'
      : signIn?.state === 'expired' || signIn?.state === 'unavailable' ? 'warning' : 'neutral';
  setStatus(get('task-status'), (lapsed ? reason !== 'authority_unavailable' && reasonMessage
    : reasonClass === 'invariant_failed' ? reasonMessage : replacing ? progress : canReplace ? signInMessage : unfinished ? 'Sign-in did not finish.' : reasonMessage || progress) || '', taskTone);
  document.querySelector('main').dataset.task = task || 'loading';
  get('sign-in-options').hidden = !showingProviders || reasonClass === 'invariant_failed';
  for (const button of get('sign-in-options').querySelectorAll('[data-provider]')) {
    // Without a projection the opening choices keep the visibility they were clicked in.
    if (signIn || !opening) button.hidden = !signIn?.choices[button.dataset.provider];
    button.disabled = controlsDisabled || opening || reasonClass === 'invariant_failed';
    if (opening && button.dataset.provider === state.provider) button.setAttribute('aria-busy', 'true');
    else button.removeAttribute('aria-busy');
    button.dataset.action = choosingAccount ? 'auth.switch' : selectAction;
  }
  const action = canReplace ? 'auth.select' : decision && allowed.includes('account.confirm') ? 'account.confirm'
    : allowed.find(item => actions[item] && (item !== 'browser.access' || task === 'browser-access')
      && (item !== 'account.connect' || signIn?.state === 'admitted' || sourceContinuation));
  const taskAction = get('task-action');
  taskAction.hidden = reasonClass === 'invariant_failed' || reason === 'chrome_unavailable' || !action || showingProviders || accepted || !localAccessReady
    || action === 'browser.pair' && (!pairingAvailable || pending);
  taskAction.disabled = controlsDisabled || replacing || (!canReplace && (decision ? Boolean(state.pending) : pending));
  taskAction.textContent = canReplace ? 'Sign in again' : value?.continuation_available && action === 'account.connect' ? 'Continue setup'
    : action === 'account.connect' && signIn?.state === 'admitted' ? 'Continue' : actions[action] || '';
  taskAction.dataset.action = taskAction.hidden ? '' : action;
  taskAction.dataset.replaceCeremony = String(canReplace && !taskAction.hidden);
  const recoveryTarget = reason === 'chrome_unavailable' ? null : browserRecoveryTarget(value, reason ?? null);
  const browserReopen = get('browser-reopen');
  browserReopen.hidden = canReplace || replacing || reasonClass === 'invariant_failed' || !recoveryTarget || accepted || showingProviders || Boolean(decision) || !state.connected
    || Boolean(state.browserLaunchPending || state.acting || !localAccessReady);
  browserReopen.disabled = controlsDisabled;
  browserReopen.dataset.operation = browserReopen.hidden ? '' : recoveryTarget;
  browserReopen.textContent = 'Open Chrome';
  get('sign-in-switch').hidden = !canSwitch || choosingAccount || reasonClass === 'invariant_failed';
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
