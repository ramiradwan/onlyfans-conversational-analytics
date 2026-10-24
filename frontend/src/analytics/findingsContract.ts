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
const displayLabel = z.string().refine((value) => Array.from(value).length >= 1 && Array.from(value).length <= 256);
const row = z.object({ category_id: identifier, display_label: displayLabel,
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
const safeLabel = displayLabel.refine((value) => !/\p{C}/u.test(value));
const navigationDeclaration = z.object({ schema_version: z.literal('nlp-category-navigation.v1'),
  policy_id: identifier, revision: identifier, source_presentation: policy,
  bindings: z.array(z.object({ task: taskName, task_contract: identifier, taxonomy: policy,
    enabled_ids: z.array(identifier).max(64) }).strict()).min(1).max(2),
  groups: z.array(z.object({ group_id: identifier, display_label: safeLabel, task: taskName,
    member_ids: z.array(identifier).min(1).max(64) }).strict()).max(16) }).strict();
const navigationRow = z.object({ group_id: identifier, task: taskName,
  supporting_messages: count, supporting_conversations: count, complete_supporting_messages: count,
  partial_supporting_messages: count, share_of_complete_messages: z.number().min(0).max(1).nullable() }).strict();
const groupedResponse = subsetResponse.extend({ schema_version: z.literal('analytics-findings.v4'),
  view: z.literal('grouped'), category_filter: categoryFilter.nullable(),
  navigation: z.object({ availability: z.enum(['available', 'not_declared']), policy: policy.nullable(),
    declaration: navigationDeclaration.nullable(), current: z.array(navigationRow).max(16),
    previous: z.array(navigationRow).max(16).nullable() }).strict() });
const versionedResponse = z.discriminatedUnion('schema_version', [response, subsetResponse, filteredResponse, groupedResponse]);
export type FindingsResponseV1 = z.infer<typeof response>;
export type FindingsResponseV2 = z.infer<typeof subsetResponse>;
export type FindingsResponseV3 = z.infer<typeof filteredResponse>;
export type FindingsResponseV4 = z.infer<typeof groupedResponse>;
export type NavigationDeclaration = z.infer<typeof navigationDeclaration>;
export type NavigationGroupRow = z.infer<typeof navigationRow>;
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
  view?: 'grouped';
  expected_navigation_sha256?: string | null;
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
function parseResponse(value: unknown, request: FindingsRequest): FindingsResponse {
  const checked = versionedResponse.safeParse(value);
  if (!checked.success) throw new FindingsContractError();
  const result = checked.data;
  const selected = request.category_filter ?? null;
  if (request.view === 'grouped' ? result.schema_version !== 'analytics-findings.v4'
    : result.schema_version === 'analytics-findings.v4' || request.expected_navigation_sha256 != null) throw new FindingsContractError();
  if (result.schema_version === 'analytics-findings.v4'
    ? selected === null ? result.category_filter !== null
      : result.category_filter?.task !== selected.task || result.category_filter.category_id !== selected.category_id
    : selected === null ? result.schema_version === 'analytics-findings.v3'
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

export function parseFindings(value: unknown, request: FindingsRequest): FindingsResponse {
  if (request.view === 'grouped') throw new FindingsContractError();
  return parseResponse(value, request);
}

/** The descriptor uses ASCII sorted-key JSON, including escaped UTF-16 pairs. */
export function navigationCanonicalJson(value: unknown): string {
  function ordered(item: unknown): unknown {
    if (Array.isArray(item)) return item.map(ordered);
    if (item !== null && typeof item === 'object') return Object.fromEntries(
      Object.entries(item).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([key, child]) => [key, ordered(child)]));
    return item;
  }
  return JSON.stringify(ordered(value)).replace(/[\u007f-\uffff]/g,
    (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`);
}

async function canonicalDigest(value: unknown): Promise<string> {
  const raw = new TextEncoder().encode(navigationCanonicalJson(value));
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', raw)),
    (byte) => byte.toString(16).padStart(2, '0')).join('');
}

export async function parseGroupedFindings(value: unknown, request: FindingsRequest): Promise<FindingsResponseV4> {
  const result = parseResponse(value, request);
  if (result.schema_version !== 'analytics-findings.v4') throw new FindingsContractError();
  const pin = request.expected_navigation_sha256;
  if ((pin != null && !digest.safeParse(pin).success)
    || ((request.evidence_offset > 0 || request.conversation_offset > 0) && pin == null)) throw new FindingsContractError();
  const nav = result.navigation;
  if (nav.availability === 'not_declared') {
    if (pin != null || nav.policy !== null || nav.declaration !== null || nav.current.length || nav.previous !== null) throw new FindingsContractError();
  } else {
    const declaration = nav.declaration; const identity = nav.policy;
    if (!declaration || !identity || identity.policy_id !== declaration.policy_id || identity.revision !== declaration.revision
      || JSON.stringify(declaration.source_presentation) !== JSON.stringify(result.presentation)
      || navigationCanonicalJson(declaration).length > 65_536
      || identity.sha256 !== await canonicalDigest(declaration)
      || (pin != null && pin !== `sha256:${identity.sha256}`)) throw new FindingsContractError();
    const names = declaration.bindings.map((item) => item.task);
    const sorted = [...names].sort();
    if (new Set(names).size !== names.length || names.some((name, i) => name !== sorted[i])
      || names.length !== result.tasks.length) throw new FindingsContractError();
    for (const binding of declaration.bindings) {
      const task = result.tasks.find((item) => item.task === binding.task);
      const enabled = [...binding.enabled_ids].sort();
      if (!task || task.task_contract !== binding.task_contract || JSON.stringify(task.taxonomy) !== JSON.stringify(binding.taxonomy)
        || new Set(enabled).size !== enabled.length || enabled.some((id, i) => id !== binding.enabled_ids[i])
        || JSON.stringify([...task.rows.map((row) => row.category_id)].sort()) !== JSON.stringify(enabled)) throw new FindingsContractError();
    }
    const groups = declaration.groups;
    if (new Set(groups.map((group) => group.group_id)).size !== groups.length
      || groups.reduce((total, group) => total + group.member_ids.length, 0) > 256) throw new FindingsContractError();
    for (const group of groups) {
      const binding = declaration.bindings.find((item) => item.task === group.task);
      if (!binding || new Set(group.member_ids).size !== group.member_ids.length
        || group.member_ids.some((id) => !binding.enabled_ids.includes(id))) throw new FindingsContractError();
    }
    function checkRows(rows: NavigationGroupRow[], tasks: FindingsTask[]) {
      if (rows.length !== groups.length) throw new FindingsContractError();
      for (const [i, row] of rows.entries()) {
        const group = groups[i]; const task = tasks.find((item) => item.task === group.task);
        if (!task || row.group_id !== group.group_id || row.task !== group.task) throw new FindingsContractError();
        const members = task.rows.filter((item) => group.member_ids.includes(item.category_id));
        const unionBounds = (field: 'supporting_messages' | 'supporting_conversations' | 'complete_supporting_messages' | 'partial_supporting_messages') => {
          const values = members.map((item) => item[field]);
          return row[field] >= Math.max(0, ...values) && BigInt(row[field]) <= values.reduce((a, b) => a + BigInt(b), 0n);
        };
        if (!unionBounds('supporting_messages') || !unionBounds('supporting_conversations')
          || !unionBounds('complete_supporting_messages') || !unionBounds('partial_supporting_messages')
          || row.supporting_conversations > row.supporting_messages || Boolean(row.supporting_conversations) !== Boolean(row.supporting_messages)
          || row.complete_supporting_messages > task.complete_messages
          || row.partial_supporting_messages > task.partial_messages
          || BigInt(row.supporting_messages) !== BigInt(row.complete_supporting_messages) + BigInt(row.partial_supporting_messages)
          || (task.complete_messages === 0 ? row.share_of_complete_messages !== null
            : row.share_of_complete_messages !== row.complete_supporting_messages / task.complete_messages)) throw new FindingsContractError();
      }
    }
    checkRows(nav.current, result.tasks);
    if (result.comparison.availability === 'available') {
      if (nav.previous === null) throw new FindingsContractError();
      checkRows(nav.previous, result.comparison.tasks);
    } else if (nav.previous !== null) throw new FindingsContractError();
  }
  for (const task of result.tasks) for (const row of task.rows) {
    const body = { presentation: result.presentation, task_contract: task.task_contract, taxonomy: task.taxonomy,
      category_id: row.category_id, display_label: row.display_label, label_status: row.label_status };
    if (!safeLabel.safeParse(row.display_label).success
      || row.label_identity_sha256 !== `sha256:${await canonicalDigest(body)}`) throw new FindingsContractError();
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
