import { describe, expect, it } from 'vitest';
import { parseEvidenceContext } from '../src/analytics/questionContract';
import { findingsSource, findingsResponse, findingsBaselineFixture as baseline } from '../src/story-only/findingsFixtures';
const reference = findingsResponse().evidence_page.items[0].reference;
const source = findingsSource(reference);
const request = { reference, expected_generation: baseline.projection_generation, expected_projection_digest: baseline.projection_digest, before: 5, after: 5 };
function response() { return { schema_version: 'analytics-evidence-context.v1', reference, projection_generation: request.expected_generation,
  projection_digest: request.expected_projection_digest, checked_at: source.checked_at, anchor_index: 0,
  items: [{ reference, text: source.text, direction: source.direction, browser_span: source.browser_span }],
  has_earlier_stored_items: true, has_later_stored_items: false }; }
describe('saved evidence context contract', () => {
  it('accepts equal-time stored neighbors without ordering opaque references and rejects invalid neighbors', () => {
    const neighbor = { reference: { ...reference, message_ref: `m1:${'0'.repeat(64)}`, span: null }, text: 'A saved reply', direction: source.direction, browser_span: null };
    const value = { ...response(), anchor_index: 1, items: [neighbor, response().items[0], { ...neighbor, reference: { ...neighbor.reference, message_ref: `m1:${'f'.repeat(64)}` } }] };
    expect(parseEvidenceContext(value, request, source).items).toHaveLength(3);
    for (const field of ['account_ref', 'conversation_ref', 'source_revision'] as const) {
      const invalid = structuredClone(value);
      Object.assign(invalid.items[0].reference, { [field]: field === 'source_revision' ? reference.source_revision + 1 : `${field === 'account_ref' ? 'a1' : 'c1'}:${'9'.repeat(64)}` });
      expect(() => parseEvidenceContext(invalid, request, source)).toThrow();
    }
    expect(() => parseEvidenceContext(value, { ...request, before: 0 }, source)).toThrow();
    expect(() => parseEvidenceContext(value, { ...request, after: 0 }, source)).toThrow();
    expect(() => parseEvidenceContext({ ...value, extra: true }, request, source)).toThrow();
    expect(() => parseEvidenceContext({ ...value, projection_digest: `sha256:${'9'.repeat(64)}` }, request, source)).toThrow();
    const reversed = structuredClone(value); reversed.items[0].reference.sent_at = '2099-01-01T00:00:00Z';
    expect(() => parseEvidenceContext(reversed, request, source)).toThrow();
    const spanned = structuredClone(value); spanned.items[0].reference.span = { start: 0, end: 1 };
    expect(() => parseEvidenceContext(spanned, request, source)).toThrow();
  });
  it('verifies Unicode codepoint spans as UTF-16 offsets', () => {
    const unicodeRef = { ...reference, span: { start: 1, end: 2 } };
    const unicodeSource = { ...source, reference: unicodeRef, text: 'A\u{1f600}B', browser_span: { start: 1, end: 3 } };
    const value = { ...response(), reference: unicodeRef, items: [{ reference: unicodeRef,
      text: unicodeSource.text, direction: unicodeSource.direction, browser_span: unicodeSource.browser_span }] };
    const input = { ...request, reference: unicodeRef };
    expect(parseEvidenceContext(value, input, unicodeSource).items[0].browser_span?.end).toBe(3);
    value.items[0].browser_span.end = 2;
    expect(() => parseEvidenceContext(value, input, unicodeSource)).toThrow();
  });

  it('binds the exact verified anchor and rejects changed text, generation, bounds, duplicates and native locations', () => {
    expect(parseEvidenceContext(response(), request, source).items).toHaveLength(1);
    const mutations = [
      (v: ReturnType<typeof response>) => { v.items[0].text += 'changed'; },
      (v: ReturnType<typeof response>) => { v.anchor_index = 1; },
      (v: ReturnType<typeof response>) => { v.projection_generation += 1; },
      (v: ReturnType<typeof response>) => { v.items.push(v.items[0]); },
      (v: ReturnType<typeof response>) => { Object.assign(v.items[0], { location: source.location }); },
      (v: ReturnType<typeof response>) => { v.items[0].browser_span = { start: 999, end: 1000 }; },
    ];
    for (const mutate of mutations) { const value = structuredClone(response()); mutate(value);
      expect(() => parseEvidenceContext(value, request, source)).toThrow(); }
    expect(() => parseEvidenceContext(response(), { ...request, before: 6 }, source)).toThrow();
  });
});
