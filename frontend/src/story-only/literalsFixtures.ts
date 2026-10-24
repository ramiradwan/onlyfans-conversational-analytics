/** STORY ONLY: invented literal values and exact source occurrences. */
import { findingsRequest, findingsResponse } from './findingsFixtures';
import type { LiteralFindings, LiteralsRequest } from '../analytics/literalsContract';

export const literalsRequest: LiteralsRequest = { baseline: findingsRequest.baseline, direction: 'all', conversation_ref: null, category_filter: null,
  row_offset: 0, row_limit: 50, evidence_offset: 0, evidence_limit: 50, node_id: null, expected_normalization_sha256: null };
export function literalsResponse(request = literalsRequest): LiteralFindings {
  const source = findingsResponse({ ...findingsRequest, baseline: request.baseline, direction: request.direction, conversation_ref: request.conversation_ref });
  const { processing_mode: _mode, conversation_page: _conversations, comparison: _comparison, tasks, ...identity } = source;
  void _mode; void _conversations; void _comparison;
  const task = { ...structuredClone(tasks[1]), task: 'concept_mentions' as const, task_contract: 'concept_mentions.v1' as const };
  task.rows[0].occurrences = 4; task.rows[0].supporting_messages = 2;
  task.rows[0].partial_supporting_messages = 1;
  const base = task.rows[0]; const first = `sha256:${'2'.repeat(64)}`; const second = `sha256:${'3'.repeat(64)}`;
  const rows = [
    { ...base, node_id: first, type_id: 'synthetic.literal.type', literal_key: 'item', occurrences: 2, supporting_messages: 1, supporting_conversations: 1, complete_supporting_messages: 1, partial_supporting_messages: 0 },
    { ...base, node_id: second, type_id: 'synthetic.literal.type', literal_key: '<img>', occurrences: 1, supporting_messages: 1, supporting_conversations: 1, complete_supporting_messages: 1, partial_supporting_messages: 0 },
  ].map(({ share_of_complete_messages: _share, ...row }) => { void _share; return row; });
  const reference = source.evidence_page.items[0].reference;
  const evidence = [{ node_id: first, span: { start: 5, end: 9 } }, { node_id: first, span: { start: 14, end: 18 } }, { node_id: second, span: { start: 19, end: 24 } }]
    .filter((e) => request.node_id === null || request.node_id === e.node_id)
    .map((e) => ({ node_id: e.node_id, category_id: base.category_id, assessment: 'complete' as const, reference: { ...reference, span: e.span } }));
  return structuredClone({ ...identity, schema_version: 'analytics-literals.v1', category_filter: request.category_filter, node_id: request.node_id,
    availability: 'available', unavailable_reason: null, normalization_sha256: `sha256:${'4'.repeat(64)}`,
    normalization_policy: { policy_id: 'synthetic.literal-policy', revision: 'one', sha256: '4'.repeat(64) }, extraction: task,
    normalization_coverage: { eligible_occurrences: 4, mapped_occurrences: 3, unrepresentable_occurrences: 1, supporting_messages: 2 },
    row_page: { offset: request.row_offset, limit: request.row_limit, total: rows.length, has_more: request.row_offset + request.row_limit < rows.length,
      items: rows.slice(request.row_offset, request.row_offset + request.row_limit) },
    evidence_page: { offset: request.evidence_offset, limit: request.evidence_limit, total: evidence.length, has_more: request.evidence_offset + request.evidence_limit < evidence.length,
      items: evidence.slice(request.evidence_offset, request.evidence_offset + request.evidence_limit) } });
}
