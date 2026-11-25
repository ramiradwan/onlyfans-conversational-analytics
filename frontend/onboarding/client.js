import { command, operation, setupFrame, snapshot, UUID } from './contract.js';
import { SCOPED_SETUP_EVENT, setupEvents } from './events.js';
import { browserRecoveryTarget, openProtectedBrowser, presentationGuard } from './browser-task.js';

// A helper tab reports why it ended. The desktop shows it where retry lives.
const NOTICE_ERRORS = { extension_unavailable: 'extension_unavailable', profile_mismatch: 'profile_mismatch',
  account_mismatch: 'account_mismatch', task_expired: 'operation_expired', unavailable: 'browser_unavailable',
  needs_account: 'browser_needs_account', needs_consent: 'browser_needs_consent', needs_permission: 'browser_needs_permission' };
function currentBrowserNotices(value, notices = []) {
  if (!notices.length || value.authority_recovery !== undefined) return [];
  // Brain has already checked each task's admission generation. Keep every
  // still-pending operation's notice for the surface displaying that operation.
  const pending = new Set(value.pending_operations.map(item => item.operation_id));
  return notices.filter(notice => pending.has(notice.operation_id)).flatMap(notice => {
    if (notice.reason === 'task_changed' && value.next_task === 'account-connection'
      && value.allowed_actions.includes('browser.access') && value.authentication?.browser_operation_id === notice.operation_id) {
      return [{ ...notice, reason: 'browser_recheck_required' }];
    }
    return NOTICE_ERRORS[notice.reason] ? [{ ...notice, reason: NOTICE_ERRORS[notice.reason] }] : [];
  });
}

// The later steps of one explicit protected sign-in run only within this bound.
const RECOVERY_SIGN_IN_MS = 120_000;
const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 16_000, 30_000];
const RECONNECT_SLOW_MS = 30_000;
const outcome = (state, operationId = null, reason = null) => ({ state, operationId, reason });

async function refreshNativePresentation(generation) {
  const invoke = globalThis.__TAURI__?.core?.invoke;
  if (!invoke) throw Error('session_unavailable');
  return invoke('desktop_refresh_presentation', { generation });
}
async function focusNativeWindow() {
  await globalThis.__TAURI__?.core?.invoke('desktop_window', { action: 'focus' });
}

/** One presentation; only explicit actions can dispatch a protected command. */
export class DesktopSetupClient {
  constructor({ generation, request = globalThis.fetch.bind(globalThis), receive, storage = sessionStorage, uuid = () => crypto.randomUUID(), stream = setupEvents, openBrowser = null, refreshPresentation = refreshNativePresentation, focusWindow = focusNativeWindow, now = Date.now,
    operatorCsrf = () => globalThis.document?.querySelector('meta[name="csrf-token"]')?.content ?? null }) {
    if (!UUID.test(generation)) throw new Error('session_unavailable');
    Object.assign(this, { generation, request, receive, storage, uuid, stream, openBrowser, operatorCsrf, refreshPresentation, focusWindow, now });
    this.launchOperation = null;
    this.launchEpoch = 0;
    this.launching = false;
    this.protectedOpening = null;
    this.protectedLaunchError = null;
    this.recoverySignIn = null;
    this.providerContinuation = null;
    this.launchRequested = false;
    this.focusIntent = null;
    this.value = null;
    this.session = null;
    this.connected = false;
    this.pending = null;
    this.controller = null;
    this.intent = null;
    this.reconnectTimer = null;
    this.reconnectAttempt = 0;
    this.reconnectExhausted = false;
    this.connecting = false;
    this.stopped = false;
    this.operationError = null;
    this.browserNotices = [];
    this.key = 'desktop-setup-operation:' + generation;
    try {
      const saved = storage.getItem(this.key);
      if (UUID.test(saved)) this.pending = saved;
    } catch { /* Brain remains the durable operation owner. */ }
  }

  notify(error = null, deliberate = false) {
    const operationError = this.value && this.operationError?.journey === this.value.journey_id ? this.operationError : null;
    // The setup window still presents one current step. Configured surfaces
    // select their own feedback from the scoped fields.
    const setupNotice = this.browserNotices.find(notice => browserRecoveryTarget(this.value, notice.reason) === notice.operation_id);
    this.receive({ snapshot: this.value, pending: this.pending, connected: this.connected,
      error: error || this.protectedLaunchError || operationError?.reason || setupNotice?.reason || null, deliberate,
      failedAction: !error && !this.protectedLaunchError ? operationError?.action : null,
      operationError, browserNotices: this.browserNotices,
      ...(this.intent?.operationId === this.pending ? { pendingAction: this.intent.action } : {}),
      browserLaunchPending: Boolean(this.launchOperation || this.protectedOpening),
      recoverySignInPending: Boolean(this.recoverySignIn),
      continuingProvider: this.providerContinuation?.provider ?? null });
  }

  retireCompletedFeedback() {
    const feedback = this.operationError, value = this.value;
    if (!feedback || !value) return;
    if (feedback.journey !== value.journey_id) {
      this.operationError = null;
      return;
    }
    if (feedback.action === 'browser.pair' && value.local_ready && value.facts.pairing === 'verified') {
      this.operationError = null;
      return;
    }
    if (!['auth.select', 'auth.switch', 'account.connect'].includes(feedback.action)) return;
    const authentication = value.authentication;
    const sameChild = authentication?.browser_operation_id === feedback.operationId;
    const sameParent = feedback.parent !== null && authentication?.operation_id === feedback.parent;
    // A factual review/admission supersedes this child's presentation failure;
    // it does not settle any pending domain operation or a different attempt.
    if (sameChild && authentication.state === 'admitted'
      || sameParent && (value.decision?.operation_id === feedback.parent
        || value.decision_status?.operation_id === feedback.parent && value.decision_status.state === 'accepted')) {
      this.operationError = null;
    }
  }

  operationFeedback(operationId, action, reason) {
    const authentication = this.value?.authentication;
    this.operationError = { reason, action, operationId, journey: this.value?.journey_id,
      parent: authentication?.browser_operation_id === operationId ? authentication.operation_id
        : action === 'account.connect' ? operationId : null };
    this.retireCompletedFeedback();
  }

  returnFocus(completedOperation = null, terminalAuthentication = false) {
    const intent = this.focusIntent, value = this.value;
    if (!intent || !value || !this.connected) return;
    if (intent.epoch !== this.launchEpoch || intent.generation !== this.generation
      || intent.journey !== null && intent.journey !== value.journey_id || intent.deadline <= this.now()) {
      this.focusIntent = null; return;
    }
    const authentication = value.authentication;
    if (intent.selected && authentication && (
      authentication.operation_id !== null && authentication.operation_id !== intent.selected.parent
      || authentication.browser_operation_id !== null && authentication.browser_operation_id !== intent.operationId
      || authentication.authentication_attempt !== null && authentication.authentication_attempt.authentication_attempt_id !== intent.selected.attempt)) {
      this.focusIntent = null; return;
    }
    const sameChild = authentication?.browser_operation_id === intent.operationId;
    if (sameChild && authentication.state === 'selected' && authentication.authentication_attempt !== null && !intent.selected) {
      intent.selected = { parent: authentication.operation_id, attempt: authentication.authentication_attempt.authentication_attempt_id,
        revision: value.revision };
    }
    if (intent.journey !== value.journey_id) return;
    if (completedOperation === intent.operationId && (!terminalAuthentication || intent.selected)) intent.confirmed = true;
    const admitted = authentication?.browser_operation_id === intent.operationId && authentication.state === 'admitted';
    const withdrawn = intent.selected && value.revision > intent.selected.revision
      && authentication?.operation_id === intent.selected.parent && authentication.state === 'selection-required'
      && authentication.actions.select && authentication.authentication_attempt === null && authentication.browser_operation_id === null;
    const terminal = intent.selected && sameChild && ['retired', 'expired'].includes(authentication.state)
      && authentication.authentication_attempt?.authentication_attempt_id === intent.selected.attempt;
    if (!intent.opened || !(intent.confirmed || admitted || withdrawn || terminal)) return;
    this.focusIntent = null;
    // The native owner additionally requires Chrome or this window foreground.
    // Window focus has no bearing on authentication or operation completion.
    try { void Promise.resolve(this.focusWindow()).catch(() => undefined); } catch { /* Presentation only. */ }
  }

  markLaunch(operationId, phase) {
    const timing = this.clickTiming;
    if (!timing || timing.operationId !== operationId || timing.marked.has(phase)) return;
    timing.marked.add(phase);
    timing.observe(phase);
  }

  browserTaskPrepared(task) {
    this.markLaunch(task.operation_id, 'task-found');
    const intent = this.focusIntent;
    if (!intent || intent.operationId !== task.operation_id || intent.epoch !== this.launchEpoch) return;
    if (intent.journey !== null && intent.journey !== task.journey_id
      || this.value?.journey_id !== null && this.value?.journey_id !== task.journey_id) {
      this.focusIntent = null; return;
    }
    intent.journey = task.journey_id;
    intent.deadline = Math.min(intent.deadline, task.expires_at * 1000);
    this.returnFocus();
  }

  headers(mutation = false) {
    const operator = mutation ? this.operatorCsrf() : null;
    return { 'X-Desktop-Generation': this.generation,
      ...(operator ? { 'X-CSRF-Token': operator } : {}),
      ...(mutation ? { 'Content-Type': 'application/json', 'X-Desktop-CSRF': this.session.csrf_token } : {}) };
  }

  async json(path, body) {
    const read = body === undefined, controller = read ? new AbortController() : null;
    const parent = read ? this.controller?.signal : null;
    let timer, cancel = () => {};
    const cancellation = read ? new Promise((_, reject) => {
      cancel = () => { controller.abort(); reject(new Error('request_unconfirmed')); };
      parent?.addEventListener('abort', cancel, { once: true });
      timer = setTimeout(cancel, 3000);
      if (parent?.aborted) cancel();
    }) : null;
    let response, text;
    try {
      const request = this.request(path, { credentials: 'same-origin', redirect: 'error', cache: 'no-store',
        headers: this.headers(!read), ...(read ? { signal: controller.signal } : { method: 'POST', body: JSON.stringify(body) }) })
        .then(async response => ({ response, text: await response.text() }));
      ({ response, text } = await (read ? Promise.race([request, cancellation]) : request));
    } finally {
      clearTimeout(timer); parent?.removeEventListener('abort', cancel);
    }
    if (text.length > 16384) throw new Error('invalid_response');
    if (!response.ok) {
      const error = new Error(response.status === 401 ? 'session_unavailable'
        : response.status === 403 ? 'authorization_unavailable' : 'request_unconfirmed');
      if (response.status === 409) {
        try { error.reason = JSON.parse(text).reason; } catch { /* Malformed refusals cannot settle work. */ }
      }
      throw error;
    }
    return JSON.parse(text);
  }

  async renewPresentation(signal) {
    let code;
    try {
      code = await this.refreshPresentation(this.generation);
    } catch (error) {
      if (signal.aborted) return;
      const reason = typeof error === 'string' ? error : error?.message;
      // Only a transient loopback transport/channel timeout retries through the
      // bounded reconnect policy; rejected or missing native admission fails closed.
      if (reason === 'handoff_unavailable') throw new Error('status_unavailable');
      throw new Error('session_unavailable');
    }
    if (signal.aborted) return;
    if (!/^[A-Za-z0-9_-]{43}$/u.test(code)) throw Error('session_unavailable');
    const response = await this.request('/api/v1/desktop/entry/consume', {
      credentials: 'same-origin', redirect: 'error', cache: 'no-store', signal,
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }),
    });
    if (signal.aborted) return;
    if (response.status >= 500) throw new Error('status_unavailable');
    if (response.status === 403) throw new Error('authorization_unavailable');
    let session;
    try { session = await response.json(); }
    catch {
      throw new Error(response.ok ? 'session_unavailable' : 'status_unavailable');
    }
    if (signal.aborted) return;
    if (!response.ok || session.role !== 'desktop' || !UUID.test(session.generation)
      || session.generation === this.generation || !['setup', 'runtime'].includes(session.stage)
      || !/^[A-Za-z0-9_-]{43}$/u.test(session.csrf_token)
      || this.value?.journey_id && session.journey_id !== this.value.journey_id) throw Error('session_unavailable');
    const oldKey = this.key;
    this.generation = session.generation;
    this.key = 'desktop-setup-operation:' + this.generation;
    try {
      this.storage.setItem('desktop-presentation', this.generation);
      this.storage.removeItem(oldKey);
      if (this.pending) this.storage.setItem(this.key, this.pending);
    } catch { /* The durable operation remains in Brain. */ }
    if (globalThis.location?.pathname === '/onboarding') {
      globalThis.history?.replaceState(null, '', '/onboarding#presentation=' + this.generation);
    }
    this.value = null;
    this.session = session;
  }

  connect() {
    this.stopped = false;
    this.reconnectAttempt = 0;
    this.reconnectExhausted = false;
    return this._connect();
  }

  wake() {
    if (this.stopped || this.connected || this.connecting || !this.reconnectExhausted) return;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    void this._connect();
  }

  async _connect() {
    this.launchEpoch++;
    this.launchOperation = null;
    this.protectedOpening = null;
    this.protectedLaunchError = null;
    this.providerContinuation = null;
    this.focusIntent = null;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.controller?.abort();
    const controller = new AbortController();
    this.controller = controller;
    this.connected = false;
    this.connecting = true;
    try {
      let session;
      try { session = await this.json('/api/v1/desktop/session'); }
      catch (error) {
        if (error.message !== 'session_unavailable' || controller.signal.aborted) throw error;
        await this.renewPresentation(controller.signal);
        session = this.session;
      }
      if (controller.signal.aborted) return;
      if (session.role !== 'desktop' || session.generation !== this.generation || !['setup', 'runtime'].includes(session.stage)
        || !/^[A-Za-z0-9_-]{43}$/u.test(session.csrf_token)) throw new Error('session_unavailable');
      this.session = session;
      this.browserNotices = [];
      let lastProjection;
      // The server subscribes before its first complete authoritative snapshot.
      await this.stream(this.request, this.headers(), controller.signal, (text, event) => {
        if (controller.signal.aborted) return;
        const frame = event === SCOPED_SETUP_EVENT ? setupFrame(text, this.generation) : null;
        const value = frame?.snapshot ?? snapshot(text, this.generation);
        if (this.value && value.revision < this.value.revision) throw new Error('invalid_snapshot');
        this.browserNotices = currentBrowserNotices(value, frame?.browser_notices);
        if (frame) {
          // A feedback-only update must not drive a launch or continuation.
          const projection = JSON.stringify(value);
          if (lastProjection === projection) { this.notify(); return; }
          lastProjection = projection;
        }
        const deliberate = this.intent?.confirmed === true && this.intent.task !== value.next_task;
        if (deliberate) this.intent = null;
        const oldAuth = this.value?.authentication, nextAuth = value.authentication;
        if (oldAuth?.operation_id !== nextAuth?.operation_id
          || oldAuth?.authentication_attempt?.authentication_attempt_id !== nextAuth?.authentication_attempt?.authentication_attempt_id) {
          this.protectedOpening = null;
          this.protectedLaunchError = null;
        }
        if (value.authority_recovery_browser !== 'ready') this.protectedLaunchError = null;
        this.value = value;
        this.retireCompletedFeedback();
        const preparingSelection = this.intent?.initialAuthentication === true
          && this.intent.operationId === this.pending && this.launchOperation === this.pending
          && value.authentication?.state === 'selection-required' && value.authentication.authentication_attempt === null;
        if ((value.decision && value.decision.operation_id === this.pending)
          || (value.decision_status?.state === 'accepted' && value.decision_status.operation_id === this.pending)
          || (value.authentication?.operation_id === this.pending && value.authentication.actions.select && !preparingSelection)) {
          // The original owner keeps accepted preparation. The presentation is
          // now free to admit the separate explicit confirmation command.
          this.remember(null);
          this.launchOperation = null;
        }
        this.connected = true;
        this.returnFocus();
        this.reconnectAttempt = 0;
        this.reconnectExhausted = false;
        this.notify(null, deliberate);
        if (this.launchOperation) void this.launchBrowser();
        const pendingBefore = this.pending;
        this.continueRecoverySignIn();
        this.continueProviderSelection();
        if (this.pending && this.pending === pendingBefore
          && !value.pending_operations.some(item => item.operation_id === this.pending)) void this.receipt();
      });
    } catch (error) {
      if (!controller.signal.aborted && !this.stopped) {
        this.connected = false;
        this.intent = null;
        this.launchOperation = null;
        this.recoverySignIn = null;
        this.providerContinuation = null;
        this.protectedOpening = null;
        this.protectedLaunchError = null;
        const message = typeof error === 'string' ? error : error?.message;
        const terminal = ['session_unavailable', 'authorization_unavailable'].includes(message);
        if (terminal) {
          this.reconnectExhausted = false;
          this.notify(message);
        } else {
          const delay = RECONNECT_DELAYS[this.reconnectAttempt];
          if (delay !== undefined) {
            this.reconnectAttempt++;
            this.reconnectExhausted = false;
            this.notify(message === 'stream_expired' ? null : 'status_unavailable');
            this.reconnectTimer = setTimeout(() => {
              this.reconnectTimer = null;
              if (!controller.signal.aborted && !this.stopped) void this._connect();
            }, delay);
          } else {
            this.reconnectExhausted = true;
            this.notify('connection_unavailable');
            this.reconnectTimer = setTimeout(() => {
              this.reconnectTimer = null;
              if (!controller.signal.aborted && !this.stopped) void this._connect();
            }, RECONNECT_SLOW_MS);
          }
        }
      }
    } finally {
      if (this.controller === controller) this.connecting = false;
    }
  }

  remember(operationId) {
    this.pending = operationId;
    try {
      if (operationId) this.storage.setItem(this.key, operationId);
      else this.storage.removeItem(this.key);
    } catch { /* A replacement presentation recovers accepted work from Brain. */ }
  }

  async launchProtectedBrowser() {
    const snapshot = this.value, auth = snapshot?.authentication;
    const parent = auth?.operation_id ?? null;
    if (this.protectedOpening || !this.connected || snapshot?.authority_recovery_browser !== 'ready'
      || !auth?.actions.open_browser) return outcome('not-dispatched', parent, 'browser_unavailable');
    const presented = presentationGuard(this);
    const token = {};
    const current = () => presented() && this.protectedOpening === token
      && this.value?.authority_recovery === 'sign-in-required'
      && this.value.authentication?.operation_id === parent
      && this.value.authentication.authentication_attempt?.authentication_attempt_id
        === auth.authentication_attempt?.authentication_attempt_id;
    this.protectedOpening = token;
    this.protectedLaunchError = null;
    this.providerContinuation = null;
    this.notify();
    let claimAttempted = false;
    try {
      // Check both outcomes before spending or reporting a one-use claim.
      const invoke = globalThis.__TAURI__?.core?.invoke;
      const available = invoke && await invoke('desktop_chrome_available') === true;
      if (!current() || this.value.authority_recovery_browser !== 'ready') return outcome('superseded', parent);
      if (!available) {
        this.protectedLaunchError = 'chrome_unavailable';
        return outcome('not-dispatched', parent, 'chrome_unavailable');
      }
      claimAttempted = true;
      const opened = await openProtectedBrowser(this);
      if (!current()) return outcome('superseded', parent);
      return opened ? outcome('accepted', parent) : outcome('unconfirmed', parent, 'browser_unavailable');
    } catch {
      if (!current()) return outcome('superseded', parent);
      return outcome(claimAttempted ? 'unconfirmed' : 'not-dispatched', parent, 'browser_unavailable');
    } finally {
      if (this.protectedOpening === token) {
        const reattach = claimAttempted && current() && this.value.authority_recovery_browser === 'ready';
        this.protectedOpening = null;
        // A read-only reattach cannot spend another launch claim.
        if (reattach) void this.connect();
        this.notify();
      }
    }
  }

  /**
   * Runs the next step of an explicit protected sign-in once its owner offers it.
   * Each step runs once for its own parent and attempt. A retired, cancelled or
   * claimed sign-in, a reconnect, another action or the time bound ends the
   * sequence, and the customer then acts again.
   */
  continueRecoverySignIn() {
    const step = this.recoverySignIn, value = this.value, auth = value?.authentication;
    if (!step) return;
    const attempt = auth?.authentication_attempt?.authentication_attempt_id ?? null;
    const recovery = value?.authority_recovery;
    const live = this.connected && step.epoch === this.launchEpoch && this.now() <= step.until
      && (['preparing', 'checking', 'sign-in-required'].includes(recovery)
        || recovery === 'available' && !step.selected && this.pending === step.parent)
      && (!auth || auth.operation_id === step.parent)
      && !(auth?.state === 'retired' && attempt !== step.attempt)
      && !(auth?.state === 'selected' && (!step.selected || value.authority_recovery_browser !== 'ready'));
    if (!live) {
      this.recoverySignIn = null;
      this.notify();
      return;
    }
    if (recovery !== 'sign-in-required' || !auth) return;
    if (!step.selected) {
      if (auth.state === 'selection-required' && attempt === null && auth.actions.select && auth.choices.default
          && value.allowed_actions.includes('auth.select')) {
        void this.act('auth.select', { recover_authority: true, provider: 'default' }, step).catch(() => {
          if (this.recoverySignIn === step) { this.recoverySignIn = null; this.notify(); }
        });
      }
      return;
    }
    if (auth.state === 'selected' && attempt !== step.attempt && auth.actions.open_browser && !this.protectedOpening) {
      this.recoverySignIn = null;
      void this.launchProtectedBrowser().catch(() => {});
    }
  }

  /**
   * Sends the clicked provider selection once after a ceremony replacement or
   * prepared-source continuation adopts its successor for the same journey and
   * parent. Any reconnect, other command, changed journey or parent, missing
   * provider, refusal or two-minute bound ends the sequence.
   */
  continueProviderSelection() {
    const step = this.providerContinuation, value = this.value, auth = value?.authentication;
    if (!step) return;
    if (step.kind === 'browser-recheck') {
      this.continueBrowserRecheck(step);
      return;
    }
    const sameParentPending = value?.pending_operations.some(item =>
      item.operation_id === step.parent && item.action === 'account.connect');
    const foreignPending = value?.pending_operations.some(item =>
      item.action === 'account.connect' && item.operation_id !== step.parent
      || ['auth.select', 'auth.switch', 'auth.cancel'].includes(item.action)
        && item.operation_id !== step.replaceOperation);
    const replacementPending = value?.pending_operations.some(item => item.operation_id === step.replaceOperation);
    const awaitingAcceptance = this.pending === step.replaceOperation
      && (step.kind === 'ceremony'
        ? auth?.state === 'replacement-required' && auth.operation_id === step.parent
        : Boolean(value?.continuation_available && (!auth || auth.state === 'replacement-required' && auth.operation_id === null)));
    const replacing = auth?.state === 'replacement-pending' && auth.operation_id === step.parent;
    const adopting = auth === null && sameParentPending;
    const ready = auth?.state === 'selection-required' && auth.operation_id === step.parent
      && auth.authentication_attempt === null && auth.browser_operation_id === null;
    const live = this.connected && step.epoch === this.launchEpoch && this.now() <= step.until
      && value?.journey_id === step.journey && value.next_task === 'account-connection'
      && value.authority_recovery === undefined && !value.decision && !foreignPending
      && (awaitingAcceptance || replacing || adopting || ready);
    if (!live) {
      this.providerContinuation = null;
      this.notify();
      return;
    }
    if (!ready) return;
    if (!auth.choices?.[step.provider]) {
      this.providerContinuation = null;
      this.notify();
      return;
    }
    if (!auth.actions.select || !value.allowed_actions.includes('auth.select')) {
      if (!replacementPending) {
        this.providerContinuation = null;
        this.notify();
      }
      return;
    }
    this.providerContinuation = null;
    if (this.pending === step.replaceOperation && !replacementPending) this.remember(null);
    void this.act('auth.select', { provider: step.provider }).catch(() => {
      this.notify();
    });
  }

  recheckProvider(operationId) {
    const value = this.value, auth = value?.authentication;
    if (this.session?.stage !== 'setup' || value?.next_task !== 'account-connection'
      || !value.allowed_actions.includes('browser.access') || auth?.state !== 'selected'
      || auth.browser_operation_id !== operationId || !auth.actions.open_browser
      || !this.browserNotices.some(notice => notice.reason === 'browser_recheck_required' && notice.operation_id === operationId)) return null;
    return auth.authentication_attempt?.provider ?? null;
  }

  continueBrowserRecheck(step) {
    const value = this.value, auth = value?.authentication;
    const live = this.connected && step.epoch === this.launchEpoch && this.now() <= step.until
      && value?.journey_id === step.journey && value.authority_recovery === undefined && !value.decision
      && ['browser-access', 'account-connection'].includes(value.next_task)
      && value.pending_operations.some(item => item.action === 'account.connect' && item.operation_id === step.parent)
      && (!auth || auth.operation_id === step.parent);
    if (!live) {
      this.providerContinuation = null;
      this.notify();
      return;
    }
    if (!step.completed || value.next_task !== 'account-connection'
      || value.pending_operations.some(item => item.operation_id === step.replaceOperation)) return;
    this.providerContinuation = null;
    if (auth?.state === 'retired' && auth.authentication_attempt?.authentication_attempt_id === step.attempt
      && auth.choices?.[step.provider] && auth.actions.select && value.allowed_actions.includes('auth.select')
      && !value.pending_operations.some(item => ['auth.select', 'auth.switch', 'auth.cancel'].includes(item.action))) {
      void this.act('auth.select', { provider: step.provider }).catch(() => this.notify());
    } else this.notify();
  }

  async act(action, options = {}, continued = null) {
    const clicked = performance.now();
    const observe = phase => {
      void this.json('/api/v1/desktop/setup/timing', { operation: 'frontend', phase, status: 'ok',
        elapsed_ms: Math.max(0, Math.round(performance.now() - clicked)) }).catch(() => {});
    };
    const authentication = this.value?.authentication;
    const recheckProvider = action === 'browser.access' && continued?.recheckOperation
      ? this.recheckProvider(continued.recheckOperation) : null;
    if (continued?.recheckOperation && (!recheckProvider || this.providerContinuation?.kind === 'browser-recheck')) {
      return outcome('not-dispatched', null, 'operation_pending');
    }
    const replacesCurrentAuthentication = this.value?.allowed_actions.includes(action)
      && (options.recover_authority === true && this.value.authority_recovery === 'sign-in-required'
          && ['auth.select', 'auth.cancel'].includes(action) && authentication?.operation_id === this.pending
        || action === 'auth.select' && options.replace_ceremony === true
        && authentication?.state === 'replacement-required' && authentication.operation_id === this.pending
        || ['auth.cancel', 'auth.switch'].includes(action)
        && [authentication?.operation_id, authentication?.browser_operation_id].includes(this.pending)
        || action === 'auth.select' && authentication?.state === 'selection-required'
        && authentication.actions.select && authentication.operation_id === this.pending && !this.intent?.initialAuthentication
        || action === 'auth.select' && authentication?.state === 'reauthentication-required'
        && authentication.actions.select && authentication.browser_operation_id === this.pending);
    const replacesReview = action === 'activation.review' && this.value?.allowed_actions.includes(action)
      && this.value.pending_operations.some(item => item.action === action)
      && this.value.pending_operations.some(item => item.operation_id === this.pending
        && ['activation.review', 'activation.confirm', 'auth.select', 'auth.switch', 'auth.cancel'].includes(item.action));
    const replacesBrowser = recheckProvider && [authentication.operation_id, continued.recheckOperation].includes(this.pending);
    if (!this.connected || !this.value || this.protectedOpening
      || (this.pending && !replacesCurrentAuthentication && !replacesReview && !replacesBrowser)) {
      return outcome('not-dispatched', null, 'action_unavailable');
    }
    observe('click');
    const intent = command(this.value, action, this.uuid(), options);
    // Later launch steps of this click are recorded once each.
    this.clickTiming = { operationId: intent.operation_id, observe, marked: new Set() };
    this.operationError = null;
    this.protectedLaunchError = null;
    this.launchEpoch++;
    this.focusIntent = null;
    const epoch = this.launchEpoch;
    const current = presentationGuard(this);
    this.intent = { task: this.value.next_task, action, operationId: intent.operation_id, confirmed: false,
      initialAuthentication: action === 'account.connect' && intent.provider !== undefined };
    this.remember(intent.operation_id);
    // An explicit protected sign-in continues through the default selection and
    // the Chrome launch. Neither later step needs its own click.
    this.recoverySignIn = intent.recover_authority && ['account.connect', 'auth.select'].includes(action)
      ? { epoch, parent: action === 'account.connect' ? intent.operation_id : authentication.operation_id,
          selected: action === 'auth.select', attempt: authentication?.authentication_attempt?.authentication_attempt_id ?? null,
          until: continued?.until ?? this.now() + RECOVERY_SIGN_IN_MS }
      : null;
    const sourceContinuation = action === 'account.connect' && this.value.continuation_available
      && (!authentication || authentication.state === 'replacement-required');
    const continueProvider = typeof continued?.continueProvider === 'string' ? continued.continueProvider : null;
    const canContinueProvider = Boolean(continueProvider && this.value.journey_id !== null
      && authentication?.state === 'replacement-required' && authentication.choices?.[continueProvider] === true
      && (action === 'auth.select' && intent.replace_ceremony === true && authentication.operation_id !== null
        || sourceContinuation && authentication.operation_id === null && intent.provider === undefined));
    this.providerContinuation = canContinueProvider
      ? { epoch, journey: this.value.journey_id,
          parent: action === 'account.connect' ? intent.operation_id : authentication.operation_id,
          provider: continueProvider, kind: action === 'account.connect' ? 'source' : 'ceremony',
          replaceOperation: intent.operation_id, until: this.now() + RECOVERY_SIGN_IN_MS }
      : null;
    if (recheckProvider) {
      this.providerContinuation = { epoch, journey: this.value.journey_id, parent: authentication.operation_id,
        attempt: authentication.authentication_attempt.authentication_attempt_id, provider: recheckProvider,
        kind: 'browser-recheck', replaceOperation: intent.operation_id, until: this.now() + RECOVERY_SIGN_IN_MS };
    }
    if (this.openBrowser && !intent.replace_ceremony && !intent.recover_authority && !sourceContinuation && ['browser.access', 'account.connect', 'browser.pair', 'auth.select', 'auth.switch'].includes(action)) this.launchOperation = intent.operation_id;
    if (action === 'auth.cancel' || action === 'activation.review' || intent.replace_ceremony || intent.recover_authority || sourceContinuation) this.launchOperation = null;
    this.notify();
    try {
      observe('command-send');
      await this.json('/api/v1/desktop/setup/commands', intent);
    } catch (error) {
      if (!current()) return outcome('superseded', intent.operation_id);
      this.recoverySignIn = null;
      this.providerContinuation = null;
      if (['setup_changed', 'action_unavailable', 'authority_unavailable', 'account_mismatch', 'operation_limit_reached'].includes(error.reason)) {
        if (this.pending === intent.operation_id) this.remember(null);
        this.intent = null;
        this.launchOperation = null;
        this.notify('operation_refused');
        void this.connect();
        return outcome('refused', intent.operation_id, 'operation_refused');
      }
      return this.unconfirmed(intent.operation_id);
    }
    if (!current()) return outcome('superseded', intent.operation_id);
    // Reading the exact receipt and opening its browser may overlap.
    const [settled] = await Promise.all([
      this.receipt(intent.operation_id, current),
      this.launchOperation === intent.operation_id ? this.launchBrowser() : null,
    ]);
    return current() ? settled : outcome('superseded', intent.operation_id);
  }

  unconfirmed(operationId) {
    if (this.pending === operationId) {
      if (this.launchOperation === operationId) this.launchOperation = null;
      this.notify('operation_unconfirmed');
    }
    return outcome('unconfirmed', operationId, 'operation_unconfirmed');
  }

  async receipt(operationId = this.pending, current = presentationGuard(this)) {
    if (!operationId) return outcome('not-dispatched', null, 'operation_unavailable');
    let result;
    try { result = operation(await this.json('/api/v1/desktop/setup/operations/' + operationId)); }
    catch { return current() ? this.unconfirmed(operationId) : outcome('superseded', operationId); }
    if (!current()) return outcome('superseded', operationId);
    if (result.operation_id !== operationId) return this.unconfirmed(operationId);
    if (result.state === 'outcome_unknown' && this.launchOperation === operationId) this.launchOperation = null;
    if (['completed', 'refused'].includes(result.state) && this.pending === operationId) {
      const completed = result.state === 'completed';
      if (completed) {
        this.returnFocus(operationId);
        if (this.operationError?.operationId === operationId) this.operationError = null;
      } else if (['auth.select', 'auth.switch'].includes(result.action) && ['none', 'operation_expired'].includes(result.reason)) {
        this.returnFocus(operationId, true);
      }
      this.remember(null);
      if (this.launchOperation === operationId) this.launchOperation = null;
      const intent = this.intent?.operationId === operationId ? this.intent : null;
      if (intent) intent.confirmed = completed;
      const deliberate = intent?.confirmed === true && this.value?.next_task !== intent.task;
      if (intent && (deliberate || !completed)) this.intent = null;
      if (!completed) {
        if (this.providerContinuation?.replaceOperation === operationId) this.providerContinuation = null;
        this.operationFeedback(operationId, result.action, result.reason);
      }
      this.notify(null, deliberate);
      const continuation = this.providerContinuation;
      if (completed && result.action === 'browser.access' && continuation?.kind === 'browser-recheck'
        && continuation.replaceOperation === operationId) {
        continuation.completed = true;
        this.continueProviderSelection();
      }
    }
    const state = result.state === 'waiting' ? 'accepted' : result.state === 'outcome_unknown' ? 'unconfirmed' : result.state;
    return outcome(state, operationId, result.reason === 'none' ? null : result.reason);
  }

  async reconcile(selectedOperation = null) {
    const operationId = selectedOperation ?? (this.pending || this.value?.pending_operations[0]?.operation_id);
    if (!this.connected || !operationId || selectedOperation !== null && (!UUID.test(selectedOperation)
      || !this.value?.pending_operations.some(value => value.operation_id === selectedOperation))) {
      return outcome('not-dispatched', operationId ?? null, 'operation_unavailable');
    }
    const current = presentationGuard(this);
    try { await this.json('/api/v1/desktop/setup/operations/' + operationId + '/reconcile', {}); }
    catch { return current() ? this.unconfirmed(operationId) : outcome('superseded', operationId); }
    if (!current()) return outcome('superseded', operationId);
    return this.receipt(operationId, current);
  }

  async launchBrowser(explicit = false, selectedOperation = null) {
    if (!this.openBrowser || !this.connected) return outcome('not-dispatched', selectedOperation, 'browser_unavailable');
    if (this.value?.authority_recovery !== undefined) {
      this.launchOperation = null;
      return outcome('not-dispatched', selectedOperation, 'browser_unavailable');
    }
    if (this.launching) {
      if (!explicit && this.launchOperation) this.launchRequested = true;
      return outcome('not-dispatched', selectedOperation, 'operation_pending');
    }
    const selected = explicit && this.value?.pending_operations.find(item => item.operation_id === selectedOperation);
    if (explicit && (!UUID.test(selectedOperation ?? '') || !selected) || !explicit && selectedOperation !== null) {
      return outcome('not-dispatched', selectedOperation, 'browser_unavailable');
    }
    const operationId = explicit ? selectedOperation : this.launchOperation;
    if (!operationId) return outcome('not-dispatched', null, 'browser_unavailable');
    if (explicit && this.recheckProvider(operationId)) {
      return this.act('browser.access', {}, { recheckOperation: operationId });
    }
    const epoch = this.launchEpoch, journey = this.value?.journey_id;
    const presented = presentationGuard(this);
    const authChild = explicit && this.value?.authentication?.browser_operation_id === operationId;
    const stillSelected = () => this.value?.journey_id === journey
      && this.value?.pending_operations.some(item => item.operation_id === operationId && item.action === selected.action)
      && (!authChild || this.value?.authentication?.browser_operation_id === operationId && this.value.authentication.actions.open_browser);
    const current = () => presented()
      && this.value?.authority_recovery === undefined
      && (explicit ? stillSelected() : this.launchOperation === operationId);
    this.launching = true;
    this.launchRequested = false;
    if (explicit || !this.focusIntent || this.focusIntent.operationId !== operationId || this.focusIntent.epoch !== epoch) {
      this.focusIntent = { operationId, epoch, generation: this.generation, journey: this.value?.journey_id,
        deadline: Math.min(this.value?.expires_at * 1000 || 0, this.now() + 180000), opened: false, confirmed: false };
    }
    if (explicit && this.operationError?.operationId === operationId) this.operationError = null;
    this.returnFocus();
    this.notify();
    try {
      const opened = await this.openBrowser(this, operationId, undefined, current);
      // The receipt or selected child can advance while the native launch is returning.
      // Its exact focus guard separately handles a verified owner transition.
      if (opened && presented()) this.markLaunch(operationId, 'launch');
      if (opened && presented() && this.focusIntent?.epoch === epoch && this.focusIntent.operationId === operationId) {
        this.focusIntent.opened = true;
        this.returnFocus();
      }
      if (!current()) return outcome('superseded', operationId);
      if (opened) {
        if (this.launchOperation === operationId) { this.launchOperation = null; this.notify(); }
      }
      else if (explicit) this.notify('browser_preparing');
      return outcome('accepted', operationId);
    } catch (error) {
      if (!current()) return outcome('superseded', operationId);
      this.focusIntent = null;
      if (this.launchOperation === operationId) this.launchOperation = null;
      const reason = error === 'chrome_unavailable' || error?.message === 'chrome_unavailable' ? 'chrome_unavailable' : 'browser_unavailable';
      this.operationFeedback(operationId, this.value?.pending_operations.find(value => value.operation_id === operationId)?.action
        ?? (this.intent?.operationId === operationId ? this.intent.action : null), reason);
      this.notify();
      return outcome('unconfirmed', operationId, reason);
    } finally {
      if (this.launchEpoch === epoch && this.launchOperation === operationId && !presented()) this.launchOperation = null;
      this.launching = false;
      if (this.connected && this.launchOperation && (this.launchRequested || this.launchEpoch !== epoch)) void this.launchBrowser();
    }
  }

  stop() {
    this.stopped = true;
    this.connecting = false;
    this.reconnectExhausted = false;
    this.launchEpoch++;
    this.launchOperation = null;
    this.recoverySignIn = null;
    this.providerContinuation = null;
    this.protectedOpening = null;
    this.protectedLaunchError = null;
    this.focusIntent = null;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.controller?.abort();
    this.connected = false;
  }
}
