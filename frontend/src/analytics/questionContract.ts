import { z } from 'zod';

const instant = z.string().max(64).refine((value) =>
  /T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value)));
const count = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER);
const ref = (prefix: string) => z.string().regex(new RegExp(`^${prefix}:[0-9a-f]{64}$`));
const digest = z.string().regex(/^sha256:[0-9a-f]{64}$/);
const coverage = z.enum(['complete', 'partial', 'unknown']);
const span = z.object({ start: count, end: count }).strict().refine((s) => s.end > s.start);
export const questionId = z.enum(['no_later_creator_reply.v1', 'pricing_discussions.v1']);
export type QuestionId = z.infer<typeof questionId>;
export const questionPlan = z.object({
  question: questionId, start: instant, end: instant, timezone: z.string().min(1).max(64),
  filters: z.object({ conversation_ref: ref('c1').nullable().optional(),
    language: z.literal('en').nullable().optional() }).strict().optional(),
  cutoff: instant.nullable().optional(), sort: z.literal('evidence_time_desc').optional(),
  page_size: count.min(1).max(200).default(50), cursor: z.string().min(1).max(4096).nullable().optional(),
}).strict().refine((p) => Date.parse(p.start) < Date.parse(p.end)
  && (!p.cutoff || Date.parse(p.end) <= Date.parse(p.cutoff)));
export type QuestionPlan = z.infer<typeof questionPlan>;
export const questionEvidence = z.object({
  account_ref: ref('a1'), conversation_ref: ref('c1'), message_ref: ref('m1'),
  source_revision: count, source_version_digest: digest, sent_at: instant,
  span: span.nullable().optional(),
}).strict();
export type QuestionEvidence = z.infer<typeof questionEvidence>;
const row = z.object({
  account_ref: ref('a1'), conversation_ref: ref('c1'), latest_evidence_at: instant,
  reason: z.enum(['no_later_creator_reply', 'pricing_discussion']), coverage,
  evidence: z.array(questionEvidence).min(1).max(32), evidence_truncated: z.boolean(),
}).strict();
const snapshot = z.object({
  account_ref: ref('a1'), source_revision: count, projection_generation: count.min(1),
  generation_id: z.string().min(1).max(128), canonical_content_digest: digest,
  projection_digest: digest, derived_at: instant, source_message_count: count,
  retention_due_at: instant.nullable(),
}).strict();
const resultSchema = z.object({
  schema_version: z.literal('analytics-question-result.v1'), availability: z.literal('available'),
  counting_unit: z.literal('conversations'),
  question: z.object({
    plan: questionPlan, account_ref: ref('a1'), cutoff: instant,
    retention_cutoff_exclusive: instant, selection_clipped_by_retention: z.boolean(),
    definition_digest: digest, snapshot,
  }).strict(),
  page: z.object({
    rows: z.array(row).max(200),
    coverage: z.object({
      history: coverage, ordering: z.enum(['source', 'inferred', 'unknown']),
      evaluated_start: instant.nullable().optional(), evaluated_end: instant.nullable().optional(),
      eligible_classification_count: count.nullable().optional(),
      analyzed_classification_count: count.nullable().optional(),
      supported_languages: z.array(z.literal('en')).max(1),
    }).strict(),
    evaluated_conversation_count: count, undetermined_conversation_count: count,
    total_matching_conversations: count.nullable().optional(), has_more: z.boolean(), truncated: z.boolean(),
  }).strict(),
  checked_at: instant, next_cursor: z.string().min(1).max(4096).nullable(),
}).strict();
export type QuestionResult = z.infer<typeof resultSchema>;
export type QuestionRow = QuestionResult['page']['rows'][number];
export const evidenceResponse = z.object({
  reference: questionEvidence,
  location: z.object({ conversation_id: z.string().min(1).max(512),
    message_id: z.string().min(1).max(512) }).strict(),
  text: z.string().max(131072), direction: z.enum(['inbound', 'outbound']),
  checked_at: instant, browser_span: span.nullable(),
}).strict();
export type SourceEvidence = z.infer<typeof evidenceResponse>;
export const questionCatalog = z.object({ questions: z.array(z.object({
  question: questionId, enabled: z.boolean(), limitations: z.array(z.string().max(512)).max(8).optional(),
  reason: z.string().max(128).optional(),
}).strict()).max(2) }).strict().refine((value) =>
  new Set(value.questions.map((q) => q.question)).size === value.questions.length);
export type QuestionCatalog = z.infer<typeof questionCatalog>;
export class QuestionContractError extends Error {
  constructor() { super('The response could not be verified. Run the question again.'); }
}
export function evidenceKey(value: QuestionEvidence): string {
  return JSON.stringify(questionEvidence.parse(value));
}
export function snapshotKey(result: QuestionResult): string {
  return JSON.stringify([result.question.account_ref, result.question.definition_digest,
    result.question.snapshot, result.question.cutoff]);
}

export function parseQuestionResult(value: unknown, request: QuestionPlan): QuestionResult {
  const parsed = resultSchema.safeParse(value);
  if (!parsed.success) throw new QuestionContractError();
  const result = parsed.data;
  const { question: q, page } = result;
  const sameInstant = (a: string, b: string) => Date.parse(a) === Date.parse(b);
  if (q.plan.question !== request.question || !sameInstant(q.plan.start, request.start)
    || !sameInstant(q.plan.end, request.end) || q.plan.timezone !== request.timezone
    || q.plan.page_size !== request.page_size || q.account_ref !== q.snapshot.account_ref
    || (request.cutoff && !sameInstant(q.cutoff, request.cutoff))
    || !q.plan.cutoff || !sameInstant(q.cutoff, q.plan.cutoff)
    || (q.plan.filters?.conversation_ref ?? null) !== (request.filters?.conversation_ref ?? null)
    || (q.plan.filters?.language ?? null) !== (request.filters?.language ?? null)
    || page.rows.length > request.page_size || page.has_more !== (result.next_cursor !== null)
    || (page.has_more && page.rows.length === 0)
    || (page.truncated && (page.has_more || page.total_matching_conversations != null))
    || page.evaluated_conversation_count < page.rows.length + page.undetermined_conversation_count
    || (page.total_matching_conversations != null && page.total_matching_conversations < page.rows.length)
    || new Set(page.rows.map((r) => r.conversation_ref)).size !== page.rows.length
    || page.rows.reduce((n, r) => n + r.evidence.length, 0) > 512) throw new QuestionContractError();
  let previous: QuestionRow | undefined;
  for (const r of page.rows) {
    if (r.account_ref !== q.account_ref
      || r.reason !== (request.question === 'pricing_discussions.v1' ? 'pricing_discussion' : 'no_later_creator_reply')
      || (request.filters?.conversation_ref && r.conversation_ref !== request.filters.conversation_ref)
      || new Set(r.evidence.map((e) => e.message_ref)).size !== r.evidence.length
      || !r.evidence.some((e) => sameInstant(e.sent_at, r.latest_evidence_at))) throw new QuestionContractError();
    if (previous && (Date.parse(previous.latest_evidence_at) < Date.parse(r.latest_evidence_at)
      || (previous.latest_evidence_at === r.latest_evidence_at
        && previous.conversation_ref >= r.conversation_ref))) throw new QuestionContractError();
    previous = r;
    for (const e of r.evidence) {
      const at = Date.parse(e.sent_at);
      if (e.account_ref !== q.account_ref || e.conversation_ref !== r.conversation_ref
        || e.source_revision !== q.snapshot.source_revision || at > Date.parse(r.latest_evidence_at)
        || at <= Date.parse(q.retention_cutoff_exclusive) || at > Date.parse(q.cutoff)
        || (request.question === 'pricing_discussions.v1'
          && (at < Date.parse(request.start) || at >= Date.parse(request.end)))) throw new QuestionContractError();
    }
  }
  return result;
}

export function parseSourceEvidence(value: unknown, reference: QuestionEvidence): SourceEvidence {
  const parsed = evidenceResponse.safeParse(value);
  if (!parsed.success || evidenceKey(parsed.data.reference) !== evidenceKey(reference)) throw new QuestionContractError();
  const result = parsed.data;
  if (reference.span) {
    const points = Array.from(result.text);
    if (reference.span.end > points.length || result.browser_span?.start !== points.slice(0, reference.span.start).join('').length
      || result.browser_span?.end !== points.slice(0, reference.span.end).join('').length) throw new QuestionContractError();
  } else if (result.browser_span !== null) throw new QuestionContractError();
  return result;
}

/** Compare a response with the active account; this does not grant read authority. */
export async function expectedAccountRef(accountId: string): Promise<string> {
  const encoder = new TextEncoder();
  const prefix = encoder.encode('ofca:analytics-ref:v1\0');
  const domain = encoder.encode('account');
  const identity = encoder.encode(accountId);
  const bytes = new Uint8Array(prefix.length + 2 + domain.length + 8 + identity.length);
  bytes.set(prefix);
  const view = new DataView(bytes.buffer);
  view.setUint16(prefix.length, domain.length);
  bytes.set(domain, prefix.length + 2);
  const offset = prefix.length + 2 + domain.length;
  view.setBigUint64(offset, BigInt(identity.length));
  bytes.set(identity, offset + 8);
  const hash = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  return `a1:${Array.from(hash, (b) => b.toString(16).padStart(2, '0')).join('')}`;
}
