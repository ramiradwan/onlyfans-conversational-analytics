import { AccessFollower, createWebAuthnApi, EnrollmentOutcomeError, lapsedAccess, LoginOutcomeError, WebAuthnStatusError } from '../shared-setup/webauthn.ts';

const STATUS_RETRY_DELAYS = [1000, 2000, 4000];

/** The native presentation uses the existing WebAuthn owner and session. */
export class DesktopEnrollment {
  constructor({ api = createWebAuthnApi(), receive, refresh }) {
    Object.assign(this, { api, receive, refresh });
    this.state = { checked: false, enrolled: null, authenticated: false, busy: false, checking: false, recoveringStatus: false, error: null, access: null };
    this.controller = null;
    this.watcher = null;
    this.retryTimer = null;
    this.retryCount = 0;
    this.stopped = false;
    // A pushed access change reads the status again without the busy placeholder.
    this.follower = new AccessFollower(() => { void this.check({ quiet: true }); });
  }
  publish(value) { this.state = { ...this.state, ...value }; this.receive(this.state); }
  watch() {
    if (this.watcher || !this.api.watch) return;
    this.watcher = new AbortController();
    void this.api.watch(this.watcher.signal, access => this.follower.push(access));
  }
  // Each read or ceremony holds the follower until it ends. A newer one retires
  // it at once, so the comparison never waits for a retired request to settle.
  start() {
    this.retire();
    const controller = new AbortController(); this.controller = controller;
    this.follower.hold();
    return controller;
  }
  retire() {
    if (!this.controller) return;
    this.controller.abort(); this.controller = null;
    this.follower.abandon();
  }
  scheduleStatusRead(delay) {
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => { void this.check(); }, delay);
  }
  async check({ quiet = false } = {}) {
    if (this.stopped || this.state.busy) return;
    clearTimeout(this.retryTimer);
    const rechecking = this.state.checked;
    const wasAuthenticated = this.state.authenticated;
    const controller = this.start();
    this.watch();
    if (!quiet) this.publish({ busy: true, checking: true, error: null });
    let outcome = null;
    try {
      const value = await this.api.state(controller.signal);
      if (!controller.signal.aborted) {
        outcome = 'available';
        // Keep a ceremony's message when the read only confirms the shown state.
        this.publish({ ...value, checked: true, access: null, recoveringStatus: false, ...(this.state.enrolled === null ? { error: null } : {}) });
        this.retryCount = 0;
        if (rechecking && value.authenticated && !wasAuthenticated) void this.refresh();
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        const access = lapsedAccess(error);
        outcome = error instanceof WebAuthnStatusError ? error.access : null;
        if (access) this.publish({ checked: true, enrolled: null, authenticated: false, access, recoveringStatus: false, error: null });
        else {
          const kind = error instanceof WebAuthnStatusError ? error.kind : 'unavailable';
          const delay = ['network', 'unavailable'].includes(kind) ? STATUS_RETRY_DELAYS[this.retryCount] : undefined;
          this.publish({ checked: true, enrolled: null, authenticated: false, access: null,
            recoveringStatus: delay !== undefined,
            error: kind === 'unavailable' ? 'passkey_status_unavailable' : 'passkey_status_' + kind });
          if (delay !== undefined) {
            this.retryCount++;
            this.scheduleStatusRead(delay);
          }
        }
      }
    } finally {
      if (this.controller === controller) {
        this.controller = null;
        if (!quiet) this.publish({ busy: false, checking: false });
        this.follower.release(outcome);
      }
    }
  }
  async authenticate() {
    if (this.stopped || this.state.busy || this.state.enrolled === null || this.state.authenticated || this.state.access) return;
    // The ceremony retires a quiet read. The follower compares again when it ends.
    const controller = this.start();
    let reconcile = false;
    this.publish({ busy: true, error: null });
    try {
      if (this.state.enrolled) await this.api.login(controller.signal);
      else await this.api.enroll(controller.signal);
      if (!controller.signal.aborted) {
        this.publish({ enrolled: true, authenticated: true });
        // The reconnect owns a long-lived event stream; it is not a new command.
        void this.refresh();
      }
    } catch (cause) {
      if (controller.signal.aborted) return;
      reconcile = cause instanceof EnrollmentOutcomeError && cause.enrolled === null;
      this.publish(reconcile
        ? { enrolled: null, recoveringStatus: true, error: 'passkey_status_unavailable' }
        : cause instanceof EnrollmentOutcomeError && cause.enrolled
          ? { enrolled: true, error: 'passkey_sign_in_required' }
        : { error: cause instanceof LoginOutcomeError && cause.stage === 'begin' ? 'passkey_start_unavailable' : 'passkey_unconfirmed' });
    } finally {
      if (this.controller === controller) {
        this.controller = null;
        this.publish({ busy: false });
        // Registration may already have committed. Read status, never repeat the ceremony.
        // A pushed access change can start this read first and cancel the scheduled read.
        if (reconcile) this.scheduleStatusRead(0);
        this.follower.release();
      }
    }
  }
  stop() { this.stopped = true; clearTimeout(this.retryTimer); this.retire(); this.watcher?.abort(); }
}
