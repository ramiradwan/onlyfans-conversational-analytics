import { describe, expect, it } from 'vitest';

import { literalKey, parseLiterals, type LiteralFindings } from '../src/analytics/literalsContract';
import { literalsRequest, literalsResponse } from '../src/story-only/literalsFixtures';

describe('literal findings contract', () => {
  it('keeps normalization separate from extraction and accepts evidence outside the current row page', () => {
    const request = { ...literalsRequest, row_limit: 1 };
    const value = parseLiterals(literalsResponse(request), request);
    expect(value.row_page.items).toHaveLength(1);
    expect(value.evidence_page.items.some((e) => e.node_id !== value.row_page.items[0].node_id)).toBe(true);
    expect(value.normalization_coverage).toEqual({ eligible_occurrences: 4, mapped_occurrences: 3, unrepresentable_occurrences: 1, supporting_messages: 2 });
    const selected = { ...request, node_id: value.row_page.items[0].node_id, expected_normalization_sha256: value.normalization_sha256 };
    expect(parseLiterals(literalsResponse(selected), selected).evidence_page.total).toBe(2);
    expect(parseLiterals(literalsResponse(selected), selected).row_page.total).toBe(2);
  });
  it('checks Unicode codepoints, NFC and the exact strip policy without case folding', () => {
    const value = literalsResponse(); value.row_page.items[0].literal_key = '😀'.repeat(256);
    expect(parseLiterals(value, literalsRequest).row_page.items[0].literal_key).toHaveLength(512);
    for (const key of ['😀'.repeat(257), '', ' cafe\u0301 ', 'cafe\u0301', ' item', 'item\u0085', '\ud800']) {
      value.row_page.items[0].literal_key = key;
      expect(() => parseLiterals(value, literalsRequest)).toThrow();
    }
    expect(literalKey(' cafe\u0301 ')).toBe('café');
    expect(literalKey('Item')).toBe('Item');
    value.row_page.items[0].literal_key = '\ufeffitem';
    expect(parseLiterals(value, literalsRequest).row_page.items[0].literal_key).toBe('\ufeffitem');
  });
  it('retains extraction on unavailable normalization and distinguishes an undeclared task', () => {
    const value = literalsResponse(); value.availability = 'unavailable'; value.unavailable_reason = 'not_prepared';
    value.normalization_sha256 = null; value.normalization_policy = null; value.normalization_coverage = null;
    value.row_page = { ...value.row_page, total: 0, items: [], has_more: false };
    value.evidence_page = { ...value.evidence_page, total: 0, items: [], has_more: false };
    expect(parseLiterals(value, literalsRequest).extraction).not.toBeNull();
    value.unavailable_reason = 'task_not_declared';
    expect(() => parseLiterals(value, literalsRequest)).toThrow(); value.extraction = null;
    expect(parseLiterals(value, literalsRequest).extraction).toBeNull();
  });
  const mutations: [string, (v: LiteralFindings) => void][] = [
    ['source revision', (v) => { v.source_revision += 1; }],
    ['snapshot field', (v) => { v.snapshot_sha256 = 'wrong'; }],
    ['generation', (v) => { v.projection_generation += 1; }],
    ['direction', (v) => { v.direction = 'outbound'; }],
    ['category', (v) => { v.category_filter = { task: 'message_topics', category_id: 'other' }; }],
    ['node echo', (v) => { v.node_id = v.row_page.items[0].node_id; }],
    ['window', (v) => { v.requested_window.end = '2026-09-20T00:00:00Z'; }],
    ['effective window', (v) => { v.effective_window.start = '2026-09-17T00:00:00Z'; }],
    ['coverage', (v) => { v.normalization_coverage!.mapped_occurrences += 1; }],
    ['extraction accounting', (v) => { v.extraction!.uncertain_messages += 1; }],
    ['row count', (v) => { v.row_page.items[0].supporting_messages += 1; }],
    ['row order', (v) => { v.row_page.items.reverse(); }],
    ['row duplicates', (v) => { v.row_page.items[1] = v.row_page.items[0]; }],
    ['label identity', (v) => { v.row_page.items[0].display_label = 'other'; }],
    ['page total', (v) => { v.row_page.total += 1; }],
    ['page offset', (v) => { v.evidence_page.offset = 1; }],
    ['duplicate evidence', (v) => { v.evidence_page.items[1] = v.evidence_page.items[0]; }],
    ['evidence order', (v) => { v.evidence_page.items.reverse(); }],
    ['evidence account', (v) => { v.evidence_page.items[0].reference.account_ref = `a1:${'f'.repeat(64)}`; }],
    ['evidence source', (v) => { v.evidence_page.items[0].reference.source_revision += 1; }],
    ['evidence category', (v) => { v.evidence_page.items[0].category_id = 'other'; }],
    ['missing exact span', (v) => { v.evidence_page.items[0].reference.span = null; }],
    ['unknown field', (v) => { Object.assign(v, { percentage: 1 }); }],
    ['nonfinite count', (v) => { v.row_page.total = Infinity; }],
  ];
  it.each(mutations)('rejects %s drift', (_name, mutate) => {
    const value = literalsResponse(); mutate(value); expect(() => parseLiterals(value, literalsRequest)).toThrow();
  });
  it('requires normalization pin on pages and selections and rejects publication drift', () => {
    const request = { ...literalsRequest, row_offset: 1, row_limit: 1 };
    expect(() => parseLiterals(literalsResponse(request), request)).toThrow();
    const pinned = { ...request, expected_normalization_sha256: literalsResponse().normalization_sha256 };
    expect(parseLiterals(literalsResponse(pinned), pinned).row_page.items).toHaveLength(1);
    const value = literalsResponse(pinned); value.normalization_sha256 = `sha256:${'a'.repeat(64)}`;
    expect(() => parseLiterals(value, pinned)).toThrow();
  });
  it('accepts another literal type in a message selected by a concept category', () => {
    const request = { ...literalsRequest, category_filter: { task: 'concept_mentions' as const, category_id: 'synthetic-mention' } };
    const value = literalsResponse(request);
    const other = { ...value.extraction!.rows[0], category_id: 'synthetic-other', display_label: 'synthetic-other',
      supporting_messages: 1, occurrences: 1, partial_supporting_messages: 0 };
    value.extraction!.rows.push(other); value.extraction!.rows[0].occurrences = 3;
    Object.assign(value.row_page.items[1], { category_id: other.category_id, display_label: other.display_label, type_id: 'synthetic.other.type' });
    value.evidence_page.items[2].category_id = other.category_id;
    const result = parseLiterals(value, request);
    expect(result.row_page.items.map((r) => r.category_id)).toEqual(['synthetic-mention', 'synthetic-other']);
    expect(result.evidence_page.items[2].category_id).toBe('synthetic-other');
    const selected = { ...request, node_id: result.row_page.items[1].node_id, expected_normalization_sha256: result.normalization_sha256 };
    value.node_id = selected.node_id;
    value.evidence_page = { ...value.evidence_page, total: 1, items: [value.evidence_page.items[2]] };
    expect(parseLiterals(value, selected).evidence_page.total).toBe(1);
  });
});
