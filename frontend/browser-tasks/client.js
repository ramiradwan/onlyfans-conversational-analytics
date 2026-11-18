import { isPayloadProjection, isPreparingPayload, UUID } from '../../shared/browser-tasks/wire.mjs';
import { parseOnboardingJson } from '../../shared/onboarding/json.mjs';
import { connectExtension, openWaiting, reattachExtension } from './extension.js';
import { readBrowserTaskEvents } from './events.js';

const AUTHENTICATION_POLL_MS = 500;
// A sign-in task can open before its ceremony exists. Its helper reads again until the ceremony arrives.
export const PREPARING_POLL_MS = 100;
export const PREPARING_LIMIT_MS = 120000;
// While the helper stream is healthy the payload read is only a safety check.
export const STREAM_POLL_MS = 15000;
const STREAM_RETRY_CAP_MS = 5000;
// An extension that is reloading may miss a helper's request to close. It is asked again, a few times.
export const CLOSE_RETRY_MS = 1000;
const CLOSE_ATTEMPTS = 3;
const INITIAL_READ_TIMEOUT_MS = 3000;
const INITIAL_READ_DELAYS = [1000, 2000, 4000];

// Read failures are not decisions about the underlying authentication attempt.
// Browser-task routes use 401/409 for an unavailable or replaced admission,
// 400 for invalid requests, and 503 for a temporarily unavailable owner.
class BrowserTaskReadError extends Error {
  constructor(code, status = null) {
    super(['task_changed', 'task_expired', 'invalid_response'].includes(code) ? code : 'service_unavailable');
    this.code = code;
    this.status = status;
  }
}
function requestFailure(status) {
  if (status === 401 || status === 409) return new BrowserTaskReadError('task_changed', status);
  if (status === 400) return new BrowserTaskReadError('invalid_response', status);
  if (status >= 400 && status < 500) return new BrowserTaskReadError('request_refused', status);
  if (status >= 500 && status < 600) return new BrowserTaskReadError('owner_unavailable', status);
  return new BrowserTaskReadError('invalid_response', status);
}
function temporaryReadFailure(error) {
  return error instanceof BrowserTaskReadError
    ? ['read_timeout', 'owner_unavailable'].includes(error.code)
    : error instanceof TypeError;
}

function waitForInitialRead(delay, signal) {
  return new Promise(resolve => {
    const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve(); };
    const timer = setTimeout(finish, delay);
    if (signal.aborted) finish();
    else signal.addEventListener('abort', finish, { once: true });
  });
}
// Reasons a helper reports to Brain before it asks the extension to close it.
const ENDINGS = ['profile_mismatch', 'account_mismatch', 'task_changed', 'task_expired', 'unavailable', 'auth_access_required', 'extension_disconnected'];
// Why the extension cannot open a sign-in, as the desktop shows it.
const ACCESS_REASONS = { 'needs-account': 'needs_account', 'needs-consent': 'needs_consent', 'needs-permission': 'needs_permission' };

export class BrowserTaskClient {
  constructor({ generation, receive, request = globalThis.fetch.bind(globalThis), connect = connectExtension,
    reattach = reattachExtension, events = readBrowserTaskEvents, now = Date.now, nativeLaunch = false, wait = openWaiting }) {
    if (!UUID.test(generation)) throw new Error('session_unavailable');
    Object.assign(this, { generation, receive, request, connect, reattach, events, now, wait });
    this.waiting = null; this.preparedLink = null; this.stages = [];
    this.link = null; this.expiry = null; this.session = null; this.payload = null; this.serial = Promise.resolve(); this.run = 0;
    this.pollTimer = null; this.stream = null; this.streamTimer = null; this.streaming = false;
    this.initialReadController = null;
    this.pollBase = this.pollDelay = 2000;
    this.nativeLaunch = nativeLaunch === true;
    this.lastLink = null; this.ending = null; this.extensionSeen = false; this.reattached = false;
    this.receive = value => {
      if (!['service_unavailable', 'status_reconnecting'].includes(value.state)) this.lastPresentation = value;
      receive(value);
      if (ENDINGS.includes(value.state) && !this.ending) this.ending = this.end(value.state).catch(() => undefined);
    };
  }
  /** Report why this helper ended, then ask the extension to close it. An error that would be lost stays shown. */
  async end(state) {
    const run = this.run;
    const reason = state === 'extension_disconnected' ? 'unavailable'
      : state === 'unavailable' && !this.extensionSeen ? 'extension_unavailable'
        : state === 'auth_access_required' ? ACCESS_REASONS[this.extensionState] ?? 'unavailable' : state;
    let reported = false;
    try { if (this.session) { await this.json('ended', { reason }); reported = true; } } catch { /* not recorded */ }
    // Never close a newer helper after the original ending report settles.
    if (run !== this.run || reason === 'extension_unavailable') return;
    // Brain already knows a changed or expired task. Other reasons close only once the desktop has them.
    if (!reported && !['task_changed', 'task_expired'].includes(reason)) return;
    if (this.lastLink?.end?.() === true) return;
    await this.reattachOnce('close');
  }
  async reattachOnce(type) {
    // A helper that ended while its sign-in was prepared closes with the task it read.
    const payload = this.payload ?? (type === 'close' ? this.declared : null);
    if (this.reattached || !payload) return false;
    this.reattached = true;
    for (let attempt = 1; ; attempt++) {
      const done = await this.reattach({ payload, type }).catch(() => false);
      if (done || type !== 'close' || attempt === CLOSE_ATTEMPTS) return done;
      await new Promise(resolve => { setTimeout(resolve, CLOSE_RETRY_MS); });
    }
  }
  /** A sign-in helper the desktop opened records its loading steps, timed from its navigation start unless given. */
  loaded(phase, elapsed = globalThis.performance?.now?.()) {
    if (!this.automatic || !this.session || this.payload !== null || !Number.isFinite(elapsed)) return;
    void this.json('timing', { operation: 'helper', phase, status: 'ok', elapsed_ms: Math.round(elapsed) }).catch(() => {});
  }
  /** Sign-in stage marks of a desktop-opened helper, timed from navigation start and sent together once it attached. */
  stage(phase) {
    const elapsed = globalThis.performance?.now?.();
    if (!this.automatic || !Number.isFinite(elapsed) || this.stages.length >= 8) return;
    this.stages.push({ operation: 'helper', phase, status: 'ok', elapsed_ms: Math.round(elapsed) });
  }
  flushStages() {
    const marks = this.stages.splice(0);
    if (marks.length && this.session) void this.json('timing', { marks }).catch(() => {});
  }
  async json(path, body, signal) {
    const response = await this.request('/api/v1/browser-tasks/' + path, { credentials: 'same-origin', redirect: 'error', cache: 'no-store',
      ...(path === 'timing' ? { keepalive: true } : {}),
      ...(signal ? { signal } : {}),
      headers: { 'X-Desktop-Generation': this.generation,
        ...(body === undefined ? {} : { 'Content-Type': 'application/json', 'X-Desktop-CSRF': this.session.csrf_token }) },
      ...(body === undefined ? {} : { method: 'POST', body: JSON.stringify(body) }) });
    if (!response.ok) throw requestFailure(response.status);
    const text = await response.text();
    try { return parseOnboardingJson(text); }
    catch {
      // An invalid response is never a transient connection failure.
      throw new BrowserTaskReadError('invalid_response');
    }
  }
  async initialRead(path, signal, {
    deadline = Infinity, deadlineCode = 'preparation_unavailable',
    onRetry = () => this.receive({ state: 'status_reconnecting' }),
  } = {}) {
    // Read-only session/payload recovery. Preparation uses the same budget
    // without restarting the admitted task, port or authentication ceremony.
    const check = () => {
      if (signal.aborted) throw new BrowserTaskReadError('cancelled');
      if (deadline <= this.now()) throw new BrowserTaskReadError(deadlineCode);
    };
    for (let attempt = 0; ; attempt++) {
      check();
      const controller = new AbortController();
      let timeout, cancel;
      try {
        const cancellation = new Promise((_, reject) => {
          cancel = () => {
            controller.abort();
            reject(new BrowserTaskReadError('cancelled'));
          };
          signal.addEventListener('abort', cancel, { once: true });
          const remaining = deadline - this.now();
          timeout = setTimeout(() => {
            reject(new BrowserTaskReadError(remaining <= INITIAL_READ_TIMEOUT_MS ? deadlineCode : 'read_timeout'));
            controller.abort();
          }, Math.min(INITIAL_READ_TIMEOUT_MS, remaining));
        });
        const value = await Promise.race([this.json(path, undefined, controller.signal), cancellation]);
        check();
        return value;
      } catch (error) {
        check();
        if (!temporaryReadFailure(error) || attempt >= INITIAL_READ_DELAYS.length) throw error;
        onRetry();
      } finally {
        clearTimeout(timeout);
        signal.removeEventListener('abort', cancel);
      }
      await waitForInitialRead(Math.min(INITIAL_READ_DELAYS[attempt], deadline - this.now()), signal);
    }
  }
  async start() {
    const automatic = this.nativeLaunch;
    this.nativeLaunch = false;
    this.stop(); const run = this.run;
    const controller = this.initialReadController = new AbortController();
    this.automatic = automatic;
    this.portEpoch = 0;
    this.extensionSeen = false;
    this.reattached = false;
    this.lastLink = null;
    this.session = null;
    this.payload = null;
    this.statusUnavailable = false;
    this.statusReadSequence = 0;
    this.lastPresentation = null;
    this.authEpoch = 0;
    this.automaticPending = false;
    this.extensionState = null;
    this.authNotification = null;
    this.authState = null;
    this.openedSent = false;
    this.pairStarted = false;
    this.pairFinished = false;
    this.pairCodeSent = false;
    this.ending = null;
    this.declared = null;
    this.stages = [];
    this.receive({ state: 'loading' });
    try {
      // The payload read needs no session value, so both reads run together. The payload is used only after the session checks.
      const took = {};
      const initialAbort = new AbortController();
      const abortInitial = () => initialAbort.abort();
      controller.signal.addEventListener('abort', abortInitial, { once: true });
      const timed = path => {
        const begun = globalThis.performance?.now?.();
        return this.initialRead(path, initialAbort.signal).then(value => {
          took[path] = globalThis.performance?.now?.() - begun;
          return value;
        }, error => {
          initialAbort.abort();
          throw error;
        });
      };
      let session, first;
      try {
        [session, first] = await Promise.all([timed('session'), timed('payload')]);
      } finally {
        controller.signal.removeEventListener('abort', abortInitial);
        initialAbort.abort();
      }
      if (run !== this.run) return;
      if (session.role !== 'browser_task' || session.generation !== this.generation
        || !/^[A-Za-z0-9_-]{43}$/u.test(session.csrf_token)) throw new Error('task_changed');
      this.session = session;
      // Each first read records its own round trip; the payload step stays timed from navigation start.
      this.loaded('session-get', took.session);
      this.loaded('payload-get', took.payload);
      this.loaded('payload');
      const payload = await this.prepared(run, first, controller.signal);
      if (run !== this.run || payload === null) return;
      if (!isPayloadProjection(payload) || payload.task.admission_generation !== this.generation || payload.task.task_id !== session.task_id
        || payload.task.journey_id !== session.journey_id) throw new Error('task_changed');
      this.payload = payload;
      this.receive({ state: 'loading', payload });
      // A sign-in popup closes when this read sees its task end.
      this.fallbackPoll = payload.task.purpose === 'browser.authentication' ? AUTHENTICATION_POLL_MS : 2000;
      this.pollBase = this.pollDelay = this.streaming ? STREAM_POLL_MS : this.fallbackPoll;
      if (this.finishCompleted(payload)) return;
      const remaining = payload.task.expires_at * 1000 - this.now();
      if (remaining <= 0 || remaining > 1800000) throw new Error('task_expired');
      this.expiry = setTimeout(() => { this.stop(); this.receive({ state: 'task_expired', payload }); }, remaining);
      const early = this.preparedLink;
      this.preparedLink = null;
      const portEpoch = ++this.portEpoch;
      const attach = receive => (typeof early?.attach === 'function' && early.attach(payload, receive) ? early
        : (early?.close?.(), this.connect({ payload, receive })));
      const attaching = attach(value => {
        if (run !== this.run || portEpoch !== this.portEpoch) return;
        // The ceremony has taken over the waiting window, or this sign-in will not open it.
        if (!['ready', 'paused', 'auth_idle'].includes(value.state)) this.releaseWaiting();
        if (!['unavailable', 'connection_lost', 'invalid_response', 'extension_refused'].includes(value.state) || value.observation) {
          this.extensionSeen = true;
        }
        this.extensionState = value.state;
        if (payload.task.purpose === 'browser.authentication' && value.state === 'auth_idle' && this.automatic) {
          this.stage('auth-idle');
          this.flushStages();
        }
        if (value.state === 'auth_opening' && payload.task.purpose === 'browser.authentication' && !this.openedSent) {
          // The desktop's backstop learns that this sign-in's popup opened.
          this.openedSent = true;
          void this.json('opened', {}).catch(() => {});
        }
        if (!['ready', 'paused', 'auth_idle'].includes(value.state)) {
          this.automatic = false;
          this.automaticPending = false;
          this.authEpoch++;
        }
        if (value.state === 'auth_idle' && this.authNotification) {
          this.receive({ state: this.authState ?? 'auth_checking', payload });
          return;
        }
        if (value.state === 'auth_idle' && this.automaticPending) {
          this.receive({ state: 'auth_opening', payload });
          return;
        }
        if (value.state === 'pairing_saved' && payload.task.purpose === 'browser.pairing-confirmation') {
          this.receive({ state: 'pairing_saved', payload });
          return;
        }
        if (value.state === 'pairing_completed' && payload.task.purpose === 'browser.pairing-confirmation') {
          if (!this.pairStarted || this.pairFinished) return;
          this.pairFinished = true;
          this.serial = this.serial.then(async () => {
            if (run !== this.run) return;
            const current = await this.currentPayload();
            if (run === this.run && !this.finishCompleted(current)) this.receive({ state: 'pairing_checking', payload });
          }).catch(error => {
            if (run !== this.run) return;
            if (['task_changed', 'task_expired'].includes(error.message)) this.stop();
            this.receive({ state: error.message === 'task_changed' ? 'task_changed'
              : error.message === 'task_expired' ? 'task_expired' : 'pairing_unconfirmed', payload });
          });
          return;
        }
        if (payload.task.purpose === 'browser.authentication' && value.state === 'auth_idle' && this.automatic) {
          this.automatic = false;
          this.automaticPending = true;
          const epoch = this.authEpoch;
          this.receive({ state: 'auth_opening', payload });
          this.serial = this.serial.then(async () => {
            if (run !== this.run) return;
            const current = await this.currentPayload();
            if (run !== this.run || epoch !== this.authEpoch || this.extensionState !== 'auth_idle') return;
            if (this.finishCompleted(current)) return;
            if (this.link?.authenticate() !== true) throw Error('unavailable');
          }).catch(error => {
            if (run === this.run && portEpoch === this.portEpoch) { this.stop(); this.receive({ state: error.message === 'task_changed' ? 'task_changed'
              : error.message === 'task_expired' ? 'task_expired' : 'service_unavailable', payload }); }
          });
          return;
        }
        if (payload.task.purpose === 'browser.pairing-confirmation' && value.state === 'pairing_compare') {
          if (!this.pairStarted || this.pairCodeSent) return;
          this.pairCodeSent = true;
          this.receive({ state: 'pairing_checking', payload });
          this.serial = this.serial.then(async () => {
            if (run !== this.run) return;
            await this.json('pairing', { comparison_code: value.comparison_code });
            if (run === this.run) this.receive({ state: 'pairing_checking', payload });
          }).catch(error => {
            // The comparison may have committed. Keep this exact in-flight
            // handshake alive while bounded reads reconcile its outcome.
            if (run === this.run) {
              if (error.message === 'task_changed') this.stop();
              this.receive({ state: 'pairing_unconfirmed', payload });
            }
          });
          return;
        }
        if (payload.task.purpose === 'browser.pairing-confirmation' && ['ready', 'paused'].includes(value.state)) {
          this.receive({ state: this.pairCodeSent ? 'pairing_checking' : 'pairing_connecting', payload });
          if (this.pairStarted) return;
          this.pairStarted = true;
          // The native action already accepted this exact pairing intent. Check
          // its admission again before asking the extension to open its attempt.
          this.serial = this.serial.then(async () => {
            if (run !== this.run) return;
            await this.currentPayload();
            if (run === this.run && this.link?.pair() !== true) throw Error('unavailable');
          }).catch(error => {
            if (run === this.run) { this.stop(); this.receive({ state: error.message === 'task_changed' ? 'task_changed' : 'pairing_unconfirmed', payload }); }
          });
          return;
        }
        if (['auth_checking', 'auth_closed'].includes(value.state)) {
          if (payload.task.purpose !== 'browser.authentication') return;
          const closed = value.state === 'auth_closed';
          const notification = closed ? 'closed' : JSON.stringify(value.notification);
          if (notification === this.authNotification) { this.receive({ state: this.authState, payload }); return; }
          this.authNotification = notification;
          this.authState = 'auth_checking'; this.receive({ state: this.authState, payload });
          this.serial = this.serial.then(async () => {
            if (run !== this.run) return;
            const acknowledgement = await this.json(closed ? 'closed' : 'notification', closed ? {} : value.notification);
            if (run === this.run) {
              if (closed) {
                // Brain has durably accepted this exact window's closure. Its
                // owner reconciles the child independently of this helper.
                if (acknowledgement?.state !== 'checking' || Object.keys(acknowledgement).length !== 1) throw Error('service_unavailable');
                this.complete('auth_closed', payload);
                return;
              }
              this.authState = value.notification.notification === 'changed' ? 'auth_checking'
                : value.notification.notification === 'retired' ? 'task_changed' : 'auth_unconfirmed';
              this.receive({ state: this.authState, payload });
            }
          }).catch(() => { if (run === this.run) { this.stop(); this.receive({ state: 'service_unavailable', payload }); } });
          return;
        }
        if (value.state === 'invalid_response') {
          this.stop();
          this.receive({ state: 'service_unavailable', payload });
          return;
        }
        if (['unavailable', 'connection_lost', 'extension_refused'].includes(value.state)) {
          // Read admission before attributing a disconnect to the extension.
          // A previously admitted extension or explicit refusal is not absence;
          // desktop owns any follow-up recovery without reopening a second port.
          const disconnected = this.extensionSeen || value.state !== 'unavailable';
          this.serial = this.serial.then(async () => {
            if (run !== this.run || portEpoch !== this.portEpoch) return;
            const current = await this.currentPayload();
            if (run === this.run && portEpoch === this.portEpoch && !this.finishCompleted(current)) {
              this.receive({ state: disconnected ? 'extension_disconnected' : 'unavailable', payload });
            }
          }).catch(error => {
            if (run === this.run && portEpoch === this.portEpoch) { this.stop(); this.receive({ state: error.message === 'task_changed' ? 'task_changed' : 'service_unavailable', payload }); }
          });
          return;
        }
        this.receive({ ...value, state: payload.task.purpose === 'browser.pairing-confirmation'
          && ['needs-consent', 'needs-permission', 'needs-account'].includes(value.state) ? 'pairing_access_required'
          : payload.task.purpose === 'browser.authentication'
          && ['needs-consent', 'needs-permission', 'needs-account', 'ready', 'paused'].includes(value.state)
          ? (['ready', 'paused'].includes(value.state) ? 'loading' : 'auth_access_required') : value.state, payload });
        // Only verified permitted identity is delivered to Brain. Intermediate
        // consent UI states stay in this task while its draft scope is changing.
        if (!['ready', 'paused'].includes(value.state) || !['browser.access', 'browser.identity'].includes(payload.task.purpose)) return;
        this.serial = this.serial.then(async () => {
          if (run !== this.run) return;
          await this.json('observation', value.observation);
          if (run === this.run) {
            // This helper has delivered its observation. Its extension port no
            // longer owns the status of the accepted desktop operation.
            this.complete('reported', payload);
          }
        }).catch(error => { if (run === this.run) { this.stop(); this.receive({ state: error.message, payload }); } });
      });
      this.link = this.lastLink = attaching;
      this.stage('attach-sent');
      this.flushStages();
      this.schedulePoll(run);
      // A stream opened during preparation stays this generation's only stream.
      if (!this.stream) this.listen(run);
    } catch (error) {
      if (run === this.run) {
        const payload = this.payload ?? this.declared;
        this.stop();
        this.receive({ state: ['task_changed', 'task_expired'].includes(error.message) ? error.message : 'service_unavailable', payload });
      }
    } finally {
      if (this.initialReadController === controller) this.initialReadController = null;
    }
  }
  /** Waits while Brain prepares the ceremony of an already admitted sign-in task.
   * A sign-in that opens itself shows the extension's waiting window meanwhile. The extension only reads ahead. */
  async prepared(run, first, signal) {
    let current = first;
    const started = this.now();
    if (!isPreparingPayload(first)) return first;
    if (first.task.admission_generation !== this.generation || first.task.task_id !== this.session.task_id
      || first.task.journey_id !== this.session.journey_id) throw new BrowserTaskReadError('task_changed');
    const expires = first.task.expires_at * 1000;
    const deadline = Math.min(expires, started + PREPARING_LIMIT_MS);
    const deadlineCode = expires <= started + PREPARING_LIMIT_MS ? 'task_expired' : 'preparation_unavailable';
    this.declared = first;
    if (deadline <= this.now()) throw new BrowserTaskReadError(deadlineCode);
    if (this.automatic) { this.waiting = this.wait({ payload: first }); this.loaded('waiting'); }
    // The extension reads ahead on the port this helper attaches with once the ceremony exists.
    this.preparedLink = this.connect({ payload: first, prepare: true, receive: () => {} });
    while (isPreparingPayload(current)) {
      if (deadline <= this.now()) throw new BrowserTaskReadError(deadlineCode);
      // The task's stream says when its payload changed. The timer reads only while that stream is down.
      if (!this.stream) this.listen(run);
      await this.preparingWake(Math.min(this.streaming ? STREAM_POLL_MS : PREPARING_POLL_MS, deadline - this.now()));
      if (run !== this.run || signal.aborted) return null;
      let reconnecting = false;
      const next = await this.initialRead('payload', signal, {
        deadline, deadlineCode,
        onRetry: () => { reconnecting = true; this.receive({ state: 'status_reconnecting', payload: first }); },
      });
      if (run !== this.run || signal.aborted) return null;
      if (!isPreparingPayload(next) && !isPayloadProjection(next)) {
        throw new BrowserTaskReadError('task_changed');
      }
      if (['task_id', 'admission_generation', 'journey_id', 'operation_id', 'purpose', 'scope_digest', 'source_revision', 'expires_at']
        .some(key => next.task[key] !== first.task[key])
        || ['cancelled', 'expired'].includes(next.task.state)) throw new BrowserTaskReadError('task_changed');
      if (isPreparingPayload(next)) {
        if (reconnecting) this.receive({ state: 'loading', payload: first });
      } else {
        this.stage('payload-ready');
      }
      current = next;
    }
    return current;
  }
  /** Resolves on the next frame for this task, or after the delay. A frame during a read is kept for the next wait. */
  preparingWake(delay) {
    if (this.preparingChanged) { this.preparingChanged = false; return Promise.resolve(); }
    return new Promise(resolve => {
      const wake = () => { clearTimeout(this.pollTimer); if (this.wakePreparing === wake) this.wakePreparing = null; resolve(); };
      this.wakePreparing = wake;
      this.pollTimer = setTimeout(wake, delay);
    });
  }
  preparingFrame() {
    if (this.wakePreparing) this.wakePreparing();
    else this.preparingChanged = true;
  }
  listen(run) {
    const controller = new AbortController();
    this.stream = controller;
    let attempt = 0;
    const loop = async () => {
      while (run === this.run) {
        try {
          await this.events({ request: this.request, generation: this.generation, signal: controller.signal,
            open: () => { if (run === this.run) { attempt = 0; this.streamHealth(run, true); } },
            frame: value => this.frame(run, value) });
        } catch { /* reconnect below */ }
        if (run !== this.run) return;
        this.streamHealth(run, false);
        await new Promise(resolve => { this.streamTimer = setTimeout(resolve, Math.min(1000 * 2 ** attempt++, STREAM_RETRY_CAP_MS)); });
      }
    };
    void loop();
  }
  streamHealth(run, healthy) {
    if (run !== this.run || this.streaming === healthy) return;
    this.streaming = healthy;
    if (!this.payload) {
      // A preparing helper whose stream dropped reads again at once, then on its short timer.
      if (!healthy) this.preparingFrame();
      return;
    }
    this.pollBase = healthy ? STREAM_POLL_MS : this.fallbackPoll;
    if (!this.statusUnavailable) this.pollDelay = this.pollBase;
    this.schedulePoll(run);
  }
  frame(run, value) {
    if (run !== this.run) return;
    if (value.type === 'end' && value.task_id === this.session?.task_id) {
      // The desktop gave up on this sign-in and shows its own error. This helper closes.
      const payload = this.payload;
      this.stop(); this.receive({ state: 'task_changed', payload });
      return;
    }
    if (!this.payload) {
      if (value.type === 'task' && value.task_id === this.session?.task_id) { this.stage('stream-wake'); this.preparingFrame(); }
      return;
    }
    if (value.task_id !== this.payload.task.task_id) return;
    if (value.type === 'focus') {
      this.serial = this.serial.then(async () => {
        if (run !== this.run || await this.link?.focus?.() !== true || run !== this.run) return;
        await this.json('focused', {});
      }).catch(() => undefined);
      return;
    }
    // A frame is only an invalidation. Use the same authoritative read as the fallback.
    this.serial = this.serial.then(() => this.reconcileStatus(run));
  }
  async reconcileStatus(run) {
    if (run !== this.run) return;
    const sequence = ++this.statusReadSequence;
    // A delayed safety read must not overwrite the result of a newer invalidation.
    const current = () => run === this.run && sequence === this.statusReadSequence;
    try {
      const value = await this.currentPayload();
      if (!current() || this.finishCompleted(value)) return;
      this.pollDelay = this.pollBase;
      if (this.statusUnavailable) {
        this.statusUnavailable = false;
        if (this.lastPresentation) this.receive(this.lastPresentation);
      }
    } catch (error) {
      if (!current()) return;
      if (['task_expired', 'task_changed'].includes(error.message)) {
        const payload = this.payload;
        this.stop(); this.receive({ state: error.message, payload }); return;
      }
      if (!temporaryReadFailure(error)) {
        const payload = this.payload;
        this.stop(); this.receive({ state: 'service_unavailable', payload }); return;
      }
      this.pollDelay = Math.min((this.statusUnavailable ? this.pollDelay : this.fallbackPoll) * 2, 15000);
      this.statusUnavailable = true;
      this.receive({ state: 'status_reconnecting', payload: this.payload });
    }
    if (current()) this.schedulePoll(run);
  }
  schedulePoll(run) {
    clearTimeout(this.pollTimer);
    this.pollTimer = setTimeout(() => { void this.reconcileStatus(run); }, this.pollDelay);
  }

  async replacePairing() {
    if (this.extensionState !== 'pairing_saved' || this.payload?.task.purpose !== 'browser.pairing-confirmation') return false;
    const run = this.run;
    this.extensionState = 'pairing_connecting';
    this.receive({ state: 'pairing_connecting', payload: this.payload });
    try {
      const value = await this.currentPayload();
      if (run !== this.run || this.finishCompleted(value)) return false;
      this.pairStarted = true;
      if (this.link?.replacePairing() !== true) throw Error('unavailable');
      return true;
    } catch (error) {
      if (run === this.run) { this.stop(); this.receive({ state: error.message === 'task_changed' ? 'task_changed' : 'pairing_unconfirmed', payload: this.payload }); }
      return false;
    }
  }
  async currentPayload() {
    const current = await this.json('payload');
    if (!isPayloadProjection(current) || current.task.admission_generation !== this.generation
      || !this.payload || ['task_id', 'journey_id', 'operation_id', 'purpose', 'scope_digest', 'source_revision', 'expires_at']
        .some(key => current.task[key] !== this.payload.task[key])
      || ['extension_id', 'entry_source', 'creator_account_id'].some(key => current[key] !== this.payload[key])
      || JSON.stringify(current.participant) !== JSON.stringify(this.payload.participant)
      || !(current.task.state === 'completed' && current.ceremony === null)
        && JSON.stringify(current.ceremony) !== JSON.stringify(this.payload.ceremony)
      || ['cancelled', 'expired'].includes(current.task.state)) throw Error('task_changed');
    if (current.task.expires_at * 1000 <= this.now()
      || current.ceremony && Date.parse(current.ceremony.expires_at) <= this.now()) throw Error('task_expired');
    return current;
  }
  finishCompleted(payload) {
    if (payload.task.state !== 'completed') return false;
    if (payload.task.purpose === 'browser.pairing-confirmation' && this.pairStarted && !this.pairFinished) {
      // Brain's comparison commit precedes the extension's durable Noise pin.
      // Disconnecting here would abort the handshake that this helper started.
      this.receive({ state: 'pairing_checking', payload });
      return false;
    }
    this.complete('task_completed', payload);
    return true;
  }
  complete(state, payload) {
    if (state === 'task_completed' && payload.task.purpose === 'browser.authentication' && this.session) {
      void this.json('timing', { operation: 'helper', phase: 'helper-finish', status: 'ok', elapsed_ms: 0 }).catch(() => {});
    }
    const link = this.link;
    this.link = null;
    this.stop();
    this.receive({ state, payload });
    // A helper whose port died earlier makes one fresh attachment to finish.
    void Promise.resolve(link?.finish ? link.finish() : false).catch(() => false)
      .then(finished => {
        link?.close();
        if (finished !== true) return this.reattachOnce(state === 'task_completed' ? 'finish' : 'close');
      });
  }
  async open() {
    const run = this.run;
    try {
      const payload = await this.currentPayload();
      if (run !== this.run) return false;
      if (this.finishCompleted(payload)) return false;
      return this.link?.open() === true;
    } catch (error) {
      if (run === this.run) { this.stop(); this.receive({ state: error.message === 'task_changed' ? 'task_changed' : 'service_unavailable', payload: this.payload }); }
      return false;
    }
  }
  async authenticate() {
    if (this.extensionState !== 'auth_idle' || this.automaticPending || this.authNotification
      || this.statusUnavailable || this.payload?.task.purpose !== 'browser.authentication') return false;
    const run = this.run, epoch = this.authEpoch, portEpoch = this.portEpoch;
    this.automaticPending = true;
    this.receive({ state: 'auth_opening', payload: this.payload });
    try {
      const payload = await this.currentPayload();
      if (run !== this.run || portEpoch !== this.portEpoch || epoch !== this.authEpoch
        || this.extensionState !== 'auth_idle' || this.authNotification) return false;
      if (this.finishCompleted(payload)) return false;
      if (this.link?.authenticate() !== true) throw Error('unavailable');
      return true;
    } catch (error) {
      if (run === this.run && portEpoch === this.portEpoch) { this.stop(); this.receive({ state: error.message === 'task_changed' ? 'task_changed' : 'service_unavailable', payload: this.payload }); }
      return false;
    }
  }
  releaseWaiting() {
    const waiting = this.waiting;
    this.waiting = null;
    waiting?.close();
  }
  stop() {
    this.releaseWaiting();
    this.preparedLink?.close(); this.preparedLink = null;
    this.nativeLaunch = false; this.automatic = false; this.run++; this.link?.close(); this.link = null;
    // A preparing helper can be stopped by its stream while waiting for its
    // next read. Resolve that wait so the retired start() can finish.
    this.wakePreparing?.();
    clearTimeout(this.expiry); clearTimeout(this.pollTimer); clearTimeout(this.streamTimer);
    this.initialReadController?.abort(); this.initialReadController = null;
    this.stream?.abort(); this.stream = null; this.streaming = false;
    this.wakePreparing = null; this.preparingChanged = false;
  }
}
