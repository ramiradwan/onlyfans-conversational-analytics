import { z } from 'zod';

import type { FindingsBaseline } from './analyticsReadModel';
import type { FindingsCategoryFilter, FindingsDirection } from './findingsContract';
import { evidenceKey, questionEvidence } from './questionContract';

const count = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER);
const digest = z.string().regex(/^sha256:[0-9a-f]{64}$/);
const identifier = z.string().min(1).max(160).regex(/^[A-Za-z0-9_.:-]+$/);
const instant = z.string().datetime({ offset: true });
const windowSchema = z.object({ scope: z.enum(['all_time', 'requested', 'effective']), start: instant.nullable(), end: instant.nullable() }).strict();
const policy = z.object({ policy_id: identifier, revision: identifier, sha256: z.string().regex(/^[0-9a-f]{64}$/) }).strict();
const category = z.object({ task: z.enum(['message_topics', 'concept_mentions']), category_id: identifier }).strict();
const text = z.string().refine((s) => Array.from(s).length > 0 && Array.from(s).length <= 256
  && Array.from(s).every((p) => { const n = p.codePointAt(0)!; return n < 0xd800 || n > 0xdfff; }));
// Match Python's Unicode strip policy; JavaScript trim also removes U+FEFF.
export function literalKey(s: string): string {
  const points = Array.from(s);
  const space = (p: string) => {
    const n = p.codePointAt(0)!;
    return (n >= 9 && n <= 13) || (n >= 28 && n <= 32) || (n >= 8192 && n <= 8202)
      || [133, 160, 5760, 8232, 8233, 8239, 8287, 12288].includes(n);
  };
  let first = 0; let end = points.length;
  while (first < end && space(points[first])) first += 1;
  while (end > first && space(points[end - 1])) end -= 1;
  return points.slice(first, end).join('').normalize('NFC');
}
const label = { category_id: identifier, display_label: text, label_status: z.enum(['resolved', 'id_only']), label_identity_sha256: digest };
const support = { supporting_messages: count, supporting_conversations: count, occurrences: count,
  complete_supporting_messages: count, partial_supporting_messages: count };
const extraction = z.object({ task: z.literal('concept_mentions'), task_contract: z.literal('concept_mentions.v1'),
  component_id: identifier, component_sha256: digest, taxonomy: policy, availability: z.enum(['current', 'partial', 'pending']),
  eligible_messages: count, complete_messages: count, partial_messages: count, pending_messages: count,
  failed_messages: count, unsupported_messages: count, uncertain_messages: count, scan_complete: z.boolean(),
  rows: z.array(z.object({ ...label, ...support, share_of_complete_messages: z.number().min(0).max(1).nullable() }).strict()).max(64) }).strict();
const row = z.object({ node_id: digest, type_id: identifier, ...label, ...support,
  literal_key: text.refine((s) => literalKey(s) === s) }).strict();
const evidence = z.object({ node_id: digest, category_id: identifier, assessment: z.enum(['complete', 'partial']),
  reference: questionEvidence.refine((r) => r.span != null) }).strict();
const response = z.object({ schema_version: z.literal('analytics-literals.v1'),
  account_ref: z.string().regex(/^a1:[0-9a-f]{64}$/), source_revision: count, projection_generation: count.min(1),
  projection_digest: digest, canonical_content_digest: digest, graph_digest: digest, pipeline_revision: z.string().min(1),
  pipeline_config_digest: digest, pipeline_identity_digest: digest, snapshot_sha256: digest, profile_sha256: digest,
  presentation: policy, requested_window: windowSchema, effective_window: windowSchema, direction: z.enum(['all', 'inbound', 'outbound']),
  conversation_ref: z.string().regex(/^c1:[0-9a-f]{64}$/).nullable(), category_filter: category.nullable(), node_id: digest.nullable(),
  availability: z.enum(['available', 'unavailable']), unavailable_reason: z.enum(['task_not_declared', 'not_prepared', 'capacity']).nullable(),
  normalization_sha256: digest.nullable(), normalization_policy: policy.nullable(), extraction: extraction.nullable(),
  normalization_coverage: z.object({ eligible_occurrences: count, mapped_occurrences: count, unrepresentable_occurrences: count, supporting_messages: count }).strict().nullable(),
  row_page: z.object({ offset: count, limit: count.min(1).max(100), total: count, has_more: z.boolean(), items: z.array(row).max(100) }).strict(),
  evidence_page: z.object({ offset: count, limit: count.min(1).max(100), total: count, has_more: z.boolean(), items: z.array(evidence).max(100) }).strict(),
}).strict();
export type LiteralFindings = z.infer<typeof response>;
export type LiteralRow = LiteralFindings['row_page']['items'][number];
export interface LiteralsRequest {
  baseline: FindingsBaseline; direction: FindingsDirection; conversation_ref: string | null; category_filter: FindingsCategoryFilter | null;
  row_offset: number; row_limit: number; evidence_offset: number; evidence_limit: number;
  node_id: string | null; expected_normalization_sha256: string | null;
}
export class LiteralsContractError extends Error {
  constructor() { super('These literal values could not be verified. Refresh the saved analysis.'); }
}
function micros(s: string): bigint {
  const fraction = s.match(/\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/)?.[1] ?? '';
  if (!Number.isFinite(Date.parse(s)) || fraction.slice(6).replace(/0/g, '')) throw new LiteralsContractError();
  return BigInt(Date.parse(s)) * 1000n + BigInt(fraction.padEnd(6, '0').slice(3, 6));
}
const sameTime = (a: string | null, b: string | null) => a === null || b === null ? a === b : micros(a) === micros(b);
function pageValid(p: { offset: number; limit: number; total: number; has_more: boolean; items: unknown[] }, offset: number, limit: number) {
  return p.offset === offset && p.limit === limit && p.items.length === Math.min(limit, Math.max(0, p.total - offset))
    && p.has_more === (offset + p.items.length < p.total);
}
function supportValid(r: z.infer<typeof row> | z.infer<typeof extraction>['rows'][number]) {
  return r.supporting_conversations <= r.supporting_messages && r.supporting_messages <= r.occurrences
    && BigInt(r.complete_supporting_messages) + BigInt(r.partial_supporting_messages) === BigInt(r.supporting_messages)
    && (r.label_status !== 'id_only' || r.display_label === r.category_id);
}
export function parseLiterals(value: unknown, request: LiteralsRequest): LiteralFindings {
  const parsed = response.safeParse(value); if (!parsed.success) throw new LiteralsContractError();
  const r = parsed.data; const fail = () => { throw new LiteralsContractError(); };
  if ((request.node_id !== null || request.row_offset > 0 || request.evidence_offset > 0) && request.expected_normalization_sha256 === null) fail();
  const { requested_window: window, ...identity } = request.baseline;
  for (const [key, expected] of Object.entries(identity)) if (r[key as keyof LiteralFindings] !== expected) fail();
  const start = window.start === null ? null : micros(window.start); const end = window.end === null ? null : micros(window.end);
  if (window.scope !== 'requested' || r.requested_window.scope !== window.scope || !sameTime(r.requested_window.start, window.start)
    || !sameTime(r.requested_window.end, window.end) || (start !== null && end !== null && start > end)
    || r.direction !== request.direction || r.conversation_ref !== request.conversation_ref || r.node_id !== request.node_id
    || (r.category_filter?.task ?? null) !== (request.category_filter?.task ?? null)
    || (r.category_filter?.category_id ?? null) !== (request.category_filter?.category_id ?? null)
    || !pageValid(r.row_page, request.row_offset, request.row_limit) || !pageValid(r.evidence_page, request.evidence_offset, request.evidence_limit)) fail();
  const effective = r.effective_window;
  if (effective.scope !== 'effective' || (effective.start === null) !== (effective.end === null)) fail();
  if (effective.start !== null && effective.end !== null && (micros(effective.start) > micros(effective.end)
    || (start !== null && micros(effective.start) < start) || (end !== null && micros(effective.end) > end))) fail();
  const available = r.availability === 'available';
  if (available ? r.unavailable_reason !== null || r.normalization_sha256 === null || r.normalization_policy === null || r.normalization_coverage === null || r.extraction === null
    : r.unavailable_reason === null || r.normalization_sha256 !== null || r.normalization_policy !== null || r.normalization_coverage !== null
      || r.row_page.total !== 0 || r.evidence_page.total !== 0) fail();
  if ((r.unavailable_reason === 'task_not_declared') !== (r.extraction === null)
    || (request.expected_normalization_sha256 !== null && r.normalization_sha256 !== request.expected_normalization_sha256)) fail();
  const task = r.extraction;
  if (task) {
    if ([task.complete_messages, task.partial_messages, task.pending_messages, task.failed_messages, task.unsupported_messages, task.uncertain_messages]
      .reduce((n, v) => n + BigInt(v), 0n) !== BigInt(task.eligible_messages) || new Set(task.rows.map((v) => v.category_id)).size !== task.rows.length) fail();
    for (const v of task.rows) if (!supportValid(v) || v.complete_supporting_messages > task.complete_messages || v.partial_supporting_messages > task.partial_messages
      || (task.complete_messages === 0 ? v.share_of_complete_messages !== null : v.share_of_complete_messages !== v.complete_supporting_messages / task.complete_messages)) fail();
  }
  const coverage = r.normalization_coverage;
  if (coverage && (BigInt(coverage.mapped_occurrences) + BigInt(coverage.unrepresentable_occurrences) !== BigInt(coverage.eligible_occurrences)
    || coverage.supporting_messages > (task?.eligible_messages ?? 0) || coverage.supporting_messages > coverage.eligible_occurrences)) fail();
  const nodes = new Map<string, LiteralRow>(); let previous: LiteralRow | undefined;
  for (const v of r.row_page.items) {
    const labelRow = task?.rows.find((x) => x.category_id === v.category_id);
    if (!supportValid(v) || v.supporting_conversations === 0 || nodes.has(v.node_id) || !labelRow
      || labelRow.display_label !== v.display_label || labelRow.label_status !== v.label_status || labelRow.label_identity_sha256 !== v.label_identity_sha256
      || v.complete_supporting_messages > labelRow.complete_supporting_messages || v.partial_supporting_messages > labelRow.partial_supporting_messages
      || v.supporting_messages > labelRow.supporting_messages || v.supporting_conversations > labelRow.supporting_conversations || v.occurrences > labelRow.occurrences
      || v.occurrences > (coverage?.mapped_occurrences ?? 0)) fail();
    if (previous && (previous.supporting_conversations < v.supporting_conversations
      || (previous.supporting_conversations === v.supporting_conversations && (previous.supporting_messages < v.supporting_messages
        || (previous.supporting_messages === v.supporting_messages && (previous.occurrences < v.occurrences
          || (previous.occurrences === v.occurrences && previous.node_id >= v.node_id))))))) fail();
    nodes.set(v.node_id, v); previous = v;
  }
  const seen = new Set<string>();
  let previousEvidence: LiteralFindings['evidence_page']['items'][number] | undefined;
  for (const e of r.evidence_page.items) {
    const at = micros(e.reference.sent_at); const key = JSON.stringify([e.node_id, e.category_id, evidenceKey(e.reference)]);
    if (seen.has(key) || e.reference.account_ref !== r.account_ref || e.reference.source_revision !== r.source_revision
      || (request.conversation_ref !== null && e.reference.conversation_ref !== request.conversation_ref)
      || (request.node_id !== null && e.node_id !== request.node_id)
      || !task?.rows.some((x) => x.category_id === e.category_id && (e.assessment === 'complete' ? x.complete_supporting_messages : x.partial_supporting_messages) > 0)
      || e.reference.span!.end > 131_072
      || (nodes.has(e.node_id) && nodes.get(e.node_id)!.category_id !== e.category_id)
      || (start !== null && at < start) || (end !== null && at > end)) fail();
    if (previousEvidence) {
      const left = [previousEvidence.reference.message_ref, previousEvidence.node_id, previousEvidence.category_id];
      const right = [e.reference.message_ref, e.node_id, e.category_id];
      let order = 0;
      for (let i = 0; i < left.length && order === 0; i += 1) order = left[i] < right[i] ? -1 : left[i] > right[i] ? 1 : 0;
      if (order > 0 || (order === 0 && (previousEvidence.reference.span!.start > e.reference.span!.start
        || (previousEvidence.reference.span!.start === e.reference.span!.start && previousEvidence.reference.span!.end >= e.reference.span!.end)))) fail();
    }
    seen.add(key);
    previousEvidence = e;
  }
  return r;
}
export function literalsSnapshot(r: LiteralFindings): string {
  return JSON.stringify({ ...r, node_id: undefined, row_page: { total: r.row_page.total }, evidence_page: undefined });
}
