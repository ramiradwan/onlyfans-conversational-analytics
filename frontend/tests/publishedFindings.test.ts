import { webcrypto } from 'node:crypto';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import comparison from '../../shared/analytics/findings-comparison.v1.json';
import evidence from '../../shared/analytics/findings-evidence.v1.json';
import published from '../../shared/analytics/findings-response.v1.json';
import type { AccountRef } from '../src/analytics/analyticsContract';
import { parseFindings, type FindingsRequest } from '../src/analytics/findingsContract';
import { expectedAccountRef, parseSourceEvidence } from '../src/analytics/questionContract';

beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => vi.unstubAllGlobals());

function requestFor(value: typeof published | typeof comparison): FindingsRequest {
  return { baseline: { account_ref: value.account_ref as AccountRef, source_revision: value.source_revision,
    projection_generation: value.projection_generation, projection_digest: value.projection_digest,
    canonical_content_digest: value.canonical_content_digest, graph_digest: value.graph_digest,
    pipeline_revision: value.pipeline_revision, pipeline_config_digest: value.pipeline_config_digest,
    pipeline_identity_digest: value.pipeline_identity_digest,
    requested_window: { scope: 'requested', start: value.requested_window.start, end: value.requested_window.end } },
  direction: 'all', conversation_ref: null, evidence_offset: 0, evidence_limit: 50, conversation_offset: 0, conversation_limit: 50 };
}
describe('published findings response fixtures', () => {
  it('accepts actual published-generation counts labels and independent prior coverage', async () => {
    const result = parseFindings(published, requestFor(published));
    expect(result.account_ref).toBe(await expectedAccountRef(evidence.account_id));
    for (const task of result.tasks) {
      expect(task.eligible_messages).toBe(8); expect(task.complete_messages).toBe(3);
      expect([task.partial_messages, task.pending_messages, task.failed_messages, task.unsupported_messages, task.uncertain_messages]).toEqual([1, 1, 1, 1, 1]);
    }
    expect(result.tasks.flatMap((task) => task.rows).some((row) => row.label_status === 'resolved')).toBe(true);
    expect(result.tasks.flatMap((task) => task.rows).some((row) => row.label_status === 'id_only')).toBe(true);
    const bounded = parseFindings(comparison, requestFor(comparison));
    expect(bounded.comparison.availability).toBe('available');
    expect(bounded.comparison.requested_window?.end).toBe(bounded.requested_window.start);
    expect(bounded.comparison.tasks[0].complete_messages).not.toBe(bounded.tasks[0].complete_messages);
  });
  it('accepts the actual live-resolver response for the second Unicode occurrence', () => {
    const result = parseFindings(published, requestFor(published));
    const reference = result.evidence_page.items.find((item) => item.task === 'concept_mentions'
      && item.reference.message_ref === evidence.resolved_evidence.reference.message_ref && item.reference.span?.start === 7)!.reference;
    const source = parseSourceEvidence(evidence.resolved_evidence, reference);
    expect(source.reference.span).toEqual({ start: 7, end: 11 });
    expect(source.browser_span).toEqual({ start: 8, end: 12 });
    expect(source.text.slice(source.browser_span!.start, source.browser_span!.end)).toBe('echo');
  });
});
