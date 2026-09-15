import { afterEach, describe, expect, it, vi } from 'vitest';

import { createCompanionPairingApi } from '../src/services/companionPairingApi';

const id = 'A'.repeat(43);
const status = {
  pairing_id: id,
  creator_account_id: 'creator-1',
  generation: 1,
  version: 0,
  state: 'open',
  expires_at: '2026-09-12T12:05:00+00:00',
  comparison_code: null,
  agent_identity_thumbprint: null,
};
const response = (body: unknown) => new Response(JSON.stringify(body));

afterEach(() => vi.useRealTimers());

describe('companion pairing public API', () => {
  it('lists bounded public pins and sends versioned CSRF revocation', async () => {
    const pin = { ...status, state: 'admitted' };
    const fetch = vi.fn().mockResolvedValueOnce(response({ pins: [pin] }))
      .mockResolvedValueOnce(response({ ...pin, state: 'revoked' }));
    const api = createCompanionPairingApi({ fetch, getCsrfToken: () => 'csrf-fixture' });
    expect(await api.pins()).toEqual([pin]);
    expect((await api.revoke(id, 4)).state).toBe('revoked');
    expect(fetch.mock.calls[1]).toEqual([`/api/v1/companion/pins/${id}/revoke`, expect.objectContaining({
      method: 'POST', body: '{"version":4}', headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-fixture' }),
    })]);
  });

  it('uses same-origin CSRF-protected commands and exact versioned decisions', async () => {
    const fetch = vi.fn(async () => response(status));
    const api = createCompanionPairingApi({ fetch, getCsrfToken: () => 'csrf-fixture' });
    await api.open('creator-1');
    await api.get(id);
    await api.change(id, 'confirm', 4);
    await api.change(id, 'decline', 4);
    await api.change(id, 'cancel', 4);
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/companion/pairings',
      `/api/v1/companion/pairings/${id}`,
      `/api/v1/companion/pairings/${id}/confirm`,
      `/api/v1/companion/pairings/${id}/decline`,
      `/api/v1/companion/pairings/${id}/cancel`,
    ]);
    expect(fetch.mock.calls[0][1]).toMatchObject({
      method: 'POST', credentials: 'same-origin', redirect: 'error', cache: 'no-store',
      headers: { 'X-CSRF-Token': 'csrf-fixture', 'Content-Type': 'application/json' },
      body: JSON.stringify({ creator_account_id: 'creator-1' }),
    });
    expect(fetch.mock.calls[1][1]).toMatchObject({ method: 'GET', credentials: 'same-origin' });
    expect(fetch.mock.calls[1][1]).not.toHaveProperty('body');
    expect(fetch.mock.calls[2][1]).toHaveProperty('body', '{"version":4}');
  });

  it('refuses mutations before fetch without CSRF', async () => {
    const fetch = vi.fn();
    const api = createCompanionPairingApi({ fetch, getCsrfToken: () => null });
    await expect(api.open('creator-1')).rejects.toMatchObject({ code: 'csrf' });
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each([
    { ...status, unexpected: 'not-public-state' },
    { ...status, state: 'awaiting_confirmation' },
    { ...status, comparison_code: '123' },
    { ...status, expires_at: 'invalid' },
    { ...status, version: -1 },
    { ...status, creator_account_id: 'other-account' },
  ])('rejects malformed or mismatched public state without parser details (%#)', async (body) => {
    const api = createCompanionPairingApi({
      fetch: vi.fn(async () => response(body)), getCsrfToken: () => 'csrf-fixture',
    });
    await expect(api.open('creator-1')).rejects.toMatchObject({
      code: 'response', message: 'The extension pairing request could not be completed.',
    });
  });

  it('rejects another pairing ID from a scoped response', async () => {
    const api = createCompanionPairingApi({
      fetch: vi.fn(async () => response({ ...status, pairing_id: 'B'.repeat(43) })),
    });
    await expect(api.get(id)).rejects.toMatchObject({ code: 'response' });
  });

  it('does not read an error response body or expose native exceptions', async () => {
    const body = new ReadableStream({ pull: vi.fn() });
    const getReader = vi.spyOn(body, 'getReader');
    const fetch = vi.fn().mockResolvedValueOnce({ ok: false, body }).mockRejectedValueOnce(
      new Error('untrusted native error detail'),
    );
    const api = createCompanionPairingApi({ fetch });
    await expect(api.get(id)).rejects.toMatchObject({ code: 'request' });
    expect(getReader).not.toHaveBeenCalled();
    await expect(api.get(id)).rejects.toMatchObject({
      code: 'response', message: 'The extension pairing request could not be completed.',
    });
  });

  it('cancels oversized streamed responses before JSON parsing', async () => {
    const cancel = vi.fn();
    const body = new ReadableStream({
      start(controller) { controller.enqueue(new Uint8Array(8193)); }, cancel,
    });
    const api = createCompanionPairingApi({ fetch: vi.fn(async () => new Response(body)) });
    await expect(api.get(id)).rejects.toMatchObject({ code: 'response' });
    expect(cancel).toHaveBeenCalledOnce();
  });

  it('aborts requests at the deadline and propagates caller cancellation', async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    const fetch = vi.fn((_url: string | URL | Request, init?: RequestInit) => new Promise<Response>(
      (_resolve, reject) => {
        const signal = init!.signal!;
        signals.push(signal);
        signal.addEventListener('abort', () => reject(new Error('untrusted abort detail')));
      },
    ));
    const api = createCompanionPairingApi({ fetch });
    const timeout = expect(api.get(id)).rejects.toMatchObject({ code: 'timeout' });
    await vi.advanceTimersByTimeAsync(10_000);
    await timeout;
    expect(signals[0].aborted).toBe(true);
    const controller = new AbortController();
    const cancellation = expect(api.get(id, controller.signal)).rejects.toMatchObject({ code: 'cancelled' });
    controller.abort();
    await cancellation;
    expect(signals[1].aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('does not start an already cancelled request', async () => {
    const fetch = vi.fn();
    const controller = new AbortController();
    controller.abort();
    const api = createCompanionPairingApi({ fetch });
    await expect(api.get(id, controller.signal)).rejects.toMatchObject({ code: 'cancelled' });
    expect(fetch).not.toHaveBeenCalled();
  });
});
