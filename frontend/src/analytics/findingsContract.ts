import { z } from 'zod';

import type { FindingsBaseline } from './analyticsReadModel';
import { evidenceKey, questionEvidence } from './questionContract';

const count = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER);
const digest = z.string().regex(/^sha256:[0-9a-f]{64}$/);
const identifier = z.string().min(1).max(160).regex(/^[A-Za-z0-9_.:-]+$/);
const instant = z.string().datetime({ offset: true });
const windowSchema = z.object({ scope: z.enum(['all_time', 'requested', 'effective']),
  start: instant.nullable(), end: instant.nullable() }).strict();
const policy = z.object({ policy_id: identifier, revision: identifier, sha256: z.string().regex(/^[0-9a-f]{64}$/) }).strict();
const taskName = z.enum(['message_topics', 'concept_mentions']);
const categoryFilter = z.object({ task: taskName, category_id: identifier }).strict();
const conversationRef = z.string().regex(/^c1:[0-9a-f]{64}$/);
const row = z.object({ category_id: identifier, display_label: z.string().min(1).max(256),
  label_status: z.enum(['resolved', 'id_only']), label_identity_sha256: digest,
  supporting_messages: count, supporting_conversations: count, occurrences: count,
  complete_supporting_messages: count, partial_supporting_messages: count,
  share_of_complete_messages: z.number().min(0).max(1).nullable() }).strict();
const task = z.object({ task: taskName, task_contract: identifier, component_id: identifier,
  component_sha256: digest, taxonomy: policy, availability: z.enum(['current', 'partial', 'pending']),
  eligible_messages: count, complete_messages: count, partial_messages: count, pending_messages: count,
  failed_messages: count, unsupported_messages: count, uncertain_messages: count,
  scan_complete: z.boolean(), rows: z.array(row).max(64) }).strict();
const response = z.object({ schema_version: z.literal('analytics-findings.v1'),
  account_ref: z.string().regex(/^a1:[0-9a-f]{64}$/), source_revision: count,
  projection_generation: count.min(1), projection_digest: digest, canonical_content_digest: digest,
  graph_digest: digest, pipeline_revision: z.string().min(1), pipeline_config_digest: digest,
  pipeline_identity_digest: digest, snapshot_sha256: digest, profile_sha256: digest,
  presentation: policy, requested_window: windowSchema, effective_window: windowSchema,
  direction: z.enum(['all', 'inbound', 'outbound']), processing_mode: z.enum(['enabled', 'paused']),
  conversation_ref: conversationRef.nullable(),
  conversation_page: z.object({ offset: count, limit: count.min(1).max(100), total: count, has_more: z.boolean(),
    items: z.array(z.object({ conversation_ref: conversationRef, eligible_messages: count }).strict()).max(100) }).strict(),
  comparison: z.object({ availability: z.enum(['available', 'unavailable']),
    unavailable_reason: z.enum(['unbounded_window', 'zero_width_window', 'window_out_of_range']).nullable(),
    boundary_policy: z.literal('previous_end_exclusive'), requested_window: windowSchema.nullable(),
    effective_window: windowSchema.nullable(), tasks: z.array(task).max(2) }).strict(),
  tasks: z.array(task).length(2), evidence_page: z.object({ offset: count, limit: count.min(1).max(100),
    total: count, has_more: z.boolean(), items: z.array(z.object({ task: taskName, category_id: identifier,
      assessment: z.enum(['complete', 'partial']), reference: questionEvidence }).strict()).max(100) }).strict(),
}).strict();
const taskAvailability = z.object({ task: taskName,
  availability: z.enum(['declared', 'not_declared']) }).strict();
const subsetResponse = response.extend({ schema_version: z.literal('analytics-findings.v2'),
  tasks: z.array(task).min(1).max(2), task_availability: z.array(taskAvailability).length(2) });
const filteredResponse = subsetResponse.extend({ schema_version: z.literal('analytics-findings.v3'), category_filter: categoryFilter });
const versionedResponse = z.discriminatedUnion('schema_version', [response, subsetResponse, filteredResponse]);
export type FindingsResponseV1 = z.infer<typeof response>;
export type FindingsResponseV2 = z.infer<typeof subsetResponse>;
export type FindingsResponseV3 = z.infer<typeof filteredResponse>;
export type FindingsCategoryFilter = z.infer<typeof categoryFilter>;
export type FindingsResponse = z.infer<typeof versionedResponse>;
export type FindingsTask = FindingsResponse['tasks'][number];
export type FindingsTaskAvailability = z.infer<typeof taskAvailability>;
export type FindingsDirection = FindingsResponse['direction'];
export interface FindingsRequest {
  baseline: FindingsBaseline;
  direction: FindingsDirection;
  evidence_offset: number;
  evidence_limit: number;
  conversation_ref: string | null;
  conversation_offset: number;
  conversation_limit: number;
  category_filter?: FindingsCategoryFilter | null;
}
export class FindingsContractError extends Error {
  constructor() { super('These text analysis results could not be verified. Refresh the analytics view.'); }
}
function instantMicros(value: string): bigint {
  const fraction = value.match(/\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/)?.[1] ?? '';
  if (fraction.slice(6).replace(/0/g, '') || !Number.isFinite(Date.parse(value))) throw new FindingsContractError();
  return BigInt(Date.parse(value)) * 1000n + BigInt(fraction.padEnd(6, '0').slice(3, 6));
}
const sameInstant = (a: string | null | undefined, b: string | null | undefined) => a == null || b == null ? a == b
  : instantMicros(a) === instantMicros(b);
function checkEffective(window: FindingsResponse['effective_window'], start: bigint | null, end: bigint | null, exclusiveEnd = false) {
  if (window.scope !== 'effective' || (window.start === null) !== (window.end === null)) throw new FindingsContractError();
  if (window.start !== null && window.end !== null) {
    const first = instantMicros(window.start); const last = instantMicros(window.end);
    if (first > last || (start !== null && first < start)
      || (end !== null && (exclusiveEnd ? last >= end : last > end))) throw new FindingsContractError();
  }
}
export function parseFindings(value: unknown, request: FindingsRequest): FindingsResponse {
  const checked = versionedResponse.safeParse(value);
  if (!checked.success) throw new FindingsContractError();
  const result = checked.data;
  const selected = request.category_filter ?? null;
  if (selected === null ? result.schema_version === 'analytics-findings.v3'
    : result.schema_version !== 'analytics-findings.v3'
      || result.category_filter.task !== selected.task || result.category_filter.category_id !== selected.category_id) throw new FindingsContractError();
  if (selected && !result.tasks.some((item) => item.task === selected.task && item.rows.some((r) => r.category_id === selected.category_id))) throw new FindingsContractError();
  const { requested_window: requested, ...identity } = request.baseline;
  const start = requested.start === null ? null : instantMicros(requested.start);
  const end = requested.end === null ? null : instantMicros(requested.end);
  for (const [key, expected] of Object.entries(identity)) {
    if (result[key as keyof FindingsResponse] !== expected) throw new FindingsContractError();
  }
  const page = result.evidence_page;
  const conversations = result.conversation_page;
  const declaredTasks = new Set(result.tasks.map((item) => item.task));
  if (declaredTasks.size !== result.tasks.length || (result.schema_version !== 'analytics-findings.v1'
    && (new Set(result.task_availability.map((item) => item.task)).size !== 2
      || result.task_availability.some((item) => (item.availability === 'declared') !== declaredTasks.has(item.task))))) throw new FindingsContractError();
  if (result.direction !== request.direction || result.requested_window.scope !== requested.scope
    || !sameInstant(result.requested_window.start, requested.start) || !sameInstant(result.requested_window.end, requested.end)
    || page.offset !== request.evidence_offset || page.limit !== request.evidence_limit
    || page.items.length > page.limit || page.total < page.offset + page.items.length
    || page.has_more !== (page.offset + page.items.length < page.total)
    || (page.has_more && page.items.length === 0)
    || result.requested_window.scope !== 'requested' || result.effective_window.scope !== 'effective'
    || (result.effective_window.start === null) !== (result.effective_window.end === null)
    || (result.effective_window.start !== null && result.effective_window.end !== null
      && Date.parse(result.effective_window.start) > Date.parse(result.effective_window.end))
    ) throw new FindingsContractError();
  checkEffective(result.effective_window, start, end);
  if (result.conversation_ref !== request.conversation_ref || conversations.offset !== request.conversation_offset
    || conversations.limit !== request.conversation_limit || conversations.items.length > conversations.limit
    || conversations.total < conversations.offset + conversations.items.length
    || conversations.has_more !== (conversations.offset + conversations.items.length < conversations.total)
    || (conversations.has_more && conversations.items.length === 0)
    || new Set(conversations.items.map((item) => item.conversation_ref)).size !== conversations.items.length) throw new FindingsContractError();
  const comparison = result.comparison;
  if (comparison.availability === 'available') {
    if (start === null || end === null || end <= start || comparison.unavailable_reason !== null || comparison.tasks.length !== declaredTasks.size
      || comparison.requested_window?.scope !== 'requested' || !comparison.effective_window
      || comparison.requested_window.start === null || instantMicros(comparison.requested_window.start) !== start - (end - start)
      || !sameInstant(comparison.requested_window.end, requested.start)
      || new Set(comparison.tasks.map((item) => item.task)).size !== declaredTasks.size
      || comparison.tasks.some((item) => !declaredTasks.has(item.task))) throw new FindingsContractError();
    checkEffective(comparison.effective_window, start - (end - start), start, true);
    for (const previous of comparison.tasks) {
      const main = result.tasks.find((item) => item.task === previous.task);
      if (!main || previous.component_id !== main.component_id || previous.component_sha256 !== main.component_sha256
        || JSON.stringify(previous.taxonomy) !== JSON.stringify(main.taxonomy)
        || previous.rows.length !== main.rows.length || previous.rows.some((r) => !main.rows.some((m) =>
          m.category_id === r.category_id && m.label_identity_sha256 === r.label_identity_sha256 && m.display_label === r.display_label && m.label_status === r.label_status))) throw new FindingsContractError();
    }
  } else if (!comparison.unavailable_reason || comparison.tasks.length !== 0 || comparison.requested_window !== null
    || comparison.effective_window !== null) throw new FindingsContractError();
  for (const item of [...result.tasks, ...comparison.tasks]) {
    const covered = [item.complete_messages, item.partial_messages, item.pending_messages, item.failed_messages, item.unsupported_messages, item.uncertain_messages].reduce((total, n) => total + BigInt(n), 0n);
    if (covered !== BigInt(item.eligible_messages) || item.task_contract !== `${item.task}.v1`
      || new Set(item.rows.map((r) => r.category_id)).size !== item.rows.length) throw new FindingsContractError();
    for (const r of item.rows) {
      if ((r.label_status === 'id_only' && r.display_label !== r.category_id)
        || r.supporting_conversations > r.supporting_messages
        || r.complete_supporting_messages > item.complete_messages
        || r.partial_supporting_messages > item.partial_messages
        || BigInt(r.supporting_messages) !== BigInt(r.complete_supporting_messages) + BigInt(r.partial_supporting_messages)
        || (item.task === 'message_topics' ? r.occurrences !== r.supporting_messages : r.occurrences < r.supporting_messages)
        || (item.complete_messages === 0 ? r.share_of_complete_messages !== null
          : r.share_of_complete_messages !== r.complete_supporting_messages / item.complete_messages)) throw new FindingsContractError();
    }
  }
  const keys = new Set<string>();
  for (const item of page.items) {
    const key = JSON.stringify([item.task, item.category_id, evidenceKey(item.reference)]);
    const at = instantMicros(item.reference.sent_at);
    if (keys.has(key) || item.reference.account_ref !== result.account_ref
      || (selected !== null && (item.task !== selected.task || item.category_id !== selected.category_id))
      || item.reference.source_revision !== result.source_revision
      || (request.conversation_ref !== null && item.reference.conversation_ref !== request.conversation_ref)
      || !result.tasks.find((t) => t.task === item.task)?.rows.some((r) => r.category_id === item.category_id)
      || (item.task === 'concept_mentions' && !item.reference.span)
      || (item.task === 'message_topics' && item.reference.span != null)
      || (start !== null && at < start) || (end !== null && at > end)) throw new FindingsContractError();
    keys.add(key);
  }
  return result;
}

export function findingsSnapshot(result: FindingsResponse): string {
  return JSON.stringify({ ...result, evidence_page: undefined, conversation_page: undefined });
}

export function findingsTaskAvailability(result: FindingsResponse): FindingsTaskAvailability[] {
  return result.schema_version !== 'analytics-findings.v1' ? result.task_availability
    : (['message_topics', 'concept_mentions'] as const).map((task) => ({ task, availability: 'declared' }));
}
