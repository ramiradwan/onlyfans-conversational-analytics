import type { QuestionCatalog, QuestionPlan, QuestionResult, SourceEvidence } from '../src/analytics/questionContract';
import type { MessagePageResponse } from '../src/protocol';
import { createBridgeTransportStore } from '../src/store/transportStore';

export const ACCOUNT = 'synthetic-question-account';
export const ACCOUNT_REF = 'a1:f4b9e6888355be647493a70419d2b6320d6d58375f062450acb63246a8ff202f';
export const NOW = new Date('2026-09-19T12:00:00Z');
const digest = `sha256:${'1'.repeat(64)}`;
export const catalog: QuestionCatalog = { questions: [
  { question: 'no_later_creator_reply.v1', enabled: true, limitations: ['synthetic'] },
  { question: 'pricing_discussions.v1', enabled: false, reason: 'analytics_pricing_not_qualified' },
] };
export const plan: QuestionPlan = { question: 'no_later_creator_reply.v1',
  start: '2026-09-18T00:00:00Z', end: NOW.toISOString(), cutoff: NOW.toISOString(), timezone: 'UTC', page_size: 50 };
export function answer(request: QuestionPlan = plan, index = 1): QuestionResult {
  const conversation = `c1:${String(index).repeat(64)}`;
  const reference = { account_ref: ACCOUNT_REF, conversation_ref: conversation,
    message_ref: `m1:${String(index).repeat(64)}`, source_revision: 7,
    source_version_digest: digest, sent_at: '2026-09-19T10:00:00Z', span: null };
  return { schema_version: 'analytics-question-result.v1', availability: 'available', counting_unit: 'conversations',
    question: { plan: { ...request, cursor: null, cutoff: NOW.toISOString() }, account_ref: ACCOUNT_REF,
      cutoff: NOW.toISOString(), retention_cutoff_exclusive: '2026-06-21T12:00:00Z', selection_clipped_by_retention: false,
      definition_digest: digest, snapshot: { account_ref: ACCOUNT_REF, source_revision: 7, projection_generation: 2,
        generation_id: 'synthetic-generation', canonical_content_digest: digest, projection_digest: digest,
        derived_at: '2026-09-19T11:55:00Z', source_message_count: 3, retention_due_at: '2026-09-20T12:00:00Z' } },
    page: { rows: [{ account_ref: ACCOUNT_REF, conversation_ref: conversation,
      latest_evidence_at: reference.sent_at, reason: 'no_later_creator_reply', coverage: 'partial',
      evidence: [reference], evidence_truncated: false }],
      coverage: { history: 'partial', ordering: 'inferred', supported_languages: [] },
      evaluated_conversation_count: 1, undetermined_conversation_count: 0, total_matching_conversations: 1,
      has_more: false, truncated: false }, checked_at: NOW.toISOString(), next_cursor: null };
}
export function evidence(): SourceEvidence {
  return { reference: answer().page.rows[0].evidence[0],
    location: { conversation_id: 'synthetic-chat', message_id: 'synthetic-message' },
    text: 'Synthetic <img src=x onerror=alert(1)>', direction: 'inbound', checked_at: NOW.toISOString(), browser_span: null };
}
export function context() {
  return { ...createBridgeTransportStore().getState(), creatorAccountId: ACCOUNT, viewRevision: 7,
    connection: 'connected' as const, readModelState: 'realtime' as const,
    projection: { status: 'current' as const, canonical_revision: 7, projected_revision: 7, projected_at: NOW.toISOString(), reason: null } };
}
export function thread(): MessagePageResponse {
  return { creator_account_id: ACCOUNT, conversation_id: 'synthetic-chat', projection_generation: 'synthetic-history',
    read_revision: 7, generated_at: NOW.toISOString(), items: [{ message_id: 'synthetic-message',
      text: 'Synthetic saved conversation', sent_at: '2026-09-19T10:00:00Z', direction: 'inbound', sentiment: 'unknown' }],
    older_cursor: null, has_older_stored_items: false,
    conversation_coverage: { status: 'partial', boundary: null, earliest_available_at: null,
      latest_acquired_at: NOW.toISOString(), data_as_of: NOW.toISOString(), reason_code: null },
    projection: context().projection };
}
