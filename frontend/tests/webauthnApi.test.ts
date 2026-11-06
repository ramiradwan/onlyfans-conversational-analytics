import { afterEach, describe, expect, it, vi } from 'vitest';

import { AccessFollower, createWebAuthnApi, lapsedAccess, type PushedAccess } from '../shared-setup/webauthn';

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

const refused = (body: string) => createWebAuthnApi({ fetch: vi.fn(async () => new Response(body, { status: 409 })) as typeof fetch })
  .state!(new AbortController().signal).catch((error: unknown) => error);
const frame = (access: string) => `event: access\ndata: {"access":"${access}"}\n\n`;
function events(chunks: string[], close = true, cancel?: () => void): Response {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(stream) { for (const chunk of chunks) stream.enqueue(encoder.encode(chunk)); if (close) stream.close(); },
    cancel,
  }), { headers: { 'Content-Type': 'text/event-stream' } });
}

describe('pushed access', () => {
  it.each(['renewing', 'expired', 'removed'] as const)('reads a %s refusal as a lapsed access state', async (access) => {
    const failure = await refused(JSON.stringify({ detail: 'Enrollment state is unavailable', access }));
    expect(failure).toMatchObject({ name: 'WebAuthnStatusError', kind: 'access', status: 409, access });
    expect(lapsedAccess(failure)).toBe(access);
  });

  it.each([
    JSON.stringify({ detail: 'Enrollment state is unavailable', access: 'unavailable' }),
    JSON.stringify({ detail: 'Enrollment state is unavailable', access: 'paused' }),
    JSON.stringify({ detail: 'Enrollment state is unavailable', access: 'renewing', reason: 'membership' }),
    JSON.stringify({ access: 'renewing' }),
    '{"detail":"Enrollment state is unavailable","access":"renewing","access":"removed"}',
    'Enrollment state is unavailable',
  ])('keeps any other refusal unavailable: %s', async (body) => {
    const failure = await refused(body);
    expect(failure).toMatchObject({ name: 'WebAuthnStatusError', kind: 'unavailable', status: 409 });
    expect(lapsedAccess(failure)).toBeNull();
  });

  it('delivers each access frame across chunk boundaries from one connection', async () => {
    const text = frame('renewing') + ': keepalive\n\n' + 'event: other\ndata: {"access":"removed"}\n\n' + frame('available');
    const controller = new AbortController();
    const received: PushedAccess[] = [];
    const request = vi.fn(async () => events([text.slice(0, 9), text.slice(9, 40), text.slice(40)], false));
    await createWebAuthnApi({ fetch: request as typeof fetch }).watch!(controller.signal, (access) => {
      received.push(access);
      if (received.length === 2) controller.abort();
    });
    expect(received).toEqual(['renewing', 'available']);
    expect(request).toHaveBeenCalledOnce();
    expect(request).toHaveBeenCalledWith('/api/v1/webauthn/session-state/events', expect.objectContaining({
      credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: { Accept: 'text/event-stream' },
    }));
  });

  it('reconnects with a growing delay after invalid or ended streams and stops for a refused origin', async () => {
    vi.useFakeTimers();
    try {
      const received: PushedAccess[] = [];
      const request = vi.fn()
        .mockResolvedValueOnce(events([frame('paused'), frame('available')]))
        .mockResolvedValueOnce(events([frame('removed')]))
        .mockResolvedValueOnce(new Response('', { status: 403 }));
      const watching = createWebAuthnApi({ fetch: request as typeof fetch }).watch!(new AbortController().signal, (access) => received.push(access));
      await vi.advanceTimersByTimeAsync(999);
      expect(request).toHaveBeenCalledOnce();
      await vi.advanceTimersByTimeAsync(1);
      expect(request).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(2000);
      await watching;
      expect(request).toHaveBeenCalledTimes(3);
      expect(received).toEqual(['removed']);
    } finally { vi.useRealTimers(); }
  });

  it('drops a stream that goes quiet past its keepalive interval', async () => {
    vi.useFakeTimers();
    try {
      const controller = new AbortController();
      let cancelled = false;
      const request = vi.fn()
        .mockResolvedValueOnce(events([frame('available')], false, () => { cancelled = true; }))
        .mockImplementationOnce(async () => { controller.abort(); return new Response('', { status: 503 }); });
      const watching = createWebAuthnApi({ fetch: request as typeof fetch }).watch!(controller.signal, () => undefined);
      await vi.advanceTimersByTimeAsync(44_999);
      expect(cancelled).toBe(false);
      await vi.advanceTimersByTimeAsync(1);
      expect(cancelled).toBe(true);
      await vi.advanceTimersByTimeAsync(1000);
      await watching;
      expect(request).toHaveBeenCalledTimes(2);
    } finally { vi.useRealTimers(); }
  });

  it('starts the reconnect delay over after a connection that lasted', async () => {
    vi.useFakeTimers();
    try {
      const controller = new AbortController();
      let control: ReadableStreamDefaultController<Uint8Array> | undefined;
      const request = vi.fn()
        .mockResolvedValueOnce(events([]))
        .mockResolvedValueOnce(events([]))
        .mockImplementationOnce(async () => new Response(new ReadableStream<Uint8Array>({ start(stream) { control = stream; } }),
          { headers: { 'Content-Type': 'text/event-stream' } }))
        .mockImplementationOnce(async () => { controller.abort(); return new Response('', { status: 503 }); });
      const watching = createWebAuthnApi({ fetch: request as typeof fetch }).watch!(controller.signal, () => undefined);
      await vi.advanceTimersByTimeAsync(3000);
      expect(request).toHaveBeenCalledTimes(3);
      for (let tick = 0; tick < 4; tick++) {
        await vi.advanceTimersByTimeAsync(15_000);
        control!.enqueue(new TextEncoder().encode(': keepalive\n\n'));
      }
      await vi.advanceTimersByTimeAsync(1001);
      control!.close();
      await vi.advanceTimersByTimeAsync(999);
      expect(request).toHaveBeenCalledTimes(3);
      await vi.advanceTimersByTimeAsync(1);
      await watching;
      expect(request).toHaveBeenCalledTimes(4);
    } finally { vi.useRealTimers(); }
  });

  it.each([
    ['a data field over 256 characters', [`event: access\ndata: {"access":"removed"${' '.repeat(240)}}\n\n`, frame('removed')]],
    ['a line over 1024 characters', [`: ${'x'.repeat(1023)}\n\n`, frame('removed')]],
    ['an unterminated line over 1024 characters', ['x'.repeat(1025)]],
  ])('drops a stream with %s and reconnects', async (_case, chunks) => {
    vi.useFakeTimers();
    try {
      const controller = new AbortController();
      const received: PushedAccess[] = [];
      const request = vi.fn()
        .mockResolvedValueOnce(events(chunks, false))
        .mockImplementationOnce(async () => { controller.abort(); return new Response('', { status: 503 }); });
      const watching = createWebAuthnApi({ fetch: request as typeof fetch }).watch!(controller.signal, (access) => received.push(access));
      await vi.advanceTimersByTimeAsync(1000);
      await watching;
      expect(request).toHaveBeenCalledTimes(2);
      expect(received).toEqual([]);
    } finally { vi.useRealTimers(); }
  });

  it('cancels the open stream when the page retires it', async () => {
    const controller = new AbortController();
    let cancelled = false;
    const request = vi.fn(async () => events([frame('available')], false, () => { cancelled = true; }));
    await createWebAuthnApi({ fetch: request as typeof fetch }).watch!(controller.signal, () => controller.abort());
    expect(cancelled).toBe(true);
    expect(request).toHaveBeenCalledOnce();
  });
});

describe('access follower', () => {
  it('reads again once for a pushed value the last read did not observe', () => {
    const reread = vi.fn();
    const follower = new AccessFollower(reread);
    follower.hold(); follower.push('renewing'); follower.release('renewing');
    follower.push('renewing');
    expect(reread).not.toHaveBeenCalled();
    follower.push('available');
    expect(reread).toHaveBeenCalledOnce();
    // A failed read does not ask again until something new is pushed.
    follower.hold(); follower.release(null);
    expect(reread).toHaveBeenCalledOnce();
    follower.push('available');
    expect(reread).toHaveBeenCalledTimes(2);
  });

  it('compares a value pushed during a ceremony when it ends', () => {
    const reread = vi.fn();
    const follower = new AccessFollower(reread);
    follower.hold(); follower.release('available');
    follower.hold(); follower.push('removed'); follower.push('available'); follower.release();
    expect(reread).not.toHaveBeenCalled();
    follower.hold(); follower.push('removed'); follower.release();
    expect(reread).toHaveBeenCalledOnce();
  });

  it('keeps the comparison for a read retired before it answered', () => {
    const reread = vi.fn();
    const follower = new AccessFollower(reread);
    follower.hold(); follower.release('available');
    follower.push('renewing');
    expect(reread).toHaveBeenCalledOnce();
    // A newer read retires the one the push asked for and answers with the older value.
    follower.hold(); follower.abandon();
    follower.hold(); follower.release('available');
    expect(reread).toHaveBeenCalledTimes(2);
  });

  it('compares a read that answered during a ceremony when the ceremony ends', () => {
    const reread = vi.fn();
    const follower = new AccessFollower(reread);
    follower.hold(); follower.release('available');
    follower.push('renewing');
    expect(reread).toHaveBeenCalledOnce();
    follower.hold(); follower.hold();
    follower.release('available');
    expect(reread).toHaveBeenCalledOnce();
    follower.release();
    expect(reread).toHaveBeenCalledTimes(2);
  });
});
