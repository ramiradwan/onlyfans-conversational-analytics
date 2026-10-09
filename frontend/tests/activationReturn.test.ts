import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createActivationReturnApi } from '../src/services/activationReturn';

const journey = '00000000-0000-4000-8000-000000000001';
const other = '00000000-0000-4000-8000-000000000002';
const entry = '00000000-0000-4000-8000-000000000003';
const response = (state: string, journeyId = journey, entryId: string | null = state === 'none' ? null : entry) => new Response(JSON.stringify({ state, journey_id: journeyId, entry_id: entryId }), {
  headers: { 'Content-Type': 'application/json' },
});

describe('automatic local activation return', () => {
  beforeEach(() => sessionStorage.clear());
  afterEach(() => vi.useRealTimers());
  it('does nothing when this journey has no staged activation', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(response('none'));
    const api = createActivationReturnApi({ fetch, csrf: () => 'csrf' });
    expect(await api.start(journey)).toBe('none');
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][1]?.method).toBe('GET');
  });

  it('waits without dispatch or journal until the local approval is ready', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(response('waiting'))
      .mockResolvedValueOnce(response('ready')).mockResolvedValueOnce(response('checking'));
    const api = createActivationReturnApi({ fetch, csrf: () => 'csrf' });
    expect(await api.start(journey)).toBe('waiting');
    expect(sessionStorage.length).toBe(0);
    expect(await api.start(journey)).toBe('checking');
    expect(fetch.mock.calls.map(([, request]) => request?.method)).toEqual(['GET', 'GET', 'POST']);
  });

  it('dispatches a staged activation once with the existing local session and CSRF', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(response('ready')).mockResolvedValueOnce(response('checking'));
    const api = createActivationReturnApi({ fetch, csrf: () => 'csrf' });
    expect(await Promise.all([api.start(journey), api.start(journey)])).toEqual(['checking', 'checking']);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[1]).toEqual(['/api/v1/onboarding/activation-return', expect.objectContaining({
      method: 'POST', credentials: 'same-origin', redirect: 'error', cache: 'no-store', body: '{}',
      headers: { Accept: 'application/json', 'X-Onboarding-Journey': journey,
        'Content-Type': 'application/json', 'X-CSRF-Token': 'csrf', 'X-Onboarding-Activation-Entry': entry },
    })]);
    expect(JSON.stringify(fetch.mock.calls)).not.toContain('activation_continuation');
  });

  it('reads after an uncertain dispatch without posting again', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(response('ready')).mockRejectedValueOnce(new TypeError('network'))
      .mockResolvedValueOnce(response('checking')).mockResolvedValueOnce(response('ready'));
    const api = createActivationReturnApi({ fetch, csrf: () => 'csrf' });
    expect(await api.start(journey)).toBe('checking');
    expect(await api.start(journey)).toBe('unconfirmed');
    expect(fetch.mock.calls.map(([, request]) => request?.method)).toEqual(['GET', 'POST', 'GET', 'GET']);
  });

  it('Check again is always read-only', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(async () => response('ready'));
    const api = createActivationReturnApi({ fetch, csrf: () => 'csrf' });
    expect(await api.check(journey)).toBe('ready');
    expect(await api.check(journey)).toBe('ready');
    expect(fetch.mock.calls.every(([, request]) => request?.method === 'GET')).toBe(true);
  });

  it.each([
    ['wrong journey', () => response('ready', other)],
    ['extra authority', () => new Response(JSON.stringify({ state: 'ready', journey_id: journey, activation_continuation: 'unexpected' }))],
    ['unknown state', () => response('complete')],
    ['duplicate key', () => new Response(`{"state":"none","state":"ready","journey_id":"${journey}"}`)],
    ['oversized body', () => new Response(' '.repeat(2048))],
  ])('refuses %s before any mutation', async (_name, reply) => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(reply());
    const api = createActivationReturnApi({ fetch, csrf: () => 'csrf' });
    expect(await api.start(journey)).toBe('unconfirmed');
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('never posts without the local CSRF token', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(async () => response('ready'));
    const api = createActivationReturnApi({ fetch, csrf: () => null });
    expect(await api.start(journey)).toBe('unconfirmed');
    expect(fetch.mock.calls.every(([, request]) => request?.method === 'GET')).toBe(true);
  });

  it('preserves an uncertain entry across page reloads and accepts a distinct new entry', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(response('ready')).mockRejectedValueOnce(new TypeError('network'))
      .mockResolvedValueOnce(response('ready')).mockResolvedValueOnce(response('ready'))
      .mockResolvedValueOnce(response('ready', journey, other)).mockResolvedValueOnce(response('checking', journey, other));
    expect(await createActivationReturnApi({ fetch, csrf: () => 'csrf' }).start(journey)).toBe('unconfirmed');
    const reloaded = createActivationReturnApi({ fetch, csrf: () => 'csrf' });
    expect(await reloaded.start(journey)).toBe('unconfirmed');
    expect(await reloaded.start(journey)).toBe('checking');
    expect(fetch.mock.calls.map(([, request]) => request?.method)).toEqual(['GET', 'POST', 'GET', 'GET', 'GET', 'POST']);
    expect(Object.values(sessionStorage)).toEqual(['1', '1']);
  });

  it('requires a saved entry marker before dispatch', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(response('ready'));
    const api = createActivationReturnApi({ fetch, csrf: () => 'csrf', storage: () => ({
      getItem: () => null, setItem: () => { throw new Error('storage unavailable'); },
    }) });
    expect(await api.start(journey)).toBe('unconfirmed');
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it.each(['fetch', 'body'])('bounds an unresponsive %s even when abort is ignored', async (stage) => {
    vi.useFakeTimers();
    const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(() => stage === 'fetch'
      ? new Promise<Response>(() => undefined)
      : Promise.resolve(new Response(new ReadableStream(), { headers: { 'Content-Type': 'application/json' } })));
    const work = createActivationReturnApi({ fetch, csrf: () => 'csrf' }).start(journey);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(await work).toBe('unconfirmed');
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('requires JSON content type before interpreting a ready response', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(new Response(JSON.stringify({
      state: 'ready', journey_id: journey, entry_id: entry,
    }), { headers: { 'Content-Type': 'text/html' } }));
    expect(await createActivationReturnApi({ fetch }).start(journey)).toBe('unconfirmed');
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('does not dispatch when the page retires during the initial read', async () => {
    let finish!: (value: Response) => void;
    const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    let current = true;
    const work = createActivationReturnApi({ fetch, csrf: () => 'csrf' }).start(journey, () => current);
    current = false;
    finish(response('ready'));
    expect(await work).toBe('none');
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(sessionStorage.length).toBe(0);
  });

  it('retains a current subscriber when React replaces an effect during its read', async () => {
    let finish!: (value: Response) => void;
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }))
      .mockResolvedValueOnce(response('checking'));
    const api = createActivationReturnApi({ fetch, csrf: () => 'csrf' });
    let current = true;
    const retired = api.start(journey, () => current);
    current = false;
    const active = api.start(journey, () => true);
    finish(response('ready'));
    expect(await Promise.all([retired, active])).toEqual(['checking', 'checking']);
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
