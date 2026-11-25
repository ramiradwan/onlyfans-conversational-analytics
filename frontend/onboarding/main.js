import '../shared-setup/shell.css';
import '../shared-setup/providers.css';
import { applyTheme } from '../shared-setup/presentation.js';
import { DesktopSetupClient } from './client.js';
import { renderSetup } from './view.js';
import { openBrowserTask, openSetupCode, presentationGuard } from './browser-task.js';
import { DesktopEnrollment } from './enrollment.js';
import { savedDataEntry, savedDataLinkText, savedDataUrl } from '../shared-setup/saved-data-entry.js';
import { authorityRecoveryView, authorityRecoveryOptions } from '../shared-setup/authority-recovery.js';
import { followPasskeyOpen, nativePasskeyPresentation, openPasskeySettings, passkeyPermissionMessage,
  passkeyWindow } from '../shared-setup/passkey-window.js';

applyTheme(document);
const savedDataLink = document.getElementById('saved-data-open');
let entry = 'unavailable';
let updateEntryView = () => {};
let entryRead;
function checkSavedData() {
  entryRead?.abort();
  const controller = new AbortController();
  entryRead = controller;
  void savedDataEntry(controller.signal).then(result => {
    if (controller.signal.aborted || result === entry) return;
    entry = result;
    savedDataLink.href = savedDataUrl;
    savedDataLink.textContent = savedDataLinkText;
    savedDataLink.hidden = result === 'unavailable';
    updateEntryView();
  });
}
checkSavedData();
const fields = new URLSearchParams(location.hash.slice(1));
let client;
try {
  if (fields.size !== 1 || !fields.has('presentation')) throw new Error('session_unavailable');
  let current = { snapshot: null, pending: null, connected: false, error: null };
  let choosingAccount = false;
  let acting = null, activeAction = null;
  let replacingCeremony = false;
  let provider = null;
  let finished = false;
  let runtimeObserved = false, setupGeneration = null;
  let chromeAvailable = false, chromeScope = null, chromeCheck = 0;
  const actionScope = state => {
    const value = state.snapshot;
    return state.connected && value ? JSON.stringify([value.generation, value.journey_id, value.next_task,
      value.authentication?.operation_id, value.authentication?.authentication_attempt?.authentication_attempt_id]) : null;
  };
  let passkeyFeedback = null;
  const explainsPasskey = () => document.documentElement.dataset.accountPasskey === 'true' && client?.session?.stage === 'setup'
    && current.snapshot?.next_task === 'account-connection'
    && !current.snapshot.decision && current.snapshot.authority_recovery === undefined;
  const beginAction = (name) => {
    const owner = { scope: actionScope(current) };
    activeAction = owner; acting = name; render();
    return owner;
  };
  const endAction = (owner) => {
    if (activeAction !== owner) return;
    activeAction = null; acting = null; replacingCeremony = false; render();
  };
  const missingChrome = () => current.error === 'chrome_unavailable'
    || current.snapshot?.reason === 'chrome_unavailable'
    || current.snapshot?.pending_operations.some(item => item.reason === 'chrome_unavailable');
  const scope = () => current.connected && missingChrome() && current.snapshot
    ? JSON.stringify([actionScope(current), current.pending, current.snapshot.pending_operations.map(item => item.operation_id)]) : null;
  const checkChrome = async () => {
    const owner = scope();
    if (finished || !owner) return;
    const generation = ++chromeCheck;
    try {
      const available = await globalThis.__TAURI__.core.invoke('desktop_chrome_available');
      if (!finished && generation === chromeCheck && scope() === owner) {
        chromeAvailable = available === true;
        render();
      }
    } catch { /* A failed read cannot admit a browser launch. */ }
  };
  const render = () => {
    const owner = scope();
    if (chromeScope !== owner) {
      chromeCheck++; chromeAvailable = false; chromeScope = owner;
      if (owner) void checkChrome();
    }
    const authentication = client?.session?.stage === 'runtime' ? enrollment.state : null;
    const sessionFailure = ['session_unavailable', 'authorization_unavailable', 'status_unavailable', 'connection_unavailable'].includes(current.error);
    const continuing = !finished && current.connected && !sessionFailure && current.snapshot?.reason === 'none'
      && current.snapshot.next_task === 'complete' && current.snapshot.local_ready === true
      && authentication?.checked === true && authentication.authenticated === true
      && !authentication.busy && !authentication.error;
    const passkey = current.connected && passkeyFeedback?.generation === current.snapshot?.generation
      && passkeyFeedback?.operation === (current.snapshot?.decision?.operation_id ?? null)
      ? passkeyFeedback : null;
    renderSetup(document, { ...current, choosingAccount, acting, provider, authentication, continuing, chromeAvailable, replacingCeremony,
      savedAuthorizationExpired: entry === 'authorization-expired',
      explainsPasskey: explainsPasskey(), passkeyMessage: passkey?.message, passkeySupportRef: passkey?.supportRef,
      nativePasskey: nativePasskeyPresentation(), passkeyPermission: passkey?.outcome === 'permission_required' });
    const passkeyDeadline = acting ? null : client?.takeReturnedPasskey();
    if (passkeyDeadline) void startForegroundPasskey(current.snapshot, passkeyDeadline);
    if (!setupGeneration) passkeyOpen.check();
    if (continuing) {
      finished = true;
      void passkeyWindow('discard_passkey_unlock');
      location.replace('/');
    }
  };
  const enrollment = new DesktopEnrollment({ receive: render, refresh: () => {
    if (finished) return;
    // No action or navigation may use the snapshot from the previous operator
    // session. Only the first fresh setup event can restore connected state.
    current = { ...current, connected: false };
    render();
    return client.connect();
  } });
  const passkeyOpen = followPasskeyOpen(() => {
    const authentication = enrollment.state;
    return !finished && current.connected && client?.session?.stage === 'runtime' && !current.snapshot?.authority_recovery
      && authentication.checked && authentication.nativeRequired && authentication.enrolled
      && !authentication.authenticated && !authentication.busy && !authentication.access;
  }, () => { void enrollment.authenticate(); });
  client = new DesktopSetupClient({ generation: fields.get('presentation'), receive: state => {
    if (activeAction && activeAction.scope !== actionScope(state)) {
      activeAction = null; acting = null; replacingCeremony = false;
    }
    if (current.snapshot?.authentication?.authentication_attempt?.authentication_attempt_id
      !== state.snapshot?.authentication?.authentication_attempt?.authentication_attempt_id) choosingAccount = false;
    if (!acting && !state.continuingProvider && (!state.pending || state.error
      || current.continuingProvider && state.pending === current.pending)) provider = null;
    const restored = current.snapshot?.authority_recovery && state.connected && !state.snapshot?.authority_recovery;
    const wasPaused = current.snapshot?.account_passkey === 'paused';
    current = state;
    if (client.session?.stage === 'setup' && state.connected && setupGeneration !== client.generation) {
      setupGeneration = client.generation;
      void passkeyWindow('discard_passkey_unlock');
    }
    if (client.session?.stage === 'runtime' && state.connected && !runtimeObserved) {
      runtimeObserved = true;
      checkSavedData();
    }
    if (restored && client.session?.stage === 'runtime') { void enrollment.check(); render(); return; }
    render();
    if (!wasPaused && state.connected && state.snapshot?.account_passkey === 'paused' && acting === 'passkey.pause') {
      document.getElementById('account-passkey-resume').focus();
    }
    if (client.session?.stage === 'runtime' && state.connected && !state.snapshot?.authority_recovery
      && !enrollment.state.checked && !enrollment.state.busy) {
      const initial = setupGeneration === client.generation && nativePasskeyPresentation() ? presentationGuard(client) : null;
      void enrollment.check({ initial });
    }
  }, openBrowser: openBrowserTask });
  const act = async (action, options, continued = null) => {
    if (acting) return;
    const owner = beginAction(action);
    const enrollmentIntent = explainsPasskey() && ['account.connect', 'auth.select'].includes(action);
    const step = enrollmentIntent ? { ...continued, enrollPasskey: true } : continued;
    const work = step ? client.act(action, options, step) : client.act(action, options);
    const presented = presentationGuard(client);
    try {
      const result = await work;
      if (!finished && activeAction === owner && presented() && result.state !== 'superseded') choosingAccount = false;
    } catch {
      if (!finished && activeAction === owner && presented()) client.notify('operation_refused');
    } finally { endAction(owner); }
  };
  const protectedAct = async action => {
    const view = authorityRecoveryView(current, acting);
    if (finished || !view || view.disabled || (action !== view.action && !(action === 'auth.cancel' && view.cancel))) return;
    const owner = beginAction(action);
    const work = action === 'browser.open' ? client.launchProtectedBrowser() : client.act(action, authorityRecoveryOptions(action));
    const presented = presentationGuard(client);
    try {
      const result = await work;
      if (!finished && activeAction === owner && presented() && result.state === 'unconfirmed') void client.connect();
    } catch {
      if (!finished && activeAction === owner && presented()) void client.connect();
    } finally { endAction(owner); }
  };
  document.getElementById('protected-cancel').addEventListener('click', () => { void protectedAct('auth.cancel'); });
  document.getElementById('task-action').addEventListener('click', event => {
    const button = event.currentTarget;
    if (finished || button.hidden || button.disabled || !button.dataset.action || !current.connected) return;
    if (button.dataset.protectedRecovery === 'true') { void protectedAct(button.dataset.action); return; }
    if (current.snapshot?.authority_recovery || !current.snapshot?.allowed_actions.includes(button.dataset.action)) return;
    const replacing = button.dataset.replaceCeremony === 'true';
    if (replacing && current.snapshot?.authentication?.state !== 'replacement-required') return;
    const continuingSource = button.dataset.action === 'account.connect' && current.snapshot?.continuation_available
      && !current.snapshot?.authentication;
    if (replacing || continuingSource) replacingCeremony = true;
    void act(button.dataset.action, replacing ? { replace_ceremony: true } : {});
  });
  for (const button of document.querySelectorAll('#sign-in-options [data-provider]')) {
    button.addEventListener('click', event => {
      const control = event.currentTarget;
      const { action, provider: selected, replaceCeremony, sourceContinuation } = control.dataset;
      if (finished || control.hidden || control.disabled || control.closest('[hidden]') || !current.connected
        || current.snapshot?.authority_recovery
        || !current.snapshot?.allowed_actions.includes(action)
        || !current.snapshot.authentication?.choices[selected]) return;
      const auth = current.snapshot.authentication;
      if (replaceCeremony === 'true') {
        if (action !== 'auth.select' || auth.state !== 'replacement-required' || !auth.operation_id) return;
        if (!acting) provider = selected;
        void act('auth.select', { replace_ceremony: true }, { continueProvider: selected });
        return;
      }
      if (sourceContinuation === 'true') {
        if (action !== 'account.connect' || !current.snapshot.continuation_available
          || auth.state !== 'replacement-required' || auth.operation_id) return;
        if (!acting) provider = selected;
        void act('account.connect', {}, { continueProvider: selected });
        return;
      }
      if (auth.state === 'replacement-required') return;
      if (!acting) provider = selected;
      void act(action, { provider: selected });
    });
  }
  document.getElementById('sign-in-switch').addEventListener('click', () => {
    choosingAccount = true; render();
    document.querySelector('#sign-in-options button:not([hidden])')?.focus();
  });
  document.getElementById('sign-in-back').addEventListener('click', () => {
    choosingAccount = false; render(); document.getElementById('sign-in-switch').focus();
  });
  document.getElementById('browser-reopen').addEventListener('click', async event => {
    const button = event.currentTarget, operation = button.dataset.operation;
    if (acting || button.hidden || button.disabled || !operation) return;
    const owner = beginAction('browser.open');
    const presented = presentationGuard(client);
    try { await client.launchBrowser(true, operation); }
    catch { if (!finished && activeAction === owner && presented()) client.notify('browser_unavailable'); }
    finally { endAction(owner); }
  });
  document.getElementById('setup-code-open').addEventListener('click', async event => {
    if (acting) return;
    event.currentTarget.disabled = true;
    const owner = beginAction('setup-code.open');
    const presented = presentationGuard(client);
    try { await openSetupCode(client); }
    catch (error) {
      if (!finished && activeAction === owner && presented()) {
        client.notify(error === 'chrome_unavailable' ? 'chrome_unavailable' : 'browser_unavailable');
      }
    } finally { endAction(owner); }
  });
  const expectedPasskey = value => ({
    generation: value.generation, revision: value.revision, scope_digest: value.scope_digest,
  });

  async function changePasskeyPause(paused) {
    const value = current.snapshot;
    if (acting || finished || !current.connected || (paused
      ? !['available', 'recoverable'].includes(value?.account_passkey)
      : value?.account_passkey !== 'paused')) return;
    client.discardReturnedPasskey();
    passkeyFeedback = null;
    const action = paused ? 'pause' : 'resume';
    const owner = beginAction('passkey.' + action);
    const presented = presentationGuard(client);
    try {
      const result = await client.json('/api/v1/desktop/setup/passkey/' + action, expectedPasskey(value));
      if (result?.outcome !== (paused ? 'paused' : 'resumed')) throw new Error('setup_status_unavailable');
    }
    catch {
      if (!finished && activeAction === owner && presented()) {
        passkeyFeedback = { generation: value.generation, operation: value.decision?.operation_id ?? null,
          message: paused ? 'Dipsy couldn’t confirm that setup paused.' : 'Dipsy couldn’t resume setup. Try again.' };
      }
    } finally {
      if (!finished && presented()) client.wake();
      endAction(owner);
      if (paused && current.snapshot?.account_passkey === 'paused') document.getElementById('account-passkey-resume').focus();
    }
  }

  async function startForegroundPasskey(value, deadline) {
    const presented = presentationGuard(client);
    try {
      if (!await passkeyWindow('passkey_foreground') || !presented() || document.hidden || Date.now() >= deadline
        || current.snapshot?.decision?.operation_id !== value.decision.operation_id
        || Date.parse(current.snapshot.decision.expires_at) <= Date.now()
        || current.snapshot.authentication?.state !== 'admitted'
        || current.snapshot.authentication.authentication_attempt?.authentication_attempt_id
          !== value.authentication.authentication_attempt.authentication_attempt_id) return;
      await createPasskey();
    } catch { /* The explicit Create passkey action is still available. */ }
  }

  async function createPasskey() {
    const value = current.snapshot;
    if (acting || finished || !current.connected || !['available', 'recoverable'].includes(value?.account_passkey) || !value.decision) return;
    client.discardReturnedPasskey();
    passkeyFeedback = null;
    const owner = beginAction('passkey.create');
    const presented = presentationGuard(client);
    let outcome = 'unavailable';
    let supportRef;
    try {
      const result = await client.json('/api/v1/desktop/setup/passkey', expectedPasskey(value));
      outcome = result?.outcome;
      if (outcome === 'unavailable' && typeof result.support_ref === 'string'
          && /^[A-Za-z0-9_-]{43}$/u.test(result.support_ref)) supportRef = result.support_ref;
    } catch { /* The next explicit action can check this attempt's result. */ }
    finally {
      if (!finished && activeAction === owner && presented()) {
        const messages = {
          cancelled: 'Passkey setup was cancelled.',
          permission_required: passkeyPermissionMessage,
          foreground_required: 'Bring Dipsy to the front, then try again.',
          credential_unavailable: 'Windows Hello could not find a passkey for this account.',
        };
        passkeyFeedback = {
          generation: value.generation,
          operation: value.decision.operation_id,
          supportRef, outcome,
          message: outcome === 'created' ? 'Passkey created.'
            : Object.hasOwn(messages, outcome) ? messages[outcome] : 'Dipsy couldn’t confirm passkey setup. Try again or finish later.',
        };
        client.wake();
      }
      endAction(owner);
    }
  }
  document.getElementById('account-passkey-later').addEventListener('click', () => { void changePasskeyPause(true); });
  document.getElementById('account-passkey-resume').addEventListener('click', () => { void changePasskeyPause(false); });
  document.getElementById('account-passkey-create').addEventListener('click', () => { void createPasskey(); });
  document.getElementById('local-authenticate').addEventListener('click', () => {
    void passkeyWindow('discard_passkey_unlock');
    void enrollment.authenticate();
  });
  for (const id of ['account-passkey-settings', 'local-auth-settings']) {
    document.getElementById(id).addEventListener('click', event => {
      if (!event.currentTarget.hidden && !event.currentTarget.disabled) void openPasskeySettings();
    });
  }
  const onWake = () => {
    if (finished) return;
    checkSavedData();
    void checkChrome();
    client.wake?.();
  };
  const onVisibility = () => { if (!document.hidden) onWake(); };
  window.addEventListener('focus', onWake);
  document.addEventListener('visibilitychange', onVisibility);
  window.addEventListener('pagehide', () => {
    finished = true;
    entryRead?.abort();
    chromeCheck++;
    window.removeEventListener('focus', onWake);
    document.removeEventListener('visibilitychange', onVisibility);
    client.stop();
    passkeyOpen.stop();
    enrollment.stop();
  });
  updateEntryView = render;
  void client.connect();
} catch {
  renderSetup(document, { snapshot: null, pending: null, connected: false, error: 'session_unavailable', deliberate: false });
}
