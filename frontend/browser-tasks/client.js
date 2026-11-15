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
// Reasons a helper reports to Brain before it asks the extension to close it.
const ENDINGS = ['profile_mismatch', 'account_mismatch', 'task_changed', 'task_expired', 'unavailable'];

export class BrowserTaskClient {
  constructor({ generation, receive, request = globalThis.fetch.bind(globalThis), connect = connectExtension,
    reattach = reattachExtension, events = readBrowserTaskEvents, now = Date.now, nativeLaunch = false, wait = openWaiting }) {
    if (!UUID.test(generation)) throw new Error('session_unavailable');
    Object.assign(this, { generation, receive, request, connect, reattach, events, now, wait });
    this.waiting = null; this.preparedLink = null;
    this.link = null; this.expiry = null; this.session = null; this.payload = null; this.serial = Promise.resolve(); this.run = 0;
    this.pollTimer = null; this.stream = null; this.streamTimer = null; this.streaming = false;
    this.pollBase = this.pollDelay = 2000;
    this.nativeLaunch = nativeLaunch === true;
    this.lastLink = null; this.ending = null; this.extensionSeen = false; this.reattached = false;
    this.receive = value => {
      if (value.state !== 'service_unavailable') this.lastPresentation = value;
      receive(value);
      if (ENDINGS.includes(value.state) && !this.ending) this.ending = this.end(value.state).catch(() => undefined);
    };
  }
  /** Report why this helper ended, then ask the extension to close it. An error that would be lost stays shown. */
  async end(state) {
    const reason = state === 'unavailable' && !this.extensionSeen ? 'extension_unavailable' : state;
    let reported = false;
    try { if (this.session) { await this.json('ended', { reason }); reported = true; } } catch { /* not recorded */ }
    // Without an extension nothing can close this tab. It stays as the install surface.
    if (reason === 'extension_unavailable') return;
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
  async json(path, body) {
    const response = await this.request('/api/v1/browser-tasks/' + path, { credentials: 'same-origin', redirect: 'error', cache: 'no-store',
      ...(path === 'timing' ? { keepalive: true } : {}),
      headers: { 'X-Desktop-Generation': this.generation,
        ...(body === undefined ? {} : { 'Content-Type': 'application/json', 'X-Desktop-CSRF': this.session.csrf_token }) },
      ...(body === undefined ? {} : { method: 'POST', body: JSON.stringify(body) }) });
    if (!response.ok) throw new Error([401, 409].includes(response.status) ? 'task_changed' : 'service_unavailable');
    return parseOnboardingJson(await response.text());
  }
  async start() {
    const automatic = this.nativeLaunch;
    this.nativeLaunch = false;
    this.stop(); const run = this.run;
    this.automatic = automatic;
    this.pollUnavailable = false;
    this.lastPresentation = null;
    this.authEpoch = 0;
    this.automaticPending = false;
    this.extensionState = null;
    this.authNotification = null;
    this.authState = null;
    this.pairStarted = false;
    this.pairFinished = false;
    this.pairCodeSent = false;
    this.ending = null;
    this.declared = null;
    this.receive({ state: 'loading' });
    try {
      // The payload read needs no session value, so both reads run together. The payload is used only after the session checks.
      const took = {};
      const timed = path => {
        const begun = globalThis.performance?.now?.();
        return this.json(path).then(value => { took[path] = globalThis.performance?.now?.() - begun; return value; });
      };
      const [session, first] = await Promise.all([timed('session'), timed('payload')]);
      if (run !== this.run) return;
      if (session.role !== 'browser_task' || session.generation !== this.generation
        || !/^[A-Za-z0-9_-]{43}$/u.test(session.csrf_token)) throw new Error('task_changed');
      this.session = session;
      // Each first read records its own round trip; the payload step stays timed from navigation start.
      this.loaded('session-get', took.session);
      this.loaded('payload-get', took.payload);
      this.loaded('payload');
      const payload = await this.prepared(run, first);
      if (run !== this.run || payload === null) return;
      if (!isPayloadProjection(payload) || payload.task.admission_generation !== this.generation || payload.task.task_id !== session.task_id
        || payload.task.journey_id !== session.journey_id) throw new Error('task_changed');
      this.payload = payload;
      // A sign-in popup closes when this read sees its task end.
      this.fallbackPoll = payload.task.purpose === 'browser.authentication' ? AUTHENTICATION_POLL_MS : 2000;
      this.pollBase = this.pollDelay = this.streaming ? STREAM_POLL_MS : this.fallbackPoll;
      if (this.finishCompleted(payload)) return;
      const remaining = payload.task.expires_at * 1000 - this.now();
      if (remaining <= 0 || remaining > 1800000) throw new Error('task_expired');
      this.expiry = setTimeout(() => { this.stop(); this.receive({ state: 'task_expired', payload }); }, remaining);
      const early = this.preparedLink;
      this.preparedLink = null;
      const attach = receive => (typeof early?.attach === 'function' && early.attach(payload, receive) ? early
        : (early?.close?.(), this.connect({ payload, receive })));
      this.link = this.lastLink = attach(value => {
        if (run !== this.run) return;
        // The ceremony has taken over the waiting window, or this sign-in will not open it.
        if (!['ready', 'paused', 'auth_idle'].includes(value.state)) this.releaseWaiting();
        if (value.state !== 'unavailable') this.extensionSeen = true;
        this.extensionState = value.state;
        if (!['ready', 'paused', 'auth_idle'].includes(value.state)) {
          this.automatic = false;
          this.automaticPending = false;
          this.authEpoch++;
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
            if (run === this.run) { this.stop(); this.receive({ state: error.message === 'task_changed' ? 'task_changed'
              : error.message === 'task_expired' ? 'task_expired' : 'unavailable', payload }); }
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
        if (value.state === 'unavailable') {
          // Chrome may deliver disconnect before the port's final refusal.
          // Read admission before attributing that disconnect to the extension.
          this.serial = this.serial.then(async () => {
            if (run !== this.run) return;
            const current = await this.currentPayload();
            if (run === this.run && !this.finishCompleted(current)) this.receive({ state: 'unavailable', payload });
          }).catch(error => {
            if (run === this.run) { this.stop(); this.receive({ state: error.message === 'task_changed' ? 'task_changed' : 'service_unavailable', payload }); }
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
      this.schedulePoll(run);
      // A stream opened during preparation stays this generation's only stream.
      if (!this.stream) this.listen(run);
    } catch (error) {
      if (run === this.run) { this.stop(); this.receive({ state: ['task_changed', 'task_expired'].includes(error.message) ? error.message : 'service_unavailable', payload: this.payload }); }
    }
  }
  /** Waits while Brain prepares the ceremony of an already admitted sign-in task.
   * A sign-in that opens itself shows the extension's waiting window meanwhile. The extension only reads ahead. */
  async prepared(run, first) {
    let current = first;
    const started = this.now();
    const own = isPreparingPayload(first) && first.task.admission_generation === this.generation
      && first.task.task_id === this.session.task_id && first.task.journey_id === this.session.journey_id;
    if (own) this.declared = first;
    if (this.automatic && own) { this.waiting = this.wait({ payload: first }); this.loaded('waiting'); }
    // The extension reads ahead on the port this helper attaches with once the ceremony exists.
    if (own) this.preparedLink = this.connect({ payload: first, prepare: true, receive: () => {} });
    while (isPreparingPayload(current)) {
      if (current.task.admission_generation !== this.generation || current.task.task_id !== this.session.task_id
        || current.task.journey_id !== this.session.journey_id
        || first !== current && ['task_id', 'journey_id', 'operation_id', 'purpose', 'scope_digest', 'source_revision', 'expires_at']
          .some(key => current.task[key] !== first.task[key])) throw new Error('task_changed');
      if (current.task.expires_at * 1000 <= this.now()) throw new Error('task_expired');
      if (this.now() - started >= PREPARING_LIMIT_MS) throw new Error('service_unavailable');
      // The task's stream says when its payload changed. The timer reads only while that stream is down.
      if (!this.stream) this.listen(run);
      await this.preparingWake(this.streaming ? STREAM_POLL_MS : PREPARING_POLL_MS);
      if (run !== this.run) return null;
      const next = await this.json('payload');
      if (run !== this.run) return null;
      if (isPayloadProjection(next) && ['task_id', 'journey_id', 'operation_id', 'purpose', 'scope_digest', 'source_revision', 'expires_at']
        .some(key => next.task[key] !== first.task[key])) throw new Error('task_changed');
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
    this.pollBase = this.pollDelay = healthy ? STREAM_POLL_MS : this.fallbackPoll;
    this.schedulePoll(run);
  }
  frame(run, value) {
    if (run !== this.run) return;
    if (!this.payload) {
      if (value.type === 'task' && value.task_id === this.session?.task_id) this.preparingFrame();
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
    // A frame is only an invalidation. Completion still follows a confirmed payload read.
    this.serial = this.serial.then(async () => {
      if (run !== this.run) return;
      const current = await this.currentPayload();
      if (run === this.run) this.finishCompleted(current);
    }).catch(error => {
      if (run !== this.run || !['task_expired', 'task_changed'].includes(error.message)) return;
      const payload = this.payload;
      this.stop(); this.receive({ state: error.message, payload });
    });
  }
  schedulePoll(run) {
    clearTimeout(this.pollTimer);
    this.pollTimer = setTimeout(async () => {
      if (run !== this.run) return;
      try {
        const value = await this.currentPayload();
        if (run !== this.run || this.finishCompleted(value)) return;
        this.pollDelay = this.pollBase;
        if (this.pollUnavailable && this.lastPresentation) this.receive(this.lastPresentation);
        this.pollUnavailable = false;
      } catch (error) {
        if (run !== this.run) return;
        if (['task_expired', 'task_changed'].includes(error.message)) {
          const payload = this.payload;
          this.stop(); this.receive({ state: error.message, payload }); return;
        }
        this.pollDelay = Math.min(this.pollDelay * 2, 15000);
        this.pollUnavailable = true;
        this.receive({ state: 'service_unavailable', payload: this.payload });
      }
      if (run === this.run) this.schedulePoll(run);
    }, this.pollDelay);
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
    const run = this.run;
    try {
      const payload = await this.currentPayload();
      if (run !== this.run) return false;
      if (this.finishCompleted(payload)) return false;
      return this.link?.authenticate() === true;
    } catch (error) {
      if (run === this.run) { this.stop(); this.receive({ state: error.message === 'task_changed' ? 'task_changed' : 'service_unavailable', payload: this.payload }); }
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
    clearTimeout(this.expiry); clearTimeout(this.pollTimer); clearTimeout(this.streamTimer);
    this.stream?.abort(); this.stream = null; this.streaming = false;
    this.wakePreparing = null; this.preparingChanged = false;
  }
}
