/** Read-only presentation of the independently admitted installation recovery. */
export function authorityRecoveryView(state, acting = null) {
  const value = state.snapshot;
  const recovery = value?.authority_recovery;
  if (!recovery) return null;
  const auth = value.authentication;
  const permitted = action => value.allowed_actions.includes(action);
  const disabled = !state.connected || Boolean(acting || state.browserLaunchPending);
  const card = (title, description, status, tone = 'neutral', action = null, label = null, cancel = false) =>
    ({ title, description, status, tone, action, label, cancel, disabled });
  const title = 'Sign in to open your analytics';
  const sameSignIn = 'Use the same sign-in as before.';
  const finishInChrome = 'Finish signing in in Chrome.';
  const preparing = () => card(title, sameSignIn, 'Preparing sign-in…', 'progress', null, 'Sign in');
  const checking = () => card(title, sameSignIn, 'Checking sign-in…', 'progress');
  const canceling = () => card(title, sameSignIn, 'Canceling sign-in…', 'progress', null, 'Cancel sign-in');

  // An explicit sign-in continues by itself, so its next step shows progress instead of a button.
  const continuing = state.recoverySignInPending && recovery === 'sign-in-required';
  if (!state.connected) {
    if (state.error === 'session_unavailable') return card('Restore access on this computer', '', "Setup couldn't continue.", 'error');
    if (state.error === 'connection_unavailable') return card(title, '',
      value.journey_id ? 'Dipsy can’t connect right now. Your setup progress is saved.' : 'Dipsy can’t connect right now.', 'warning');
    return card('', '', '', 'progress');
  }
  const childPending = state.pending && state.pending !== auth?.operation_id && ['account.connect', 'auth.select', 'auth.cancel'].includes(state.pendingAction);
  if (childPending && !acting) {
    return state.pendingAction === 'auth.cancel' ? checking() : preparing();
  }
  if (acting === 'account.connect' || acting === 'auth.select' || continuing && auth?.state === 'selection-required') {
    return preparing();
  }
  if (acting === 'auth.cancel') return canceling();
  if (acting === 'browser.open' || state.browserLaunchPending || continuing && auth?.state === 'selected'
      && value.authority_recovery_browser === 'ready' && auth.actions.open_browser) return card(title,
    finishInChrome, 'Opening Chrome…', 'progress', null, 'Open Chrome');
  if (recovery === 'available') return card(title, sameSignIn, '', 'neutral',
    permitted('account.connect') ? 'account.connect' : null, 'Sign in');
  if (recovery === 'preparing') return preparing();
  if (recovery === 'checking') return checking();
  if (recovery === 'restoring') return card('Opening your analytics',
    '', 'Signed in. Getting this computer ready…', 'progress');
  if (recovery === 'blocked') return card('Can’t open your analytics right now',
    'This page updates by itself if anything changes.', '');
  if (recovery !== 'sign-in-required' || !auth) return checking();
  const select = auth.actions.select && auth.choices.default && permitted('auth.select');
  const cancel = auth.actions.cancel && permitted('auth.cancel');
  if (auth.state === 'selection-required' || auth.state === 'retired') return card(title, sameSignIn,
    auth.state === 'retired' ? 'Sign-in didn’t finish.' : '', 'neutral', select ? 'auth.select' : null, 'Sign in');
  if (auth.state === 'selected') {
    const phase = value.authority_recovery_browser;
    const chromeUnavailable = state.error === 'chrome_unavailable';
    if (phase === 'ready' && auth.actions.open_browser) return card(title,
      chromeUnavailable ? 'Sign-in happens in Chrome.' : finishInChrome, chromeUnavailable ? 'Install Chrome to continue.' : '',
      chromeUnavailable ? 'warning' : 'neutral', 'browser.open', 'Open Chrome', cancel);
    if (phase === 'launch-claimed') return card(title,
      'Finish signing in in Chrome when it opens.', 'Waiting for Chrome…', 'progress', null, null, cancel);
    if (phase === 'handed-off') return card(title,
      'Finish signing in in Chrome. This page continues by itself.', 'Waiting for sign-in…', 'progress', null, null, cancel);
    return { ...card(title, 'If you didn’t finish signing in, cancel it here.',
      'Sign-in isn’t confirmed.', 'warning', null, null, cancel), cancelPrimary: cancel };
  }
  return checking();
}

export function authorityRecoveryOptions(action) {
  return action === 'auth.select' ? { recover_authority: true, provider: 'default' } : { recover_authority: true };
}
