import { webcrypto } from 'node:crypto';

import { afterEach, describe, expect, it, vi } from 'vitest';

import { expectedAccountRef, parseQuestionResult, parseSourceEvidence } from '../src/analytics/questionContract';
import { initialQuestionDates, makeQuestionPlan } from '../src/analytics/questionWindow';
import { createQuestionApi } from '../src/services/questionApi';
import { ACCOUNT, ACCOUNT_REF, answer, catalog, evidence, NOW, plan } from './questionFixture';

afterEach(() => vi.unstubAllGlobals());
describe('Question response contracts', () => {
  it('matches the backend account reference format', async () => {
    vi.stubGlobal('crypto', webcrypto);
    expect(await expectedAccountRef(ACCOUNT)).toBe(ACCOUNT_REF);
  });
  it('validates a source-linked result and exact evidence', () => {
    expect(parseQuestionResult(answer(), plan).page.rows).toHaveLength(1);
    expect(parseSourceEvidence(evidence(), evidence().reference).text).toContain('Synthetic');
  });
  it.each(['scope', 'revision', 'rows', 'text', 'cursor', 'counts', 'window', 'truncated'])(
    'rejects a mismatched %s response', (fault) => {
      const value = answer();
      if (fault === 'scope') value.page.rows[0].account_ref = `a1:${'9'.repeat(64)}`;
      if (fault === 'revision') value.page.rows[0].evidence[0].source_revision = 8;
      if (fault === 'rows') value.page.rows.push(value.page.rows[0]);
      if (fault === 'text') Object.assign(value.page.rows[0], { text: 'private' });
      if (fault === 'cursor') value.next_cursor = 'unexpected';
      if (fault === 'counts') value.page.evaluated_conversation_count = 0;
      if (fault === 'window') value.question.plan.start = '2026-09-16T00:00:00Z';
      if (fault === 'truncated') value.page.truncated = true;
      expect(() => parseQuestionResult(value, plan)).toThrow();
    });
  it('rejects replacement source text and invalid browser spans', () => {
    const value = evidence();
    value.reference.source_version_digest = `sha256:${'2'.repeat(64)}`;
    expect(() => parseSourceEvidence(value, evidence().reference)).toThrow();
    const unicode = evidence();
    unicode.text = 'A👋B'; unicode.reference.span = { start: 1, end: 2 };
    unicode.browser_span = { start: 1, end: 3 };
    expect(parseSourceEvidence(unicode, unicode.reference).browser_span?.end).toBe(3);
    unicode.browser_span.end = 2;
    expect(() => parseSourceEvidence(unicode, unicode.reference)).toThrow();
  });
});
describe('Question client', () => {
  it('uses fixed same-origin routes, session cookies and CSRF without account selectors', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify(answer()), { status: 200 }));
    await createQuestionApi({ fetcher, csrf: () => 'synthetic-csrf' }).answer(plan);
    const [url, options] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/v1/insights/questions');
    expect(options.credentials).toBe('same-origin'); expect(options.cache).toBe('no-store');
    expect(options.headers).toHaveProperty('X-CSRF-Token', 'synthetic-csrf');
    expect(String(options.body)).not.toContain(ACCOUNT);
  });
  it('does not submit requests without a session CSRF token', async () => {
    const fetcher = vi.fn();
    await expect(createQuestionApi({ fetcher, csrf: () => null }).answer(plan)).rejects.toThrow('sign in');
    expect(fetcher).not.toHaveBeenCalled();
  });
  it.each([401, 403, 404, 422, 503])('does not echo server text for status %i', async (status) => {
    const api = createQuestionApi({ csrf: () => 'synthetic', fetcher: async () =>
      new Response(JSON.stringify({ detail: { code: 'unknown', message: 'synthetic-private-text' } }), { status }) });
    try { await api.answer(plan); expect.fail('Request should fail'); } catch (error) {
      expect(String(error)).not.toContain('synthetic-private-text');
    }
  });
  it('accepts the disabled pricing catalog and rejects oversized responses', async () => {
    const api = createQuestionApi({ fetcher: async () => new Response(JSON.stringify(catalog)) });
    expect((await api.catalog()).questions[1].enabled).toBe(false);
    const huge = createQuestionApi({ fetcher: async () => new Response('x'.repeat(1_048_577)) });
    await expect(huge.catalog()).rejects.toThrow('verified');
  });
});
describe('Question calendar windows', () => {
  it('uses inclusive local dates and a separate follow-up cutoff', () => {
    const value = makeQuestionPlan(plan.question, '2026-09-16', '2026-09-17', NOW);
    expect(new Date(value.start).getDate()).toBe(16);
    expect(new Date(value.end).getDate()).toBe(18);
    expect(value.cutoff).toBe(NOW.toISOString());
    expect(initialQuestionDates(NOW).end).toContain('2026-09-19');
  });
  it.each([['2026-02-30', '2026-09-18'], ['', '2026-09-18'], ['2026-09-19', '2026-09-18'], ['2026-09-18', '2027-01-01']])(
    'rejects invalid dates %s to %s', (start, end) => {
      expect(() => makeQuestionPlan(plan.question, start, end, NOW)).toThrow();
    });
});
