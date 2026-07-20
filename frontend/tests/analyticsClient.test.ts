import { describe, expect, it, vi } from 'vitest';

import { AnalyticsClientError, buildAnalyticsUrl, fetchAnalyticsUpdate } from '../src/analytics';
import { analyticsUpdateFixture } from './analyticsFixture';

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('analytics client boundary', () => {
  it('uses the exact account-free insights path, UTC boundaries, and same-origin credentials', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(analyticsUpdateFixture()));

    await fetchAnalyticsUpdate({
      range: { startDate: '2026-06-01', endDate: '2026-06-30' },
      fetcher,
    });

    expect(fetcher).toHaveBeenCalledTimes(1);
    const [calledUrl, calledInit] = fetcher.mock.calls[0];
    const url = new URL(String(calledUrl));
    expect(url.pathname).toBe('/api/v1/insights/full');
    expect(url.searchParams.get('start_date')).toBe('2026-06-01T00:00:00.000Z');
    expect(url.searchParams.get('end_date')).toBe('2026-06-30T23:59:59.999Z');
    expect(url.search).not.toContain('account');
    expect(url.search).not.toContain('creator');
    expect(calledInit).toMatchObject({ credentials: 'same-origin', headers: { Accept: 'application/json' } });
    expect(calledInit?.headers).not.toHaveProperty('X-OFCA-Auth-Ticket');
  });

  it('maps structured authentication, availability and validation errors without retaining bodies', async () => {
    const cases = [
      [401, 'authentication_failed', null, false],
      [403, 'account_binding_mismatch', null, false],
      [404, 'analytics_unavailable', 'unavailable', false],
      [422, 'invalid_window', null, false],
      [503, 'projection_building', 'building', true],
    ] as const;

    for (const [status, code, availability, retryable] of cases) {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
        response({ detail: { code, message: `safe ${status}`, availability, retryable } }, status),
      );
      const thrown = await fetchAnalyticsUpdate({ fetcher }).catch((error: unknown) => error);
      expect(thrown).toBeInstanceOf(AnalyticsClientError);
      expect(thrown).toMatchObject({ status, code, message: `safe ${status}`, availability, retryable });
      expect(thrown).not.toHaveProperty('body');
      expect(thrown).not.toHaveProperty('response');
    }
  });

  it('buildAnalyticsUrl never adds identity query parameters', () => {
    const url = new URL(buildAnalyticsUrl('', { startDate: '', endDate: '' }));
    expect(url.pathname).toBe('/api/v1/insights/full');
    expect(url.search).toBe('');
  });
});
