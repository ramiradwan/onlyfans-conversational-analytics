import { afterEach, describe, expect, it, vi } from 'vitest';

import { createWebAuthnApi } from '../src/services/webauthnApi';

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
    } : { status: 'registered' }));

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
