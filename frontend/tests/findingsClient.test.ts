import { describe, expect, it, vi } from 'vitest';

import { createFindingsClient } from '../src/analytics/findingsClient';
import { findingsRequest, findingsResponse, findingsSource } from '../src/story-only/findingsFixtures';

describe('findings client', () => {
  it('uses a generation-bound cached-forbidden GET without account or mutation credentials', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(findingsResponse())));
    const csrf = vi.fn(); const api = createFindingsClient({ fetcher, csrf });
    await api.read(findingsRequest);
    const [path, options] = fetcher.mock.calls[0]; const url = new URL(path, 'https://example.invalid');
    expect(url.pathname).toBe('/api/v1/insights/findings');
    expect(url.searchParams.get('expected_generation')).toBe('2');
    expect(url.searchParams.get('expected_projection_digest')).toBe(findingsRequest.baseline.projection_digest);
    expect(url.searchParams.get('start_date')).toBe(findingsRequest.baseline.requested_window.start);
    expect(url.searchParams.has('account_id')).toBe(false);
    expect(options).toMatchObject({ method: 'GET', cache: 'no-store', redirect: 'error', credentials: 'same-origin' });
    expect(csrf).not.toHaveBeenCalled();
  });
  it('opens evidence only through the existing CSRF-protected live resolver', async () => {
    const reference = findingsResponse().evidence_page.items[1].reference;
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(findingsSource(reference))));
    await createFindingsClient({ fetcher, csrf: () => 'synthetic-token' }).evidence(reference);
    expect(fetcher).toHaveBeenCalledWith('/api/v1/insights/questions/evidence', expect.objectContaining({ method: 'POST', body: JSON.stringify(reference), headers: expect.objectContaining({ 'X-CSRF-Token': 'synthetic-token' }) }));
  });
  it.each([['analytics_projection_building', 'building'], ['analytics_projection_unavailable', 'unavailable']])('maps %s to fixed safe copy', async (code, state) => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { code, message: 'untrusted exception text' } }), { status: 503 }));
    await expect(createFindingsClient({ fetcher }).read(findingsRequest)).rejects.toMatchObject({ state });
    fetcher.mockResolvedValue(new Response(JSON.stringify({ detail: { code, message: 'untrusted exception text' } }), { status: 503 }));
    await expect(createFindingsClient({ fetcher }).read(findingsRequest)).rejects.not.toThrow('untrusted exception text');
  });
  it('rejects oversized response bodies', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(' '.repeat(1_048_577)));
    await expect(createFindingsClient({ fetcher }).read(findingsRequest)).rejects.toThrow(/could not be verified/);
  });
});
