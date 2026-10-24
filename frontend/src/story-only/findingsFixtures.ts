/** STORY ONLY: invented response and source text for isolated visual inspection. */
import type { AccountRef } from '../analytics/analyticsContract';
import type { FindingsBaseline } from '../analytics/analyticsReadModel';
import type { FindingsRequest, FindingsResponse, FindingsResponseV1, FindingsResponseV2, FindingsResponseV3, FindingsTask } from '../analytics/findingsContract';
import type { QuestionEvidence, SourceEvidence } from '../analytics/questionContract';

export const findingsAccount = 'synthetic-question-account';
const digest = `sha256:${'1'.repeat(64)}`;
const identity = { account_ref: 'a1:f4b9e6888355be647493a70419d2b6320d6d58375f062450acb63246a8ff202f' as AccountRef,
  source_revision: 7, projection_generation: 2, projection_digest: digest, canonical_content_digest: digest,
  graph_digest: digest, pipeline_revision: 'synthetic.pipeline.v1', pipeline_config_digest: digest, pipeline_identity_digest: digest };
export const findingsBaselineFixture: FindingsBaseline = { ...identity, requested_window: { scope: 'requested',
  start: '2026-09-18T00:00:00.000Z', end: '2026-09-19T23:59:59.999Z' } };
export const findingsRequest: FindingsRequest = { baseline: findingsBaselineFixture, direction: 'all', evidence_offset: 0, evidence_limit: 50, conversation_ref: null, conversation_offset: 0, conversation_limit: 50 };
const policy = { policy_id: 'synthetic-policy', revision: 'one', sha256: '1'.repeat(64) };
const reference: QuestionEvidence = { account_ref: identity.account_ref, conversation_ref: `c1:${'2'.repeat(64)}`,
  message_ref: `m1:${'3'.repeat(64)}`, source_revision: 7, source_version_digest: digest, sent_at: '2026-09-19T10:00:00Z', span: null };
export const findingsSourceText = '😀 e\u0301 item and item <img>';
export function findingsSource(value: QuestionEvidence): SourceEvidence {
  const points = Array.from(findingsSourceText);
  return { reference: value, text: findingsSourceText, direction: 'inbound', checked_at: '2026-09-19T12:00:00Z',
    location: { conversation_id: 'synthetic-conversation', message_id: 'synthetic-message' },
    browser_span: value.span ? { start: points.slice(0, value.span.start).join('').length, end: points.slice(0, value.span.end).join('').length } : null };
}
export function findingsResponse(request = findingsRequest): FindingsResponseV1 {
  const tasks: FindingsResponse['tasks'] = (['message_topics', 'concept_mentions'] as const).map((task) => ({
    task, task_contract: `${task}.v1`, component_id: 'synthetic-component', component_sha256: digest, taxonomy: policy,
    availability: 'pending', eligible_messages: 7, complete_messages: 2, partial_messages: 1,
    pending_messages: 1, failed_messages: 1, unsupported_messages: 1, uncertain_messages: 1, scan_complete: true,
    rows: [{ category_id: task === 'message_topics' ? 'synthetic-topic' : 'synthetic-mention',
      display_label: task === 'message_topics' ? 'Example topic' : 'synthetic-mention', label_status: task === 'message_topics' ? 'resolved' : 'id_only',
      label_identity_sha256: digest, supporting_messages: 1, supporting_conversations: 1, occurrences: task === 'message_topics' ? 1 : 2,
      complete_supporting_messages: 1, partial_supporting_messages: 0, share_of_complete_messages: 0.5 }],
  }));
  const evidence: FindingsResponse['evidence_page']['items'] = [
    ...[{ start: 5, end: 9 }, { start: 14, end: 18 }].map((span) => ({ task: 'concept_mentions' as const, category_id: 'synthetic-mention', assessment: 'complete' as const, reference: { ...reference, span } })),
    { task: 'message_topics', category_id: 'synthetic-topic', assessment: 'complete', reference },
  ];
  return structuredClone({ ...identity, ...request.baseline, schema_version: 'analytics-findings.v1', snapshot_sha256: digest, profile_sha256: digest, presentation: policy,
    effective_window: { scope: 'effective', start: reference.sent_at, end: reference.sent_at }, direction: request.direction, processing_mode: 'paused', tasks,
    conversation_ref: request.conversation_ref, conversation_page: { offset: request.conversation_offset, limit: request.conversation_limit, total: 1, has_more: false,
      items: request.conversation_offset === 0 ? [{ conversation_ref: reference.conversation_ref, eligible_messages: 7 }] : [] },
    comparison: { availability: 'available', unavailable_reason: null, boundary_policy: 'previous_end_exclusive',
      requested_window: { scope: 'requested', start: new Date(Date.parse(request.baseline.requested_window.start!) * 2 - Date.parse(request.baseline.requested_window.end!)).toISOString(), end: request.baseline.requested_window.start },
      effective_window: { scope: 'effective', start: null, end: null },
      tasks: tasks.map((task) => ({ ...task, availability: 'current', eligible_messages: 0, complete_messages: 0, partial_messages: 0, pending_messages: 0, failed_messages: 0, unsupported_messages: 0, uncertain_messages: 0,
        rows: task.rows.map((row) => ({ ...row, supporting_messages: 0, supporting_conversations: 0, occurrences: 0, complete_supporting_messages: 0, partial_supporting_messages: 0, share_of_complete_messages: null })) })) },
    evidence_page: { offset: request.evidence_offset, limit: request.evidence_limit, total: evidence.length,
      has_more: request.evidence_offset + request.evidence_limit < evidence.length,
      items: evidence.slice(request.evidence_offset, request.evidence_offset + request.evidence_limit) } });
}

export function findingsSubsetResponse(request = findingsRequest,
  declared: FindingsTask['task'][] = ['concept_mentions']): FindingsResponseV2 {
  const value = findingsResponse(request);
  const evidence = findingsResponse({ ...request, evidence_offset: 0, evidence_limit: 100 })
    .evidence_page.items.filter((item) => declared.includes(item.task));
  return { ...value, schema_version: 'analytics-findings.v2',
    task_availability: (['message_topics', 'concept_mentions'] as const).map((task) => ({
      task, availability: declared.includes(task) ? 'declared' : 'not_declared' })),
    tasks: value.tasks.filter((task) => declared.includes(task.task)),
    comparison: { ...value.comparison, tasks: value.comparison.tasks.filter((task) => declared.includes(task.task)) },
    evidence_page: { ...value.evidence_page, total: evidence.length,
      has_more: request.evidence_offset + request.evidence_limit < evidence.length,
      items: evidence.slice(request.evidence_offset, request.evidence_offset + request.evidence_limit) },
  };
}

export function findingsFilteredResponse(request: FindingsRequest,
  declared: FindingsTask['task'][] = ['message_topics', 'concept_mentions']): FindingsResponseV3 {
  if (!request.category_filter) throw new Error('Category required');
  const value = findingsSubsetResponse(request, declared);
  const evidence = findingsSubsetResponse({ ...request, evidence_offset: 0, evidence_limit: 100 }, declared)
    .evidence_page.items.filter((item) => item.task === request.category_filter!.task && item.category_id === request.category_filter!.category_id);
  return { ...value, schema_version: 'analytics-findings.v3', category_filter: { ...request.category_filter },
    conversation_page: evidence.length ? value.conversation_page : { ...value.conversation_page, items: [], total: 0, has_more: false },
    evidence_page: { ...value.evidence_page, total: evidence.length,
      has_more: request.evidence_offset + request.evidence_limit < evidence.length,
      items: evidence.slice(request.evidence_offset, request.evidence_offset + request.evidence_limit) } };
}
