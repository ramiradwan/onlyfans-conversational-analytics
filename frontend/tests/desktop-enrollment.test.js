import { describe, expect, it, vi } from 'vitest';
import { DesktopEnrollment } from '../onboarding/enrollment.js';
import { EnrollmentOutcomeError, WebAuthnStatusError } from '../shared-setup/webauthn';

function setup(enrolled = false) {
  const api = { state: vi.fn(async () => ({ enrolled, authenticated: false })),
    enroll: vi.fn(async () => {}), login: vi.fn(async () => {}) };
  const receive = vi.fn(), refresh = vi.fn();
  return { api, receive, refresh, owner: new DesktopEnrollment({ api, receive, refresh }) };
}

/** An access stream the test pushes into. It ends when its signal aborts. */
function pushed(h) {
  const opened = [];
  h.api.watch = vi.fn((signal, receive) => {
    opened.push({ signal, receive });
    return new Promise(resolve => signal.addEventListener('abort', () => resolve(), { once: true }));
  });
  return { opened, push: access => opened.at(-1).receive(access) };
}
const lapsed = access => new WebAuthnStatusError('access', 409, access);
// Lets a read that a push started answer.
const settle = () => new Promise(resolve => setTimeout(resolve, 0));
const refused = () => new DOMException('Private authenticator details', 'NotAllowedError');

describe('pushed access', () => {
  it.each(['renewing', 'expired', 'removed'])('shows %s access without retrying or starting a ceremony', async access => {
    vi.useFakeTimers();
    try {
      const h = setup(true);
      h.api.state.mockRejectedValue(lapsed(access));
      await h.owner.check();
      expect(h.owner.state).toMatchObject({ checked: true, enrolled: null, authenticated: false, busy: false, error: null, access });
      await vi.advanceTimersByTimeAsync(10 * 60_000);
      expect(h.api.state).toHaveBeenCalledOnce();
      await h.owner.authenticate();
      expect(h.api.login).not.toHaveBeenCalled(); expect(h.api.enroll).not.toHaveBeenCalled();
    } finally { vi.useRealTimers(); }
  });
  it('follows each pushed change with one quiet read', async () => {
    const h = setup(true); const stream = pushed(h);
    h.api.state.mockRejectedValueOnce(lapsed('renewing'));
    await h.owner.check();
    stream.push('renewing'); await settle();
    expect(h.api.state).toHaveBeenCalledOnce();
    const shown = h.receive.mock.calls.length;
    h.api.state.mockResolvedValueOnce({ enrolled: true, authenticated: true });
    stream.push('available'); await settle();
    expect(h.api.state).toHaveBeenCalledTimes(2);
    expect(h.owner.state).toMatchObject({ checked: true, enrolled: true, authenticated: true, busy: false, error: null, access: null });
    expect(h.receive.mock.calls.slice(shown).map(([value]) => value.busy)).not.toContain(true);
    expect(h.refresh).toHaveBeenCalledOnce();
    stream.push('available'); await settle();
    expect(h.api.state).toHaveBeenCalledTimes(2); expect(h.refresh).toHaveBeenCalledOnce();
    expect(h.api.watch).toHaveBeenCalledOnce();
    h.owner.stop();
    expect(stream.opened[0].signal.aborted).toBe(true);
  });
  it('reads a change pushed during a ceremony once the ceremony ends', async () => {
    const h = setup(true); const stream = pushed(h);
    await h.owner.check(); stream.push('available');
    let refuse;
    h.api.login.mockImplementation(() => new Promise((resolve, reject) => { refuse = reject; }));
    const ceremony = h.owner.authenticate();
    stream.push('removed'); await settle();
    expect(h.api.state).toHaveBeenCalledOnce();
    h.api.state.mockRejectedValueOnce(lapsed('removed'));
    refuse(refused()); await ceremony; await settle();
    expect(h.api.state).toHaveBeenCalledTimes(2);
    expect(h.owner.state).toMatchObject({ enrolled: null, authenticated: false, busy: false, error: null, access: 'removed' });
    expect(h.refresh).not.toHaveBeenCalled();
  });
  it('compares again after a ceremony retires a quiet read that settles late', async () => {
    const h = setup(true); const stream = pushed(h);
    await h.owner.check(); stream.push('available');
    let answer;
    h.api.state.mockImplementationOnce(() => new Promise(resolve => { answer = resolve; }));
    stream.push('renewing');
    expect(h.api.state).toHaveBeenCalledTimes(2);
    const quietRead = h.api.state.mock.calls[1][0];
    h.api.login.mockRejectedValueOnce(refused());
    h.api.state.mockRejectedValueOnce(lapsed('renewing'));
    await h.owner.authenticate(); await settle();
    expect(quietRead.aborted).toBe(true);
    expect(h.api.state).toHaveBeenCalledTimes(3);
    expect(h.owner.state).toMatchObject({ busy: false, error: null, access: 'renewing' });
    answer({ enrolled: true, authenticated: false }); await settle();
    expect(h.api.state).toHaveBeenCalledTimes(3);
    expect(h.owner.state.access).toBe('renewing');
  });
});

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
