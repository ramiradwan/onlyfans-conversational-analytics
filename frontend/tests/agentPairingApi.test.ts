import { afterEach, describe, expect, it, vi } from 'vitest';

import { requestAgentPairingTicket } from '../src/services/agentPairingApi';

function installCsrfToken(value = 'csrf-token-1') {
  const meta = document.createElement('meta');
  meta.name = 'csrf-token';
  meta.content = value;
  document.head.append(meta);
}

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  document.head.querySelector('meta[name="csrf-token"]')?.remove();
  vi.unstubAllGlobals();
});

describe('one-time Agent pairing API', () => {
  it('POSTs to the account-authenticated endpoint with same-origin credentials and CSRF', async () => {
    installCsrfToken();
    const fetch = vi.fn(async () =>
      jsonResponse({
        pairing_ticket: 'short-lived-one-time-ticket',
        expires_at: '2026-07-19T12:01:00Z',
      }),
    );
    vi.stubGlobal('fetch', fetch);
    const signal = new AbortController().signal;

    await expect(requestAgentPairingTicket(signal)).resolves.toEqual({
      pairing_ticket: 'short-lived-one-time-ticket',
      expires_at: '2026-07-19T12:01:00Z',
    });
    expect(fetch).toHaveBeenCalledOnce();
    expect(fetch).toHaveBeenCalledWith('/api/v1/agent/pairing', {
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        'X-CSRF-Token': 'csrf-token-1',
      },
      method: 'POST',
      signal,
    });
    expect(fetch.mock.calls[0][1]).not.toHaveProperty('body');
  });

  it('refuses to request a pairing credential without a CSRF token', async () => {
    const fetch = vi.fn();
    vi.stubGlobal('fetch', fetch);

    await expect(requestAgentPairingTicket()).rejects.toThrow(
      'A CSRF token is required to pair the local Agent',
    );
    expect(fetch).not.toHaveBeenCalled();
  });

  it('rejects malformed credentials instead of forwarding an unvalidated secret', async () => {
    installCsrfToken();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({
          pairing_ticket: 'ticket',
          expires_at: 'not-a-time',
          unexpected: 'field',
        }),
      ),
    );

    await expect(requestAgentPairingTicket()).rejects.toThrow(
      'Brain returned an invalid Agent pairing ticket',
    );
  });

  it('does not parse or expose an error response body', async () => {
    installCsrfToken();
    const json = vi.fn();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 403, json }) as unknown as Response),
    );

    await expect(requestAgentPairingTicket()).rejects.toThrow(
      'Agent pairing request failed (403)',
    );
    expect(json).not.toHaveBeenCalled();
  });
});
