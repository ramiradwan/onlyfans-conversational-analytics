import { describe, expect, it, vi } from 'vitest';

import { buildLiteralsUrl, createLiteralsClient } from '../src/analytics/literalsClient';
import { literalsRequest, literalsResponse } from '../src/story-only/literalsFixtures';

describe('literal findings client', () => {
  it('uses the authenticated read and only supported query parameters', async () => {
    const request = { ...literalsRequest, category_filter: { task: 'concept_mentions' as const, category_id: 'synthetic-mention' },
      node_id: `sha256:${'2'.repeat(64)}`, expected_normalization_sha256: literalsResponse().normalization_sha256 };
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(literalsResponse(request))));
    await createLiteralsClient({ fetcher }).read(request);
    const url = new URL(buildLiteralsUrl(request), 'https://example.invalid');
    expect(url.pathname).toBe('/api/v1/insights/findings/literals');
    expect(url.searchParams.get('node_id')).toBe(request.node_id);
    expect(url.searchParams.get('category_task')).toBe('concept_mentions');
    expect(url.searchParams.get('expected_normalization_sha256')).toBe(request.expected_normalization_sha256);
    expect(url.searchParams.has('conversation_offset')).toBe(false);
    expect(url.searchParams.has('conversation_limit')).toBe(false);
    expect(fetcher.mock.calls[0][1]).toMatchObject({ credentials: 'same-origin', cache: 'no-store', redirect: 'error' });
  });
  it('rejects malformed, oversized and unsuccessful responses', async () => {
    for (const response of [new Response('{}'), new Response('x'.repeat(1_048_577)), new Response(JSON.stringify({ detail: { code: 'analytics_projection_building' } }), { status: 503 })]) {
      await expect(createLiteralsClient({ fetcher: vi.fn().mockResolvedValue(response) }).read(literalsRequest)).rejects.toThrow();
    }
  });
});
