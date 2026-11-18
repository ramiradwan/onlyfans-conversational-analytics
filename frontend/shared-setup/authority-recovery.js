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
  if (!state.connected) return card('Restore access on this computer', 'Sign in to restore access.',
    'Checking recovery status\u2026', 'progress');
  const childPending = state.pending && state.pending !== auth?.operation_id && ['account.connect', 'auth.select', 'auth.cancel'].includes(state.pendingAction);
  if (childPending && !acting) {
    if (state.pendingAction === 'auth.cancel') return card('Restore access on this computer', '', 'Checking sign-in\u2026', 'progress');
    return card('Restore access on this computer', '', 'Preparing sign-in\u2026', 'progress');
  }
  if (acting === 'account.connect' || acting === 'auth.select') return card('Restore access on this computer',
    'Sign in to restore access.', 'Preparing sign-in\u2026', 'progress');
  if (acting === 'auth.cancel') return card('Restore access on this computer',
    '', 'Cancelling sign-in\u2026', 'progress');
  if (acting === 'browser.open' || state.browserLaunchPending) return card('Sign in to restore access',
    'Use the account connected to this computer.', 'Opening Chrome\u2026', 'progress');
  if (recovery === 'available') return card('Restore access on this computer',
    'Your access has expired. Sign in with your connected account to restore it.', '', 'neutral',
    permitted('account.connect') ? 'account.connect' : null, 'Restore access');
  if (recovery === 'preparing') return card('Restore access on this computer',
    'Sign in to restore access.', 'Preparing sign-in\u2026', 'progress');
  if (recovery === 'checking') return card('Restore access on this computer',
    '', 'Checking sign-in\u2026', 'progress');
  if (recovery === 'restoring') return card('Restoring access on this computer',
    '', 'Sign-in complete. Restoring access\u2026', 'progress');
  if (recovery === 'blocked') return card('Access could not be restored',
    'Contact support to restore access on this computer.', '', 'warning');
  if (recovery !== 'sign-in-required' || !auth) return card('Restore access on this computer',
    '', 'Checking sign-in\u2026', 'progress');
  const select = auth.actions.select && auth.choices.default && permitted('auth.select');
  const cancel = auth.actions.cancel && permitted('auth.cancel');
  if (auth.state === 'selection-required' || auth.state === 'retired') return card('Sign in to restore access',
    'Use the account connected to this computer.', auth.state === 'retired' ? 'Sign-in ended.' : '', 'neutral',
    select ? 'auth.select' : null, auth.state === 'retired' ? 'Sign in again' : 'Sign in');
  if (auth.state === 'selected') {
    const phase = value.authority_recovery_browser;
    if (phase === 'ready' && auth.actions.open_browser) return card('Sign in to restore access',
      'Finish signing in in Chrome.', state.error === 'chrome_unavailable' ? 'Install Chrome to continue.' : '',
      state.error === 'chrome_unavailable' ? 'warning' : 'neutral', 'browser.open', 'Open Chrome', cancel);
    if (phase === 'launch-claimed') return card('Sign in to restore access',
      'Finish signing in if Chrome opens.', 'Waiting for Chrome\u2026', 'progress', null, null, cancel);
    if (phase === 'handed-off') return card('Sign in to restore access',
      'Finish signing in in Chrome, then return here.', 'Waiting for sign-in\u2026', 'progress', null, null, cancel);
    return card('Sign in to restore access',
      'If sign-in did not open or finish, cancel it before signing in again.',
      'Sign-in is not confirmed.', 'warning', null, null, cancel);
  }
  return card('Restore access on this computer', '', 'Checking sign-in\u2026', 'progress');
}

export function authorityRecoveryOptions(action) {
  return action === 'auth.select' ? { recover_authority: true, provider: 'default' } : { recover_authority: true };
}
