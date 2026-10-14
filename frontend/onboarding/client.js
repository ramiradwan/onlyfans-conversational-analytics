import { command, operation, snapshot, UUID } from './contract.js';
import { setupEvents } from './events.js';

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
    this.launchRequested = false;
    this.focusIntent = null;
    this.value = null;
    this.session = null;
    this.connected = false;
    this.pending = null;
    this.controller = null;
    this.intent = null;
    this.reconnectTimer = null;
    this.reconnectDelay = 1000;
    this.operationError = null;
    this.key = 'desktop-setup-operation:' + generation;
    try {
      const saved = storage.getItem(this.key);
      if (UUID.test(saved)) this.pending = saved;
    } catch { /* Brain remains the durable operation owner. */ }
  }

  notify(error = null, deliberate = false) {
    this.receive({ snapshot: this.value, pending: this.pending, connected: this.connected, error: error || this.operationError?.reason || null, deliberate });
  }

  retireAuthenticationError() {
    const feedback = this.operationError, value = this.value;
    if (!feedback || !value || !['auth.select', 'auth.switch', 'account.connect'].includes(feedback.action)
      || feedback.journey !== value.journey_id) return;
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
    this.retireAuthenticationError();
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

  browserTaskPrepared(task) {
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
    const code = await this.refreshPresentation(this.generation);
    if (signal.aborted) return;
    if (!/^[A-Za-z0-9_-]{43}$/u.test(code)) throw Error('session_unavailable');
    const response = await this.request('/api/v1/desktop/entry/consume', {
      credentials: 'same-origin', redirect: 'error', cache: 'no-store', signal,
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }),
    });
    const session = await response.json();
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

  async connect() {
    this.launchEpoch++;
    this.launchOperation = null;
    this.focusIntent = null;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.controller?.abort();
    const controller = new AbortController();
    this.controller = controller;
    this.connected = false;
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
      // The server subscribes before its first complete authoritative snapshot.
      await this.stream(this.request, this.headers(), controller.signal, (text) => {
        if (controller.signal.aborted) return;
        const value = snapshot(text, this.generation);
        if (this.value && value.revision < this.value.revision) throw new Error('invalid_snapshot');
        const deliberate = this.intent?.confirmed === true && this.intent.task !== value.next_task;
        if (deliberate) this.intent = null;
        this.value = value;
        this.retireAuthenticationError();
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
        this.reconnectDelay = 1000;
        this.notify(null, deliberate);
        if (this.launchOperation) void this.launchBrowser();
        if (this.pending && !value.pending_operations.some(item => item.operation_id === this.pending)) void this.receipt();
      });
    } catch (error) {
      if (!controller.signal.aborted) {
        this.connected = false;
        this.intent = null;
        this.launchOperation = null;
        this.notify(['session_unavailable', 'authorization_unavailable'].includes(error.message) ? error.message : 'status_unavailable');
        if (!['session_unavailable', 'authorization_unavailable'].includes(error.message)) {
          this.reconnectTimer = setTimeout(() => {
            this.reconnectTimer = null;
            if (!controller.signal.aborted) void this.connect();
          }, this.reconnectDelay);
          this.reconnectDelay = Math.min(this.reconnectDelay * 2, 30_000);
        }
      }
    }
  }

  remember(operationId) {
    this.pending = operationId;
    try {
      if (operationId) this.storage.setItem(this.key, operationId);
      else this.storage.removeItem(this.key);
    } catch { /* A replacement presentation recovers accepted work from Brain. */ }
  }

  async act(action, options = {}) {
    const authentication = this.value?.authentication;
    const replacesCurrentAuthentication = ['auth.cancel', 'auth.switch'].includes(action)
      && this.value?.allowed_actions.includes(action)
      && [authentication?.operation_id, authentication?.browser_operation_id].includes(this.pending);
    if (!this.connected || !this.value || (this.pending && !replacesCurrentAuthentication)) return;
    const intent = command(this.value, action, this.uuid(), options);
    this.operationError = null;
    this.launchEpoch++;
    this.focusIntent = null;
    const epoch = this.launchEpoch;
    this.intent = { task: this.value.next_task, action, operationId: intent.operation_id, confirmed: false,
      initialAuthentication: action === 'account.connect' && intent.provider !== undefined };
    this.remember(intent.operation_id);
    if (this.openBrowser && ['browser.access', 'account.connect', 'browser.pair', 'auth.select', 'auth.switch'].includes(action)) this.launchOperation = intent.operation_id;
    if (action === 'auth.cancel') this.launchOperation = null;
    this.notify();
    try {
      await this.json('/api/v1/desktop/setup/commands', intent);
      if (epoch !== this.launchEpoch || !this.connected) return;
      await this.receipt();
      if (epoch !== this.launchEpoch || !this.connected) return;
      if (this.launchOperation) await this.launchBrowser();
    } catch (error) {
      if (epoch !== this.launchEpoch || !this.connected) return;
      if (['setup_changed', 'action_unavailable', 'authority_unavailable', 'account_mismatch', 'operation_limit_reached'].includes(error.reason)) {
        this.remember(null);
        this.intent = null;
        this.launchOperation = null;
        this.notify('operation_refused');
        void this.connect();
      } else { this.launchOperation = null; this.notify('operation_unconfirmed'); }
    }
  }

  async receipt() {
    const operationId = this.pending;
    const epoch = this.launchEpoch;
    if (!operationId) return;
    try {
      const result = operation(await this.json('/api/v1/desktop/setup/operations/' + operationId));
      if (this.pending !== operationId || result.operation_id !== operationId || this.launchEpoch !== epoch) return;
      if (result.state === 'outcome_unknown' && this.launchOperation === operationId) this.launchOperation = null;
      if (['completed', 'refused'].includes(result.state)) {
        if (result.state === 'completed') {
          this.returnFocus(operationId);
          if (this.operationError?.operationId === operationId) this.operationError = null;
        } else if (['auth.select', 'auth.switch'].includes(result.action) && ['none', 'operation_expired'].includes(result.reason)) {
          this.returnFocus(operationId, true);
        }
        this.remember(null);
        if (this.launchOperation === operationId) this.launchOperation = null;
        if (this.intent?.operationId === operationId) this.intent.confirmed = result.state === 'completed';
        const deliberate = this.intent?.confirmed === true && this.value?.next_task !== this.intent.task;
        if (deliberate || result.state === 'refused') this.intent = null;
        if (result.state === 'refused') {
          this.operationFeedback(operationId, result.action, result.reason);
        }
        this.notify(null, deliberate);
      }
    } catch {
      if (this.pending !== operationId || this.launchEpoch !== epoch) return;
      if (this.launchOperation === operationId) this.launchOperation = null;
      this.notify('operation_unconfirmed');
    }
  }

  async reconcile(selectedOperation = null) {
    if (selectedOperation !== null && (!UUID.test(selectedOperation)
      || !this.value?.pending_operations.some(value => value.operation_id === selectedOperation))) throw Error('operation_unavailable');
    const operationId = selectedOperation ?? (this.pending || this.value?.pending_operations[0]?.operation_id);
    if (!this.connected || !operationId) return;
    this.notify();
    try {
      await this.json('/api/v1/desktop/setup/operations/' + operationId + '/reconcile', {});
      await this.receipt();
    } catch { this.notify('operation_unconfirmed'); }
  }

  async launchBrowser(explicit = false, selectedOperation = null) {
    if (!this.openBrowser || !this.connected) return;
    if (this.launching) {
      if (!explicit && this.launchOperation) this.launchRequested = true;
      return;
    }
    if (selectedOperation !== null && (!explicit || !UUID.test(selectedOperation)
      || !this.value?.pending_operations.some(value => value.operation_id === selectedOperation))) throw Error('browser_unavailable');
    const operationId = selectedOperation ?? (explicit ? this.pending || this.value?.pending_operations[0]?.operation_id : this.launchOperation);
    if (!operationId) return;
    const epoch = this.launchEpoch;
    const current = () => this.connected && this.launchEpoch === epoch && (explicit || this.launchOperation === operationId);
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
      if (opened && this.connected && this.focusIntent?.epoch === epoch && this.focusIntent.operationId === operationId) {
        this.focusIntent.opened = true;
        this.returnFocus();
      }
      if (!current()) return;
      if (opened) {
        if (this.launchOperation === operationId) this.launchOperation = null;
      }
      else if (explicit) this.notify('browser_preparing');
    } catch (error) {
      if (!current()) return;
      this.focusIntent = null;
      if (this.launchOperation === operationId) this.launchOperation = null;
      const reason = error === 'chrome_unavailable' || error?.message === 'chrome_unavailable' ? 'chrome_unavailable' : 'browser_unavailable';
      this.operationFeedback(operationId, this.value?.pending_operations.find(value => value.operation_id === operationId)?.action
        ?? (this.intent?.operationId === operationId ? this.intent.action : null), reason);
      this.notify();
    } finally {
      this.launching = false;
      if (this.connected && this.launchOperation && (this.launchRequested || this.launchEpoch !== epoch)) void this.launchBrowser();
    }
  }

  stop() {
    this.launchEpoch++;
    this.launchOperation = null;
    this.focusIntent = null;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.controller?.abort();
    this.connected = false;
  }
}
