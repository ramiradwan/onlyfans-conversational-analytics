import { afterEach, describe, expect, it, vi } from 'vitest';

import { createWebAuthnApi } from '../shared-setup/webauthn';

function json(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    headers: { 'Content-Type': 'application/json' },
  });
}

function bytes(value: number[]): ArrayBuffer {
  return new Uint8Array(value).buffer;
}

afterEach(() => vi.unstubAllGlobals());

describe('WebAuthn ceremony API', () => {
  it('restores only the current authenticated session request context and clears it on sign-out', async () => {
    const setCsrfToken = vi.fn();
    const request = vi.fn().mockResolvedValueOnce(json({ enrolled: true, authenticated: true, csrf_token: 'current-session-context' }))
      .mockResolvedValueOnce(json({ enrolled: true, authenticated: false, csrf_token: null }));
    const api = createWebAuthnApi({ fetch: request, setCsrfToken });
    const signal = new AbortController().signal;
    await api.state!(signal); expect(setCsrfToken).toHaveBeenLastCalledWith('current-session-context');
    await api.state!(signal); expect(setCsrfToken).toHaveBeenLastCalledWith(null);
    expect(request.mock.calls.every(([path]) => path.endsWith('/session-state'))).toBe(true);
  });
  it('reads a closed owner session state once without beginning either ceremony', async () => {
    const request = vi.fn(async () => json({ enrolled: false, authenticated: false, csrf_token: null }));
    const api = createWebAuthnApi({ fetch: request as typeof fetch });
    const signal = new AbortController().signal;
    await expect(api.state!(signal)).resolves.toEqual({ enrolled: false, authenticated: false });
    expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith('/api/v1/webauthn/session-state', expect.objectContaining({ signal: expect.any(AbortSignal), credentials: 'same-origin', cache: 'no-store', redirect: 'error' }));
  });

  it.each([{ enrolled: false, authenticated: true }, { enrolled: false, authenticated: false, extra: 'value' }])('refuses inconsistent or extended session facts', async (state) => {
    const api = createWebAuthnApi({ fetch: vi.fn(async () => json({ ...state, csrf_token: state.authenticated ? 'fixture-csrf' : null })) as typeof fetch });
    await expect(api.state!(new AbortController().signal)).rejects.toMatchObject({ name: 'WebAuthnStatusError', kind: 'invalid' });
  });
  it.each([
    [{ authenticated: true, enrolled: true }, true],
    [{ authenticated: false, enrolled: true }, false],
    [{ authenticated: false, enrolled: false }, false],
    [{ authenticated: true, enrolled: false }, false],
  ])('reconciles a lost finish response without replay or a second ceremony (%j)', async (state, success) => {
    const create = vi.fn(async () => ({ id: 'Bwg', rawId: bytes([7, 8]), type: 'public-key',
      response: { clientDataJSON: bytes([9, 10]), attestationObject: bytes([11, 12]) } }));
    const get = vi.fn();
    vi.stubGlobal('navigator', { credentials: { create, get } });
    const request = vi.fn(async (path: string) => {
      if (path.endsWith('/begin')) return json({ challenge: 'AQID',
        rp: { id: 'bridge.localhost', name: 'Bridge' },
        user: { id: 'BAUG', name: 'creator-1', displayName: 'creator-1' },
        pubKeyCredParams: [{ type: 'public-key', alg: -7 }] });
      if (path.endsWith('/finish')) throw new TypeError('response lost');
      return json({ ...state, csrf_token: state.authenticated ? 'fixture-csrf' : null });
    });
    const attempt = createWebAuthnApi({ fetch: request as typeof fetch }).enroll();
    if (success) await expect(attempt).resolves.toBeUndefined();
    else await expect(attempt).rejects.toMatchObject({ name: 'EnrollmentOutcomeError',
      enrolled: state.authenticated && !state.enrolled ? null : state.enrolled });
    expect(request.mock.calls.map(([path]) => path)).toEqual([
      '/api/v1/webauthn/registration/begin', '/api/v1/webauthn/registration/finish', '/api/v1/webauthn/session-state',
    ]);
    expect(create).toHaveBeenCalledTimes(1);
    expect(get).not.toHaveBeenCalled();
  });

  it('uses the registration contract and base64url challenge round trip', async () => {
    const create = vi.fn(async () => ({
      id: 'Bwg',
      rawId: bytes([7, 8]),
      type: 'public-key',
      response: {
        clientDataJSON: bytes([9, 10]),
        attestationObject: bytes([11, 12]),
      },
    }));
    vi.stubGlobal('navigator', { credentials: { create } });
    const fetch = vi.fn(async () => json(fetch.mock.calls.length === 1 ? {
      challenge: 'AQID-_8',
      rp: { id: 'bridge.localhost', name: 'Bridge' },
      user: { id: 'BAUG', name: 'creator-1', displayName: 'creator-1' },
      pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
    } : { profile: 'local-first-enrollment-result.v1', status: 'registered', csrf_token: 'x'.repeat(43) }));

    await createWebAuthnApi({ fetch }).enroll();

    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      '/api/v1/webauthn/registration/begin',
      '/api/v1/webauthn/registration/finish',
    ]);
    const publicKey = create.mock.calls[0][0].publicKey;
    expect([...new Uint8Array(publicKey.challenge)]).toEqual([1, 2, 3, 251, 255]);
    expect([...new Uint8Array(publicKey.user.id)]).toEqual([4, 5, 6]);
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({
      id: 'Bwg',
      rawId: 'Bwg',
      type: 'public-key',
      response: { clientDataJSON: 'CQo', attestationObject: 'Cww' },
    });
  });

  it('uses the login contract and sends CSRF only when it exists', async () => {
    const get = vi.fn(async () => ({
      id: 'Bwg',
      rawId: bytes([7, 8]),
      type: 'public-key',
      response: {
        clientDataJSON: bytes([9, 10]),
        authenticatorData: bytes([11, 12]),
        signature: bytes([13, 14]),
        userHandle: null,
      },
    }));
    vi.stubGlobal('navigator', { credentials: { get } });
    const fetch = vi.fn(async () => json(fetch.mock.calls.length === 1 ? {
      challenge: 'AQID-_8',
      rpId: 'bridge.localhost',
      allowCredentials: [{ type: 'public-key', id: 'Bwg' }],
      userVerification: 'required',
    } : { csrf_token: 'next-token' }));

    await createWebAuthnApi({ fetch, getCsrfToken: () => 'csrf-token-1' }).login();

    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      '/api/v1/webauthn/login/begin',
      '/api/v1/webauthn/login/finish',
    ]);
    expect([...new Uint8Array(get.mock.calls[0][0].publicKey.challenge)]).toEqual([1, 2, 3, 251, 255]);
    expect([...new Uint8Array(get.mock.calls[0][0].publicKey.allowCredentials[0].id)]).toEqual([7, 8]);
    expect(fetch.mock.calls[0][1].headers).toEqual({
      Accept: 'application/json',
      'X-CSRF-Token': 'csrf-token-1',
    });
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({
      id: 'Bwg',
      rawId: 'Bwg',
      type: 'public-key',
      response: {
        clientDataJSON: 'CQo',
        authenticatorData: 'Cww',
        signature: 'DQ4',
        userHandle: null,
      },
    });
  });
});

function assertion() {
  return { id: 'Bwg', rawId: bytes([7, 8]), type: 'public-key', response: {
    clientDataJSON: bytes([9, 10]), authenticatorData: bytes([11, 12]), signature: bytes([13, 14]), userHandle: null,
  } };
}
const loginOptions = { challenge: 'AQID', rpId: 'bridge.localhost', allowCredentials: [{ type: 'public-key', id: 'Bwg' }] };
it.each([true, false])('reconciles lost login finish once without another assertion authenticated=%s', async (authenticated) => {
  const get = vi.fn(async () => assertion()); const create = vi.fn();
  vi.stubGlobal('navigator', { credentials: { get, create } });
  const request = vi.fn(async (path: string) => {
    if (path.endsWith('/begin')) return json(loginOptions);
    if (path.endsWith('/finish')) throw Error('Lost reply');
    return json({ enrolled: true, authenticated, csrf_token: authenticated ? 'fixture-csrf' : null });
  });
  const attempt = createWebAuthnApi({ fetch: request as typeof fetch }).login();
  if (authenticated) await expect(attempt).resolves.toBeUndefined();
  else await expect(attempt).rejects.toMatchObject({ name: 'LoginOutcomeError', stage: 'finish' });
  expect(request.mock.calls.map(([path]) => path)).toEqual([
    '/api/v1/webauthn/login/begin', '/api/v1/webauthn/login/finish', '/api/v1/webauthn/session-state',
  ]);
  expect(get).toHaveBeenCalledTimes(1); expect(create).not.toHaveBeenCalled();
});
it('does not reconcile a definite assertion refusal or begin a replacement enrollment', async () => {
  vi.stubGlobal('navigator', { credentials: { get: vi.fn(async () => assertion()) } });
  const request = vi.fn(async (path: string) => path.endsWith('/begin') ? json(loginOptions) : new Response('', { status: 403 }));
  await expect(createWebAuthnApi({ fetch: request as typeof fetch }).login()).rejects.toMatchObject({ stage: 'finish' });
  expect(request).toHaveBeenCalledTimes(2);
});
it.each([null, 'NotAllowedError'])('keeps the browser refusal ambiguous without submitting finish (%s)', async (failure) => {
  vi.stubGlobal('navigator', { credentials: { get: vi.fn(async () => {
    if (failure) throw new DOMException('Private technical cause', failure);
    return null;
  }) } });
  const request = vi.fn(async () => json(loginOptions));
  await expect(createWebAuthnApi({ fetch: request as typeof fetch }).login()).rejects.toMatchObject({ stage: 'credential' });
  expect(request).toHaveBeenCalledTimes(1);
});
it('bounds a stalled begin and does not start a browser ceremony', async () => {
  const get = vi.fn(); vi.stubGlobal('navigator', { credentials: { get } });
  const request = vi.fn(() => new Promise<Response>(() => {}));
  await expect(createWebAuthnApi({ fetch: request as typeof fetch, requestTimeoutMs: 5 }).login()).rejects.toMatchObject({ stage: 'begin' });
  expect(get).not.toHaveBeenCalled();
});
it('retiring a ceremony aborts credential acquisition and prevents finish', async () => {
  const controller = new AbortController();
  let signal: AbortSignal | undefined;
  vi.stubGlobal('navigator', { credentials: { get: vi.fn((options: CredentialRequestOptions) => {
    signal = options.signal; return new Promise<Credential>(() => {});
  }) } });
  const request = vi.fn(async () => json(loginOptions));
  const attempt = createWebAuthnApi({ fetch: request as typeof fetch }).login(controller.signal);
  while (!signal) await new Promise((resolve) => setTimeout(resolve, 0));
  controller.abort();
  await expect(attempt).rejects.toMatchObject({ stage: 'credential' });
  expect(signal.aborted).toBe(true); expect(request).toHaveBeenCalledTimes(1);
});


it.each([[401, 'session'], [403, 'forbidden'], [503, 'unavailable']] as const)(
  'classifies passkey status HTTP %s without inventing enrollment', async (status, kind) => {
    const api = createWebAuthnApi({ fetch: vi.fn(async () => new Response('', { status })) as typeof fetch });
    await expect(api.state!(new AbortController().signal)).rejects.toMatchObject({ name: 'WebAuthnStatusError', kind, status });
  });
it('classifies a failed passkey status connection', async () => {
  const api = createWebAuthnApi({ fetch: vi.fn(async () => { throw new TypeError('offline'); }) as typeof fetch });
  await expect(api.state!(new AbortController().signal)).rejects.toMatchObject({ name: 'WebAuthnStatusError', kind: 'network' });
});
