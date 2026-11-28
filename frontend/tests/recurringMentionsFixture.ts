import savedExample from '../../shared/nlp/application/v1/fixtures/preview-saved-recurring.json';
import type { RecurringReportRequest, RecurringReport } from '../src/analytics/recurringMentions';
import { findingsBaselineFixture } from '../src/story-only/findingsFixtures';

export const phraseId = `sha256:${'3'.repeat(64)}`;
export const normalization = `sha256:${'4'.repeat(64)}`;
export const reportRequest: RecurringReportRequest = {
  baseline: findingsBaselineFixture, phraseId: null, normalization: null, rowOffset: 0, evidenceOffset: 0, limit: 20,
};
export function reportFixture(request = reportRequest): RecurringReport {
  const b = request.baseline;
  const rows = [{ phrase_id: phraseId, literal_key: 'item', supporting_senders: 2, supporting_messages: 2, occurrences: 2 }];
  const refs = ['a', 'b'].map((id) => ({
    phrase_id: phraseId, assessment: 'complete' as const,
    reference: { account_ref: b.account_ref, conversation_ref: `c1:${id.repeat(64)}`, message_ref: `m1:${id.repeat(64)}`,
      sent_at: '2026-09-19T10:00:00Z', source_revision: b.source_revision, source_version_digest: `sha256:${'1'.repeat(64)}`, span: { start: 5, end: 9 } },
  }));
  const page = <T,>(items: T[], offset: number) => ({ offset, limit: request.limit, total: items.length,
    has_more: offset + request.limit < items.length, items: items.slice(offset, offset + request.limit) });
  return {
    schema_version: 'analytics-recurring-mentions.v1', account_ref: b.account_ref, source_revision: b.source_revision,
    projection_generation: b.projection_generation, projection_digest: b.projection_digest,
    snapshot_sha256: `sha256:${'5'.repeat(64)}`, normalization_sha256: normalization, phrase_id: request.phraseId,
    requested_window: { scope: 'requested', start: '2026-06-22T00:00:00Z', end: '2026-09-20T00:00:00Z' },
    effective_window: { scope: 'effective', start: refs[0].reference.sent_at, end: refs[1].reference.sent_at },
    availability: 'available', unavailable_reason: null,
    coverage: { eligible_messages: 3, complete_messages: 2, partial_messages: 0, failed_messages: 0,
      pending_messages: 0, uncertain_messages: 1, unsupported_messages: 0, scan_complete: true, unrepresentable_occurrences: 0 },
    row_page: page(rows, request.rowOffset), evidence_page: page(refs, request.evidenceOffset),
  };
}

export function recurringSavedFixture() { return structuredClone(savedExample); }
