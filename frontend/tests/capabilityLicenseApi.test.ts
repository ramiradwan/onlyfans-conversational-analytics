import { describe, expect, it, vi } from 'vitest';

import {
  CapabilityLicenseApiError,
  createCapabilityLicenseApi,
} from '../src/services/capabilityLicenseApi';

const CONTINUATION = `clr1.${'A'.repeat(43)}`;

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('capability license customer API', () => {
  it('submits only the opaque continuation and accepts only checking', async () => {
    const request = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => (
      jsonResponse({ state: 'checking' })
    ));
    const api = createCapabilityLicenseApi({
      fetch: request as unknown as typeof fetch,
      getCsrfToken: () => 'csrf-token',
    });

    await expect(api.redeem(CONTINUATION)).resolves.toEqual({ state: 'checking' });
    expect(request).toHaveBeenCalledTimes(1);
    const [url, init] = request.mock.calls[0];
    expect(url).toBe('/api/v1/capability-license/redeem');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({ continuation: CONTINUATION });
    expect(Object.keys(JSON.parse(String(init?.body)))).toEqual(['continuation']);
    expect(init?.credentials).toBe('same-origin');
    expect((init?.headers as Record<string, string>)['X-CSRF-Token']).toBe('csrf-token');
  });

  it('rejects malformed continuation before any redemption request', async () => {
    const request = vi.fn();
    const api = createCapabilityLicenseApi({
      fetch: request as unknown as typeof fetch,
      getCsrfToken: () => 'csrf-token',
    });

    await expect(api.redeem('clr1.too-short')).rejects.toBeInstanceOf(CapabilityLicenseApiError);
    expect(request).not.toHaveBeenCalled();
  });

  it('rejects a success response that exposes protected commercial fields', async () => {
    const request = vi.fn(async () => jsonResponse({
      state: 'checking',
      reference_id: 'must-not-reach-browser',
    }));
    const api = createCapabilityLicenseApi({
      fetch: request as unknown as typeof fetch,
      getCsrfToken: () => 'csrf-token',
    });

    await expect(api.redeem(CONTINUATION)).rejects.toThrow('invalid activation response');
  });

  it('accepts only the closed canonical readiness document', async () => {
    const request = vi.fn(async () => jsonResponse({
      schema: 'ofca-analysis-readiness/v1',
      commercial_authority: 'active',
      analysis_admission: 'blocked',
    }));
    const api = createCapabilityLicenseApi({ fetch: request as unknown as typeof fetch });

    await expect(api.readiness()).resolves.toEqual({
      schema: 'ofca-analysis-readiness/v1',
      commercial_authority: 'active',
      analysis_admission: 'blocked',
    });
    expect(request).toHaveBeenCalledWith('/api/v1/capability-license/readiness', expect.objectContaining({
      method: 'GET',
      cache: 'no-store',
      credentials: 'same-origin',
    }));
  });

  it('fails closed on impossible or extended canonical readiness', async () => {
    const admittedWithoutAuthority = createCapabilityLicenseApi({
      fetch: vi.fn(async () => jsonResponse({
        schema: 'ofca-analysis-readiness/v1',
        commercial_authority: 'required',
        analysis_admission: 'admitted',
      })) as unknown as typeof fetch,
    });
    await expect(admittedWithoutAuthority.readiness()).rejects.toThrow('invalid activation response');

    const extended = createCapabilityLicenseApi({
      fetch: vi.fn(async () => jsonResponse({
        schema: 'ofca-analysis-readiness/v1',
        commercial_authority: 'active',
        analysis_admission: 'admitted',
        license_id: 'must-not-reach-browser',
      })) as unknown as typeof fetch,
    });
    await expect(extended.readiness()).rejects.toThrow('invalid activation response');
  });

  it.each([
    [410, 'expired'],
    [404, 'not valid'],
    [403, 'authorized'],
    [409, 'no longer matches'],
    [503, 'existing activation remains unchanged'],
  ])('keeps redemption failure %i customer-safe and actionable', async (status, message) => {
    const api = createCapabilityLicenseApi({
      fetch: vi.fn(async () => jsonResponse({ detail: 'internal-provider-detail' }, status)) as unknown as typeof fetch,
      getCsrfToken: () => 'csrf-token',
    });

    await expect(api.redeem(CONTINUATION)).rejects.toThrow(message);
    await expect(api.redeem(CONTINUATION)).rejects.not.toThrow('internal-provider-detail');
  });
});
