import { focusTransition, recoveryTone, setStatus } from '../shared-setup/presentation.js';
import { browserRecoveryTarget } from './browser-task.js';
import { authorityRecoveryView } from '../shared-setup/authority-recovery.js';
import { desktopRecovery, passkeyRecovery, recoveryClass } from '../shared-setup/recovery.js';
import { renderSupportHelp } from '../shared-setup/support-help.js';

const tasks = {
  'browser-access': ['Bring in your conversations', 'Set up browser access', 'setup_browser_access'],
  'account-connection': ['Connect your account', 'Connect your account', 'setup_account_connection'],
  'finish-setup': ['Finish setup', 'Finish setup', 'setup_finish'],
  complete: ['Setup complete', 'Desktop setup', 'setup_complete'],
};
const actions = { 'browser.access': 'Continue in browser', 'account.connect': 'Sign in', 'account.confirm': 'Connect account', 'browser.pair': 'Connect extension' };
const namedProviders = new Map([['google', 'Google'], ['microsoft-personal', 'Microsoft'], ['telegram', 'Telegram'], ['tiktok', 'TikTok']]);
const chromeInstruction = 'Finish setup in your browser. This window updates by itself.';
const chooseSignIn = 'Choose how to sign in. You’ll confirm the connection here next.';
const finishSignIn = 'Finish signing in in your browser. This window continues by itself.';
const pairingDescription = 'New messages show up in your analytics once it’s connected.';
const pairingProgress = 'Keep the browser open. This window continues by itself.';
const errors = {
  status_unavailable: 'The setup connection was interrupted. Reconnecting…',
  connection_unavailable: "Dipsy can't connect right now.",
  authorization_unavailable: 'Access to setup status was refused. Sign in again.',
  operation_unconfirmed: 'The action’s outcome is not confirmed.',
  operation_refused: 'The action was refused.',
  authority_unavailable: 'Account authorization is unavailable.',
  browser_unavailable: 'The browser connection is unavailable.',
  chrome_unavailable: 'Install the required browser to continue setup.',
  browser_preparing: 'The browser task is being prepared.',
  extension_unavailable: 'Install or turn on the extension in your browser to continue setup.',
  profile_mismatch: 'Switch to the browser profile that has the extension.',
  account_mismatch: 'Sign in to the OnlyFans account you want to connect.',
  browser_needs_account: 'Sign in to your creator account in your browser.',
  browser_needs_consent: 'Browser access is not turned on in the extension. Turn it on in your browser.',
  browser_needs_permission: 'Allow access to the creator site in the extension.',
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
  renewing: ['Renewing access on this computer', 'This page updates when renewal finishes.', 'Renewal needs an internet connection.'],
  expired: ['Access on this computer isn’t active', 'This page updates by itself if you can sign in again.'],
  removed: ['Access removed from this computer', 'This computer can no longer open analytics.'],
};

// Avatars show the first character of a labelled account's name.
const initial = label => [...new Intl.Segmenter(undefined, { granularity: 'grapheme' })
  .segment((label?.display_name || label?.username || '').trim())][0]?.segment.toUpperCase() || '';
const accountName = label => label?.display_name || (label?.username ? '@' + label.username : '');

const statusIcons = { neutral: 'info', progress: 'pending', success: 'done', warning: 'attention', error: 'attention' };

export function renderSetup(document, state) {
  const get = id => document.getElementById(id);
  const value = state.snapshot;
  const snapshotLive = state.connected && value?.expires_at * 1000 > Date.now();
  const recovery = authorityRecoveryView(state, state.acting);
  const main = document.querySelector('main');
  const card = main.querySelector('.setup-task-card');
  const loading = !value && !state.error;
  const trust = Boolean(recovery || value?.next_task === 'finish-setup' && state.authentication && !state.authentication.authenticated);
  if (trust) card.dataset.composition = 'trust';
  else delete card.dataset.composition;
  const updateStatus = (element, message, tone) => {
    setStatus(element, message || '', tone);
    const kind = message && statusIcons[tone];
    const icon = element.firstElementChild;
    if (kind && icon?.dataset.statusIcon === kind) return;
    icon?.remove();
    if (kind) element.prepend(get('desktop-status-icons').content.querySelector(`[data-status-icon="${kind}"]`).cloneNode(true));
  };
  const taskStatus = get('task-status');
  const actionSlot = card.querySelector('.task-actions-slot > [data-region-content]');
  Object.assign(card.style, { inlineSize: recovery ? '100%' : '', maxInlineSize: recovery ? '560px' : '',
    marginInline: recovery ? 'auto' : '' });
  for (const control of [get('task-action'), get('protected-cancel'), get('task-pending'), card.querySelector('.desktop-loading-action')]) {
    control.style.minBlockSize = recovery ? 'var(--dipsy-static-large-control-height)' : '';
  }
  card.setAttribute('aria-busy', String(loading || Boolean(recovery && !recovery.title)));
  delete main.dataset.access;
  main.querySelector('nav').hidden = Boolean(recovery);
  get('task-step').hidden = Boolean(recovery);
  get('protected-cancel').hidden = true;
  get('task-action').dataset.protectedRecovery = '';
  if (recovery) {
    renderSupportHelp(actionSlot, false);
    main.dataset.journeyState = 'desktop.' + 'setup_authority_recovery';
    main.dataset.task = 'authority-recovery';
    get('task-title').textContent = recovery.title || 'Checking access';
    get('task-description').textContent = recovery.description;
    get('task-description').hidden = !recovery.description;
    const pending = recovery.tone === 'progress' && Boolean(recovery.status);
    const pendingRow = get('task-pending');
    pendingRow.hidden = !pending;
    updateStatus(pendingRow, pending ? recovery.status : '', 'progress');
    updateStatus(taskStatus, pending ? '' : recovery.status, recovery.tone);
    for (const id of ['header-account-avatar', 'header-account-name', 'header-account-handle', 'decision-provider']) get(id).textContent = '';
    for (const id of ['local-access', 'local-authenticate', 'decision-details', 'decision-sign-in', 'decision-provider', 'header-account', 'sign-in-options',
      'browser-reopen', 'sign-in-switch', 'sign-in-back', 'setup-code-open']) get(id).hidden = true;
    const primary = get('task-action'), cancel = get('protected-cancel');
    primary.hidden = !recovery.action;
    primary.disabled = recovery.disabled;
    primary.textContent = recovery.label || '';
    primary.dataset.action = recovery.action || '';
    primary.dataset.protectedRecovery = 'true';
    primary.dataset.replaceCeremony = 'false';
    cancel.hidden = !recovery.cancel;
    cancel.disabled = recovery.disabled;
    cancel.classList.toggle('secondary', !recovery.cancelPrimary);
    for (const group of document.querySelectorAll('.setup-actions')) group.hidden = [...group.children].every(child => child.hidden);
    focusTransition(get('task-title'), state.deliberate);
    return;
  }
  const operationReason = value?.pending_operations.find(item => item.reason !== 'none')?.reason;
  const reportedReason = state.error || (value?.reason === 'none' ? null : value?.reason) || operationReason;
  const reason = reportedReason === 'chrome_unavailable' && state.chromeAvailable === true ? 'browser_unavailable' : reportedReason;
  const reasonClass = recoveryClass(desktopRecovery, reason);
  const unavailable = reasonClass === 'invariant_failed' || reason === 'connection_unavailable';
  const authentication = state.authentication;
  const needsAuthentication = Boolean(authentication && !authentication.authenticated);
  const localAccessReady = !authentication || (authentication.checked && authentication.authenticated
    && !authentication.busy && !authentication.error);
  const lapsed = needsAuthentication && accessCopy[authentication.access] || null;
  const task = value?.next_task;
  const securing = task === 'finish-setup' && needsAuthentication;
  const checkingPasskey = needsAuthentication && (authentication.checking || authentication.recoveringStatus || authentication.checked === false);
  const passkeyPlaceholder = securing && checkingPasskey && !lapsed && !unavailable;
  if (passkeyPlaceholder) card.setAttribute('aria-busy', 'true');
  const passkeyKnown = needsAuthentication && typeof authentication.enrolled === 'boolean' && !lapsed
    && !checkingPasskey;
  const ceremonyPending = passkeyKnown && authentication.busy && !authentication.error && !unavailable;
  const localDescription = lapsed && !securing ? `${lapsed[0]}. ${lapsed[1]}` : needsAuthentication && !securing && passkeyKnown
    ? 'Your computer will ask you to confirm it’s you.' : '';
  get('local-access').hidden = !needsAuthentication || reasonClass === 'invariant_failed' || passkeyPlaceholder
    || Boolean(securing && lapsed && !lapsed[2]);
  get('local-access').dataset.journeyState = 'desktop.setup_local_access';
  setStatus(get('local-access-description'), localDescription);
  get('local-access-description').hidden = !localDescription;
  get('local-authenticate').hidden = !passkeyKnown || ceremonyPending || unavailable;
  get('local-authenticate').disabled = !state.connected || authentication?.busy;
  get('local-authenticate').textContent = authentication?.enrolled ? 'Sign in with passkey' : 'Create passkey';
  const ceremonyFailure = ['passkey_unconfirmed', 'passkey_start_unavailable'].includes(authentication?.error) && authentication?.attempt;
  const statusReadUnavailable = securing && !lapsed && !checkingPasskey && !passkeyKnown
    && (!authentication.error || ['passkey_status_unavailable', 'passkey_status_network'].includes(authentication.error));
  updateStatus(get('local-auth-pending'), ceremonyPending ? authentication.attempt === 'create' ? 'Waiting for your passkey…' : 'Waiting for verification…' : '', 'progress');
  get('local-auth-pending').hidden = !ceremonyPending;
  updateStatus(get('local-auth-status'), lapsed ? lapsed[2] || '' : passkeyPlaceholder ? ''
    : statusReadUnavailable ? 'This page updates by itself.'
    : (authentication?.recoveringStatus ? 'Checking access automatically…' : null)
    || (authentication?.checking ? 'Checking access on this computer…' : null)
    || (authentication?.error ? recoveryClass(passkeyRecovery, authentication.error) === 'invariant_failed'
      ? 'Access on this computer could not be checked.' : ceremonyFailure === 'create' ? 'Passkey setup didn’t finish.'
        : ceremonyFailure === 'sign-in' ? 'Sign-in didn’t finish.' : passkeyErrors[authentication.error] : null),
  lapsed ? 'neutral' : authentication?.error === 'passkey_sign_in_required' ? 'success'
    : ceremonyFailure || statusReadUnavailable ? 'warning' : checkingPasskey || authentication?.busy ? 'progress'
      : recoveryTone(recoveryClass(passkeyRecovery, authentication?.error)));
  const accountDecision = value?.decision?.action === 'connect-account' ? value.decision : null;
  const decisionStatus = value?.decision_status?.state;
  const decision = decisionStatus === 'accepted' ? null : accountDecision;
  const signIn = task === 'account-connection' ? value?.authentication : null;
  const allowed = value?.allowed_actions || [];
  const accepted = decisionStatus === 'accepted';
  const hasChoices = Boolean(signIn && Object.values(signIn.choices || {}).some(Boolean));
  const activeProvider = state.continuingProvider || state.provider || null;
  // The confirmation and current ceremony must both admit a sign-in change.
  const canSwitch = !accepted && Boolean(decision) && signIn?.state === 'admitted'
    && signIn.actions.switch_account && allowed.includes('auth.switch');
  const choosingAccount = Boolean(state.choosingAccount && canSwitch);
  const selectAction = signIn?.operation_id ? 'auth.select' : 'account.connect';
  const canReplace = !accepted && signIn?.state === 'replacement-required' && Boolean(signIn.operation_id)
    && signIn.actions.select && allowed.includes('auth.select') && (!state.pending || state.pending === signIn.operation_id);
  const canSelect = signIn?.state !== 'replacement-required' && !accepted && signIn?.actions.select && allowed.includes(selectAction)
    && (!state.pending || signIn.state === 'selection-required' && state.pending === signIn.operation_id && !activeProvider
      || signIn.state === 'reauthentication-required' && state.pending === signIn.browser_operation_id);
  const sourceContinuation = Boolean(value?.continuation_available && allowed.includes('account.connect')
    && (!signIn || signIn.state === 'replacement-required' && !signIn.operation_id));
  const canReplaceProviders = canReplace && hasChoices;
  const sourceContinuationProviders = !accepted && sourceContinuation && signIn?.state === 'replacement-required' && hasChoices && !state.pending;
  const canReplaceFallback = canReplace && !hasChoices;
  const sourceContinuationFallback = sourceContinuation && !sourceContinuationProviders;
  const choosingProvider = Boolean(signIn && (canSelect || choosingAccount || canReplaceProviders || sourceContinuationProviders));
  // The clicked provider stays in place and busy until its sign-in is selected or fails.
  const signInActions = ['account.connect', 'auth.select', 'auth.switch'];
  const continuingReplacement = Boolean(state.continuingProvider && !state.error && task === 'account-connection');
  // Brain reports no sign-in projection while the accepted command has not selected yet.
  const opening = Boolean(activeProvider && !state.error && task === 'account-connection'
    && (continuingReplacement
      || !['selected', 'admitted', 'reauthentication-required', 'replacement-required', 'replacement-pending'].includes(signIn?.state)
        && (signInActions.includes(state.acting) || signInActions.includes(state.pendingAction)
          || state.pending && value?.pending_operations.some(item =>
            item.operation_id === state.pending && signInActions.includes(item.action)))
      || signIn?.state === 'replacement-required' && (canReplaceProviders || sourceContinuationProviders) && signInActions.includes(state.acting)));
  const showingProviders = choosingProvider || opening;
  const controlsDisabled = !state.connected || !localAccessReady || Boolean(state.acting);
  const pairingPending = task === 'finish-setup' && value.pending_operations.some(item => item.action === 'browser.pair');
  const pairingAvailable = task === 'finish-setup' && state.connected && localAccessReady && !state.error
    && allowed.includes('browser.pair') && !pairingPending;
  const browserWork = task === 'browser-access' && value.pending_operations.some(item => item.action === 'browser.access');
  const startingChrome = task === 'browser-access' && state.acting === 'browser.access';
  const inChrome = task === 'browser-access' && (browserWork || startingChrome || state.pendingAction === 'browser.access'
    || !allowed.includes('browser.access'));
  const recoveryTarget = reason === 'chrome_unavailable' ? null : browserRecoveryTarget(value, reason ?? null);
  const terminal = reasonClass === 'invariant_failed';
  const [title, stepLabel, journeyState] = tasks[task] || (loading
    ? ['Loading setup', '', 'setup_loading'] : ['Setup unavailable', 'Desktop setup', 'setup_unavailable']);
  main.dataset.journeyState = 'desktop.' + (terminal || ['status_unavailable', 'connection_unavailable'].includes(state.error)
    ? 'setup_unavailable' : journeyState);
  main.dataset.task = terminal ? 'unavailable' : task || (loading ? 'loading' : 'unavailable');
  if (securing && !terminal) main.dataset.access = 'passkey';
  const waitingForAccess = task === 'finish-setup' && authentication && !needsAuthentication
    && (!localAccessReady || !state.connected);
  const connecting = pairingPending && state.connected && localAccessReady;
  const confirmingUnknown = task === 'account-connection' && value.pending_operations.some(item =>
    item.action === 'account.confirm' && item.state === 'outcome_unknown');
  let taskTitle = title;
  let taskDescription = '';
  if (terminal) {
    taskTitle = 'Setup couldn’t continue';
  } else if (securing) {
    if (passkeyKnown) {
      taskTitle = authentication.enrolled ? 'Confirm it’s you' : 'Protect your analytics on this computer';
      taskDescription = authentication.enrolled ? 'Your computer will ask you to confirm it’s you.'
        : 'Create a passkey. Your computer will ask you to confirm it’s you, so only you can open your analytics here.';
    } else if (lapsed) [taskTitle, taskDescription] = lapsed;
    else if (checkingPasskey) taskTitle = 'Checking this computer…';
    else taskTitle = 'Can’t check your passkey right now';
  } else if (waitingForAccess) {
    taskTitle = 'Getting ready';
    taskDescription = 'Checking access on this computer…';
  } else if (connecting || pairingAvailable) {
    taskTitle = connecting ? 'Connecting the extension' : 'Connect the extension to this app';
    taskDescription = pairingDescription;
  } else if (task === 'browser-access') {
    if (inChrome) taskDescription = chromeInstruction;
    else if (!reason || reason === 'status_unavailable') {
      taskDescription = 'The browser opens next so the extension can read activity from your creator account. You’ll review what it collects first.';
    }
  } else if (showingProviders || ['selected', 'retired', 'expired'].includes(signIn?.state)) taskDescription = chooseSignIn;
  else if (canReplace) taskDescription = 'Sign in to connect your account.';
  else if (!accepted && !confirmingUnknown) {
    if (decision) taskDescription = 'Connect your OnlyFans account to this computer';
    else if (task === 'account-connection') taskDescription = 'Sign in, then confirm the account connection here.';
    else if (task === 'finish-setup') taskDescription = 'Set up access to this computer and connect the browser.';
  }
  get('task-title').textContent = taskTitle;
  get('task-description').textContent = taskDescription;
  get('task-description').hidden = !taskDescription;
  get('decision-details').hidden = !accountDecision || choosingAccount || terminal;
  const account = !terminal && !accountDecision && !(task === 'account-connection' && accepted) && reason !== 'account_mismatch'
    && (!value?.decision || value.decision.creator_id === value.creator_label?.creator_id) ? value?.creator_label : null;
  get('header-account').hidden = !accountName(account);
  get('header-account-avatar').textContent = initial(account);
  get('header-account-name').textContent = accountName(account);
  get('header-account-handle').textContent = account?.display_name && account.username ? '@' + account.username : '';
  get('header-account-handle').hidden = !get('header-account-handle').textContent;
  get('task-step').textContent = task && task !== 'complete' ? `Step ${Object.keys(tasks).indexOf(task) + 1} of 3 · ${stepLabel}`
    : stepLabel || (terminal ? 'Desktop setup' : '');
  const pending = Boolean(state.pending || value?.pending_operations.length || startingChrome);
  get('setup-code-open').hidden = !value || pending || opening || value.facts.installation === 'verified' || Boolean(authentication);
  get('setup-code-open').disabled = !state.connected || pending || opening;
  const reasonMessage = reasonClass === 'invariant_failed' ? ''
    : reason === 'connection_unavailable' && value?.journey_id
      ? 'Dipsy can’t connect right now. Your setup progress is saved.'
      : reason === 'account_mismatch' && snapshotLive && value.facts.account === 'verified'
        ? 'Switch to the OnlyFans account you used for setup.'
        : reportedReason === 'browser_unavailable' && inChrome && recoveryTarget
          ? 'The browser closed or lost its connection.' : reasonClass ? errors[reason] : null;
  if (unavailable || reason === 'chrome_unavailable') get('setup-code-open').hidden = true;
  const signInMessage = { selected: finishSignIn, expired: 'Sign in to continue.',
    'replacement-required': 'Sign-in ended.',
    retired: 'Continue when you’re ready.', unavailable: 'The sign-in request could not be confirmed.' }[signIn?.state];
  const finalizing = task === 'finish-setup' && (allowed.includes('setup.finalize')
    || value.pending_operations.some(item => item.action === 'setup.finalize'));
  const replacing = task === 'account-connection' && (state.replacingCeremony
    || signIn?.state === 'replacement-pending'
    || continuingReplacement
    || opening && signIn?.state === 'replacement-required');
  const replacementUnknown = value?.pending_operations.some(item => ['auth.select', 'account.connect'].includes(item.action)
    && item.state === 'outcome_unknown');
  const switching = state.choosingAccount || [state.acting, state.pendingAction].some(action => ['auth.switch', 'auth.select', 'auth.cancel'].includes(action))
    || value?.pending_operations.some(item => ['auth.switch', 'auth.select', 'auth.cancel'].includes(item.action));
  const confirmedProvider = namedProviders.get(signIn?.authentication_attempt?.provider);
  const hasConfirmedProvider = decision && allowed.includes('account.confirm') && snapshotLive && Date.parse(decision.expires_at) > Date.now()
    && !reason && !confirmingUnknown && !replacing && !switching
    && signIn?.state === 'admitted' && signIn.operation_id === decision.operation_id && !signIn.actions.open_browser
    && (!value.decision_status || decisionStatus === 'confirmation-required' && value.decision_status.operation_id === decision.operation_id)
    && Boolean(confirmedProvider);
  get('decision-provider').textContent = hasConfirmedProvider ? `Signed in with ${confirmedProvider}` : '';
  get('decision-provider').hidden = !hasConfirmedProvider;
  const progress = task === 'complete' ? state.continuing ? 'Opening dashboard…' : ''
    : replacing ? (replacementUnknown ? 'Checking sign-in\u2026' : 'Preparing sign-in\u2026')
    : state.acting === 'auth.cancel' ? 'Cancelling sign-in…'
    : opening ? finishSignIn : state.acting === 'browser.open' ? 'Opening browser…'
      : choosingProvider ? '' : signInMessage || (decisionStatus === 'awaiting-authentication' ? finishSignIn
        : decisionStatus === 'unavailable' ? 'This sign-in request is unavailable.'
          : pending && !decision && !connecting && !recoveryTarget ? 'Waiting for the current action to finish.' : '');
  // The access section explains a lapse. Nothing progresses until access returns,
  // so the task line then names only errors unrelated to it.
  const unfinished = canSelect && signIn?.state === 'reauthentication-required' && !state.acting;
  const endedPrompt = (canReplace || sourceContinuationProviders) && !opening;
  const taskTone = reasonClass === 'invariant_failed' ? recoveryTone(reasonClass)
    : replacing ? 'progress' : endedPrompt || unfinished ? 'warning' : reasonClass ? recoveryTone(reasonClass)
    : progress && (state.continuing || opening || state.acting || finalizing || accepted
      || signIn?.state === 'selected' || pending && !decision) ? 'progress'
      : signIn?.state === 'expired' || signIn?.state === 'unavailable' ? 'warning' : 'neutral';
  get('sign-in-options').hidden = !showingProviders || unavailable;
  for (const button of get('sign-in-options').querySelectorAll('[data-provider]')) {
    // Without a projection with choices, the opening choices keep the visibility they were clicked in.
    if (hasChoices || !opening) button.hidden = !signIn?.choices[button.dataset.provider];
    button.disabled = controlsDisabled || opening || unavailable;
    if (opening && button.dataset.provider === activeProvider) button.setAttribute('aria-busy', 'true');
    else button.removeAttribute('aria-busy');
    button.dataset.action = choosingAccount ? 'auth.switch'
      : canReplaceProviders ? 'auth.select'
        : sourceContinuationProviders ? 'account.connect' : selectAction;
    button.dataset.replaceCeremony = String(canReplaceProviders);
    button.dataset.sourceContinuation = String(sourceContinuationProviders);
  }
  const action = canReplaceFallback ? 'auth.select' : decision && allowed.includes('account.confirm') ? 'account.confirm'
    : allowed.find(item => actions[item] && (item !== 'browser.access' || task === 'browser-access')
      && (item !== 'account.connect' || sourceContinuationFallback));
  const taskAction = get('task-action');
  const submittingPair = state.acting === 'browser.pair' || state.pendingAction === 'browser.pair';
  taskAction.hidden = unavailable || reason === 'chrome_unavailable' || !action || showingProviders || accepted || !localAccessReady
    || startingChrome || confirmingUnknown || action === 'browser.pair' && (!pairingAvailable || pending && !submittingPair);
  taskAction.disabled = controlsDisabled || replacing || action === 'browser.pair' && submittingPair
    || (!canReplaceFallback && (decision ? Boolean(state.pending) : pending));
  taskAction.textContent = canReplaceFallback ? 'Sign in again' : value?.continuation_available && action === 'account.connect' ? 'Continue setup'
    : actions[action] || '';
  taskAction.dataset.action = taskAction.hidden ? '' : action;
  taskAction.dataset.replaceCeremony = String(canReplaceFallback && !taskAction.hidden);
  const browserReopen = get('browser-reopen');
  browserReopen.hidden = canReplace || replacing || unavailable || !recoveryTarget || accepted || showingProviders || Boolean(decision) || !state.connected
    || Boolean(state.browserLaunchPending || state.acting || !localAccessReady);
  browserReopen.disabled = controlsDisabled;
  browserReopen.dataset.operation = browserReopen.hidden ? '' : recoveryTarget;
  browserReopen.textContent = 'Open browser';
  const reconnectingChrome = inChrome && reason === 'status_unavailable';
  const preparingChrome = inChrome && browserReopen.hidden && (startingChrome || state.browserLaunchPending || reason === 'browser_preparing');
  const pendingMessage = terminal ? '' : reconnectingChrome ? 'Reconnecting…' : preparingChrome ? 'Getting the browser ready…'
    : confirmingUnknown ? 'Checking whether your setup finished…'
      : finalizing && !reason ? 'Preparing this computer…'
        : accepted && task === 'account-connection' && !reason ? 'Account connected. Moving to the next step…'
          : connecting && browserReopen.hidden ? pairingProgress : '';
  updateStatus(get('task-pending'), pendingMessage, 'progress');
  get('task-pending').hidden = !pendingMessage;
  const status = terminal ? '' : lapsed ? reason !== 'authority_unavailable' && reasonMessage
    : reconnectingChrome || preparingChrome && reason === 'browser_preparing' || confirmingUnknown && (!reason || reason === 'operation_unconfirmed') ? ''
      : replacing ? progress : endedPrompt ? signInMessage : unfinished ? 'Sign-in did not finish.'
        : reasonMessage || (!pendingMessage && (connecting ? pairingProgress : progress));
  updateStatus(taskStatus, status, connecting && !reason ? 'progress' : taskTone);
  get('sign-in-switch').hidden = !canSwitch || choosingAccount || unavailable;
  get('sign-in-switch').disabled = controlsDisabled;
  get('decision-sign-in').hidden = !hasConfirmedProvider && get('sign-in-switch').hidden;
  get('sign-in-back').hidden = !choosingAccount || unavailable;
  get('sign-in-back').disabled = controlsDisabled;
  for (const group of document.querySelectorAll('.setup-actions')) {
    group.hidden = [...group.children].every(child => child.hidden);
  }
  renderSupportHelp(actionSlot, terminal && Object.hasOwn(desktopRecovery, reason)
    && !loading && !state.acting && !state.pending && !state.pendingAction && !state.browserLaunchPending
    && !actionSlot.querySelector(':scope > :not([hidden], [aria-hidden="true"], [data-support-help])'));
  for (const step of document.querySelectorAll('[data-step]')) {
    if (step.dataset.step === task) step.setAttribute('aria-current', 'step');
    else step.removeAttribute('aria-current');
    step.dataset.complete = String(Boolean(task) && Object.keys(tasks).indexOf(step.dataset.step) < Object.keys(tasks).indexOf(task));
  }
  focusTransition(get('task-title'), state.deliberate);
}
