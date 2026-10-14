import { createWebAuthnApi, EnrollmentOutcomeError, LoginOutcomeError, WebAuthnStatusError } from '../shared-setup/webauthn.ts';

/** The native presentation uses the existing WebAuthn owner and session. */
export class DesktopEnrollment {
  constructor({ api = createWebAuthnApi(), receive, refresh }) {
    Object.assign(this, { api, receive, refresh });
    this.state = { checked: false, enrolled: null, authenticated: false, busy: false, error: null };
    this.controller = null;
    this.retryTimer = null;
    this.retryCount = 0;
  }
  publish(value) { this.state = { ...this.state, ...value }; this.receive(this.state); }
  async check() {
    if (this.state.busy) return;
    clearTimeout(this.retryTimer);
    const rechecking = this.state.checked;
    this.controller?.abort();
    const controller = new AbortController(); this.controller = controller;
    this.publish({ busy: true, error: null });
    try {
      const value = await this.api.state(controller.signal);
      if (!controller.signal.aborted) {
        this.publish({ ...value, checked: true });
        this.retryCount = 0;
        if (rechecking && value.authenticated) void this.refresh();
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        const kind = error instanceof WebAuthnStatusError ? error.kind : 'unavailable';
        this.publish({ checked: true, enrolled: null, authenticated: false,
          error: kind === 'unavailable' ? 'passkey_status_unavailable' : 'passkey_status_' + kind });
        if (['network', 'unavailable'].includes(kind) && this.retryCount < 3) {
          this.retryTimer = setTimeout(() => { void this.check(); }, 1000 * 2 ** this.retryCount++);
        }
      }
    } finally {
      if (!controller.signal.aborted) this.publish({ busy: false });
    }
  }
  async authenticate() {
    if (this.state.busy || this.state.enrolled === null || this.state.authenticated) return;
    const controller = new AbortController(); this.controller = controller;
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
      this.publish(cause instanceof EnrollmentOutcomeError && cause.enrolled === null
        ? { enrolled: null, error: 'passkey_status_unavailable' }
        : cause instanceof EnrollmentOutcomeError && cause.enrolled
          ? { enrolled: true, error: 'passkey_sign_in_required' }
        : { error: cause instanceof LoginOutcomeError && cause.stage === 'begin' ? 'passkey_start_unavailable' : 'passkey_unconfirmed' });
    } finally {
      if (!controller.signal.aborted) this.publish({ busy: false });
    }
  }
  stop() { clearTimeout(this.retryTimer); this.controller?.abort(); }
}
