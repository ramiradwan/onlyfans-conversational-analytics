import { describe, expect, it } from 'vitest';

import { findingsTaskAvailability, parseFindings, type FindingsResponseV2 } from '../src/analytics/findingsContract';
import { parseSourceEvidence } from '../src/analytics/questionContract';
import { findingsRequest, findingsResponse, findingsSource, findingsSubsetResponse } from '../src/story-only/findingsFixtures';

describe('findings contract', () => {
  it('validates both tasks and retains separate coverage and counting units', () => {
    const value = parseFindings(findingsResponse(), findingsRequest);
    expect(value.tasks[1].rows[0].occurrences).toBe(2);
    expect(value.tasks[1].rows[0].supporting_messages).toBe(1);
    expect(value.tasks[0].uncertain_messages).toBe(1);
  });
  it('accepts declared subsets without inventing results for an unavailable task', () => {
    for (const declared of [['concept_mentions'], ['message_topics'], ['message_topics', 'concept_mentions']] as const) {
      const value = parseFindings(findingsSubsetResponse(findingsRequest, [...declared]), findingsRequest);
      expect(value.schema_version).toBe('analytics-findings.v2');
      expect(value.tasks.map((item) => item.task)).toEqual(declared);
      expect(value.comparison.tasks.map((item) => item.task)).toEqual(declared);
      expect(findingsTaskAvailability(value).filter((item) => item.availability === 'declared').map((item) => item.task)).toEqual(declared);
      expect(value.evidence_page.items.every((item) => value.tasks.some((task) => task.task === item.task))).toBe(true);
    }
  });
  it('keeps the legacy format strict and requires the explicit new version', () => {
    const legacy = findingsResponse(); legacy.tasks = legacy.tasks.slice(1);
    expect(() => parseFindings(legacy, findingsRequest)).toThrow();
    const current = findingsSubsetResponse();
    expect(() => parseFindings({ ...current, schema_version: 'analytics-findings.v1' }, findingsRequest)).toThrow();
    expect(() => parseFindings({ ...current, schema_version: 'analytics-findings.v3' }, findingsRequest)).toThrow();
    const { task_availability: ignored, ...missing } = current;
    expect(ignored).toHaveLength(2);
    expect(() => parseFindings(missing, findingsRequest)).toThrow();
    expect(findingsTaskAvailability(parseFindings(findingsResponse(), findingsRequest)).every((item) => item.availability === 'declared')).toBe(true);
  });
  it('rejects empty or contradictory task declarations and mismatched comparisons', () => {
    const changes: ((value: FindingsResponseV2) => void)[] = [
      (value) => { value.tasks = []; },
      (value) => { value.tasks.push(value.tasks[0]); },
      (value) => { value.task_availability = []; },
      (value) => { value.task_availability[0] = value.task_availability[1]; },
      (value) => { value.task_availability[0].availability = 'declared'; },
      (value) => { value.task_availability[1].availability = 'not_declared'; },
      (value) => { value.comparison.tasks = []; },
      (value) => { value.comparison.tasks = [findingsResponse().comparison.tasks[0]]; },
      (value) => { value.comparison.tasks.push(value.comparison.tasks[0]); },
    ];
    for (const change of changes) { const value = findingsSubsetResponse(); change(value); expect(() => parseFindings(value, findingsRequest)).toThrow(); }
  });
  it('rejects evidence for undeclared tasks and preserves separate repeated occurrences', () => {
    const value = findingsSubsetResponse();
    const parsed = parseFindings(value, findingsRequest);
    expect(parsed.evidence_page.items.map((item) => item.reference.span)).toEqual([{ start: 5, end: 9 }, { start: 14, end: 18 }]);
    value.evidence_page.items.push(findingsResponse().evidence_page.items[2]); value.evidence_page.total += 1;
    expect(() => parseFindings(value, findingsRequest)).toThrow();
  });
  it('checks subset evidence pagination against the complete declared task inventory', () => {
    const request = { ...findingsRequest, evidence_offset: 1, evidence_limit: 1 };
    const value = parseFindings(findingsSubsetResponse(request), request);
    expect(value.evidence_page).toMatchObject({ offset: 1, limit: 1, total: 2, has_more: false });
    expect(value.evidence_page.items[0].reference.span).toEqual({ start: 14, end: 18 });
  });
  it.each(['account_ref', 'source_revision', 'projection_generation', 'projection_digest', 'canonical_content_digest', 'graph_digest', 'pipeline_revision', 'pipeline_config_digest', 'pipeline_identity_digest'] as const)('rejects mismatched %s', (field) => {
    const value = findingsResponse();
    Object.assign(value, { [field]: typeof value[field] === 'number' ? 99 : 'mismatched' });
    expect(() => parseFindings(value, findingsRequest)).toThrow();
  });
  it('rejects a changed window direction page taxonomy fallback or duplicate task', () => {
    const changes = [
      (value: ReturnType<typeof findingsResponse>) => { value.requested_window.start = '2025-01-01T00:00:00Z'; },
      (value: ReturnType<typeof findingsResponse>) => { value.requested_window.start = '2026-09-18T00:00:00.000001Z'; },
      (value: ReturnType<typeof findingsResponse>) => { value.direction = 'outbound'; },
      (value: ReturnType<typeof findingsResponse>) => { value.evidence_page.has_more = true; },
      (value: ReturnType<typeof findingsResponse>) => { value.tasks[1].rows[0].display_label = 'Guessed label'; },
      (value: ReturnType<typeof findingsResponse>) => { value.tasks[1] = value.tasks[0]; },
    ];
    for (const change of changes) { const value = findingsResponse(); change(value); expect(() => parseFindings(value, findingsRequest)).toThrow(); }
  });
  it('validates exact repeated Unicode spans without changing codepoint references', () => {
    const first = findingsResponse().evidence_page.items[0].reference;
    const second = findingsResponse().evidence_page.items[1].reference;
    const source = parseSourceEvidence(findingsSource(second), second);
    expect(second.span).toEqual({ start: 14, end: 18 });
    expect(source.browser_span).toEqual({ start: 15, end: 19 });
    expect(() => parseSourceEvidence(findingsSource(first), second)).toThrow();
    expect(() => parseSourceEvidence({ ...source, browser_span: second.span }, second)).toThrow();
  });
  it('rejects out-of-window or duplicate exact evidence', () => {
    const value = findingsResponse(); value.evidence_page.items[0].reference.sent_at = '2026-01-01T00:00:00Z';
    expect(() => parseFindings(value, findingsRequest)).toThrow();
    const duplicate = findingsResponse(); duplicate.evidence_page.items[1] = duplicate.evidence_page.items[0];
    expect(() => parseFindings(duplicate, findingsRequest)).toThrow();
  });
  it('requires an adjacent equal-duration comparison with independent coverage', () => {
    const value = parseFindings(findingsResponse(), findingsRequest);
    const previous = value.comparison;
    expect(previous.boundary_policy).toBe('previous_end_exclusive');
    expect(Date.parse(previous.requested_window!.end!)).toBe(Date.parse(value.requested_window.start!));
    expect(previous.tasks[0].complete_messages).toBe(0);
    expect(previous.tasks[0].rows[0].share_of_complete_messages).toBeNull();
    const overlap = findingsResponse(); overlap.comparison.requested_window!.end = value.requested_window.end;
    expect(() => parseFindings(overlap, findingsRequest)).toThrow();
    const changed = findingsResponse(); changed.comparison.tasks[0].component_sha256 = `sha256:${'9'.repeat(64)}`;
    expect(() => parseFindings(changed, findingsRequest)).toThrow();
    const denominator = findingsResponse(); denominator.comparison.tasks[0].rows[0].share_of_complete_messages = 0;
    expect(() => parseFindings(denominator, findingsRequest)).toThrow();
  });
  it('accepts an explicitly unavailable unbounded comparison and enforces row bounds', () => {
    const request = structuredClone(findingsRequest); request.baseline.requested_window = { scope: 'requested', start: null, end: null };
    const value = findingsResponse(); value.requested_window = request.baseline.requested_window;
    value.comparison = { availability: 'unavailable', unavailable_reason: 'unbounded_window', boundary_policy: 'previous_end_exclusive', requested_window: null, effective_window: null, tasks: [] };
    expect(parseFindings(value, request).comparison.availability).toBe('unavailable');
    const many = findingsResponse(); many.tasks[0].rows = Array.from({ length: 65 }, (_, index) => ({ ...many.tasks[0].rows[0], category_id: `synthetic-${index}` }));
    expect(() => parseFindings(many, findingsRequest)).toThrow();
  });
  it('preserves microsecond precision in valid adjacent comparison windows', () => {
    const request = structuredClone(findingsRequest);
    request.baseline.requested_window = { scope: 'requested', start: '2026-09-18T00:00:00.000001Z', end: '2026-09-18T00:00:00.000003Z' };
    const value = findingsResponse(); value.requested_window = request.baseline.requested_window;
    value.effective_window = { ...request.baseline.requested_window, scope: 'effective' };
    value.comparison.requested_window = { scope: 'requested', start: '2026-09-17T23:59:59.999999Z', end: request.baseline.requested_window.start };
    value.evidence_page = { ...value.evidence_page, items: [], total: 0 };
    for (const task of value.tasks) for (const row of task.rows) Object.assign(row, { supporting_messages: 0, supporting_conversations: 0, occurrences: 0, complete_supporting_messages: 0, partial_supporting_messages: 0, share_of_complete_messages: 0 });
    expect(parseFindings(value, request).comparison.requested_window).toEqual(value.comparison.requested_window);
    value.comparison.requested_window.start = '2026-09-17T23:59:59.999998Z';
    expect(() => parseFindings(value, request)).toThrow();
  });
  it('rejects inconsistent coverage support counting units denominators and prior bounds', () => {
    const changes = [
      (value: ReturnType<typeof findingsResponse>) => { value.tasks[0].pending_messages += 1; },
      (value: ReturnType<typeof findingsResponse>) => { value.tasks[0].rows[0].supporting_messages += 1; },
      (value: ReturnType<typeof findingsResponse>) => { value.tasks[0].rows[0].occurrences += 1; },
      (value: ReturnType<typeof findingsResponse>) => { value.tasks[1].rows[0].occurrences = 0; },
      (value: ReturnType<typeof findingsResponse>) => { value.tasks[0].rows[0].share_of_complete_messages = null; },
      (value: ReturnType<typeof findingsResponse>) => { value.tasks[0].rows[0].share_of_complete_messages = 0.25; },
      (value: ReturnType<typeof findingsResponse>) => { value.comparison.effective_window = { scope: 'effective', start: value.requested_window.start, end: value.requested_window.start }; },
    ];
    for (const change of changes) { const value = findingsResponse(); change(value); expect(() => parseFindings(value, findingsRequest)).toThrow(); }
  });
});
