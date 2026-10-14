import { describe, expect, it, vi } from 'vitest';
import { DesktopEnrollment } from '../onboarding/enrollment.js';
import { EnrollmentOutcomeError } from '../shared-setup/webauthn';

function setup(enrolled = false) {
  const api = { state: vi.fn(async () => ({ enrolled, authenticated: false })),
    enroll: vi.fn(async () => {}), login: vi.fn(async () => {}) };
  const receive = vi.fn(), refresh = vi.fn();
  return { api, receive, refresh, owner: new DesktopEnrollment({ api, receive, refresh }) };
}

describe('native local enrollment', () => {
  it.each([false, true])('reads without a ceremony and waits for an explicit %s action', async enrolled => {
    const h = setup(enrolled);
    expect(h.api.state).not.toHaveBeenCalled();
    await h.owner.check();
    expect(h.api.enroll).not.toHaveBeenCalled(); expect(h.api.login).not.toHaveBeenCalled();
    await h.owner.authenticate();
    expect(h.api.enroll).toHaveBeenCalledTimes(enrolled ? 0 : 1);
    expect(h.api.login).toHaveBeenCalledTimes(enrolled ? 1 : 0);
    expect(h.refresh).toHaveBeenCalledOnce();
    expect(h.owner.state.authenticated).toBe(true);
    await h.owner.authenticate();
    expect(h.refresh).toHaveBeenCalledOnce();
  });
  it('preserves a committed enrollment without starting registration again', async () => {
    const h = setup(); await h.owner.check();
    h.api.enroll.mockRejectedValue(new EnrollmentOutcomeError(true));
    await h.owner.authenticate();
    expect(h.owner.state).toMatchObject({ enrolled: true, authenticated: false, error: 'passkey_sign_in_required' });
    expect(h.refresh).not.toHaveBeenCalled();
    await h.owner.authenticate();
    expect(h.api.enroll).toHaveBeenCalledOnce(); expect(h.api.login).toHaveBeenCalledOnce();
  });
  it('coalesces clicks and ignores a late credential result after the page closes', async () => {
    const h = setup(); await h.owner.check();
    let finish;
    h.api.enroll.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const first = h.owner.authenticate(); await h.owner.authenticate();
    expect(h.api.enroll).toHaveBeenCalledOnce();
    const signal = h.api.enroll.mock.calls[0][0];
    h.owner.stop(); finish(); await first;
    expect(signal.aborted).toBe(true); expect(h.refresh).not.toHaveBeenCalled();
    expect(h.owner.state.authenticated).toBe(false);
  });
  it('requires a status read after an unknown enrollment outcome before another ceremony', async () => {
    const h = setup(); await h.owner.check();
    h.api.enroll.mockRejectedValue(new EnrollmentOutcomeError(null));
    await h.owner.authenticate();
    expect(h.owner.state).toMatchObject({ enrolled: null, error: 'passkey_status_unavailable' });
    await h.owner.authenticate();
    expect(h.api.enroll).toHaveBeenCalledOnce(); expect(h.api.login).not.toHaveBeenCalled();
    h.api.state.mockResolvedValue({ enrolled: true, authenticated: true });
    await h.owner.check();
    expect(h.owner.state.authenticated).toBe(true); expect(h.refresh).toHaveBeenCalledOnce();
    expect(h.api.enroll).toHaveBeenCalledOnce(); expect(h.api.login).not.toHaveBeenCalled();
  });
  it('keeps an unavailable status distinct from a cancelled or rejected verification', async () => {
    const h = setup(); h.api.state.mockRejectedValueOnce(Error('unavailable'));
    await h.owner.check(); await h.owner.authenticate();
    expect(h.api.enroll).not.toHaveBeenCalled();
    expect(h.owner.state.error).toBe('passkey_status_unavailable');
    await h.owner.check();
    h.api.enroll.mockRejectedValue(new DOMException('Private authenticator details', 'NotAllowedError'));
    await h.owner.authenticate();
    expect(h.owner.state.error).toBe('passkey_unconfirmed');
    expect(h.refresh).not.toHaveBeenCalled();
  });
});
