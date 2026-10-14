import { isPayloadProjection, UUID } from '../../shared/browser-tasks/wire.mjs';
import { parseOnboardingJson } from '../../shared/onboarding/json.mjs';
import { connectExtension } from './extension.js';

export class BrowserTaskClient {
  constructor({ generation, receive, request = globalThis.fetch.bind(globalThis), connect = connectExtension, now = Date.now, nativeLaunch = false }) {
    if (!UUID.test(generation)) throw new Error('session_unavailable');
    Object.assign(this, { generation, receive, request, connect, now });
    this.link = null; this.expiry = null; this.session = null; this.payload = null; this.serial = Promise.resolve(); this.run = 0;
    this.pollTimer = null;
    this.pollDelay = 2000;
    this.nativeLaunch = nativeLaunch === true;
    this.receive = value => {
      if (value.state !== 'service_unavailable') this.lastPresentation = value;
      receive(value);
    };
  }
  async json(path, body) {
    const response = await this.request('/api/v1/browser-tasks/' + path, { credentials: 'same-origin', redirect: 'error', cache: 'no-store',
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
    this.receive({ state: 'loading' });
    try {
      const session = await this.json('session');
      if (run !== this.run) return;
      if (session.role !== 'browser_task' || session.generation !== this.generation
        || !/^[A-Za-z0-9_-]{43}$/u.test(session.csrf_token)) throw new Error('task_changed');
      this.session = session;
      const payload = await this.json('payload');
      if (run !== this.run) return;
      if (!isPayloadProjection(payload) || payload.task.admission_generation !== this.generation || payload.task.task_id !== session.task_id
        || payload.task.journey_id !== session.journey_id) throw new Error('task_changed');
      this.payload = payload;
      if (this.finishCompleted(payload)) return;
      const remaining = payload.task.expires_at * 1000 - this.now();
      if (remaining <= 0 || remaining > 1800000) throw new Error('task_expired');
      this.expiry = setTimeout(() => { this.stop(); this.receive({ state: 'task_expired', payload }); }, remaining);
      this.link = this.connect({ payload, receive: value => {
        if (run !== this.run) return;
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
      } });
      this.schedulePoll(run);
    } catch (error) {
      if (run === this.run) { this.stop(); this.receive({ state: ['task_changed', 'task_expired'].includes(error.message) ? error.message : 'service_unavailable', payload: this.payload }); }
    }
  }
  schedulePoll(run) {
    clearTimeout(this.pollTimer);
    this.pollTimer = setTimeout(async () => {
      if (run !== this.run) return;
      try {
        const value = await this.currentPayload();
        if (run !== this.run || this.finishCompleted(value)) return;
        this.pollDelay = 2000;
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
    const link = this.link;
    this.link = null;
    this.stop();
    this.receive({ state, payload });
    if (link?.finish) void Promise.resolve(link.finish()).catch(() => false).finally(() => link.close());
    else link?.close();
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
  stop() { this.nativeLaunch = false; this.automatic = false; this.run++; this.link?.close(); this.link = null; clearTimeout(this.expiry); clearTimeout(this.pollTimer); }
}
