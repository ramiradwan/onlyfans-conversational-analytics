import { z } from 'zod';

import type { FindingsBaseline } from './analyticsReadModel';
import { readBoundedFindingsJson } from './findingsClient';
import { instantMicros } from './findingsContract';
import { literalKey } from './literalsContract';
import { evidenceKey, questionEvidence } from './questionContract';
import { FINDINGS } from '../config/endpoints';
import { createQuestionApi } from '../services/questionApi';

const count = z.number().int().min(0).max(Number.MAX_SAFE_INTEGER);
const digest = z.string().regex(/^sha256:[0-9a-f]{64}$/);
const instant = z.string().datetime({ offset: true });
const windowRange = z.object({ scope: z.enum(['requested', 'effective']), start: instant.nullable(), end: instant.nullable() }).strict();
const coverage = z.object({
  eligible_messages: count, complete_messages: count, partial_messages: count, pending_messages: count,
  failed_messages: count, uncertain_messages: count, unsupported_messages: count, scan_complete: z.boolean(),
  unrepresentable_occurrences: count,
}).strict();
const row = z.object({
  phrase_id: digest, literal_key: z.string().refine((s) => s.isWellFormed() && s.length > 0 && Array.from(s).length <= 256 && literalKey(s) === s),
  supporting_senders: count.min(2), supporting_messages: count.min(2), occurrences: count.min(2),
}).strict().refine((r) => r.supporting_senders <= r.supporting_messages && r.supporting_messages <= r.occurrences);
const evidence = z.object({
  phrase_id: digest, assessment: z.enum(['complete', 'partial']), reference: questionEvidence.refine((r) => r.span != null),
}).strict();
const paging = { offset: count, limit: count.min(1).max(100), total: count, has_more: z.boolean() };
const reportSchema = z.object({
  schema_version: z.literal('analytics-recurring-mentions.v1'), account_ref: z.string().regex(/^a1:[0-9a-f]{64}$/),
  source_revision: count, projection_generation: count.min(1), projection_digest: digest, snapshot_sha256: digest,
  normalization_sha256: digest.nullable(), phrase_id: digest.nullable(), requested_window: windowRange, effective_window: windowRange,
  availability: z.enum(['available', 'unavailable']), unavailable_reason: z.enum(['task_not_declared', 'not_prepared', 'capacity']).nullable(), coverage: coverage.nullable(),
  row_page: z.object({ ...paging, items: z.array(row).max(100) }).strict(),
  evidence_page: z.object({ ...paging, items: z.array(evidence).max(100) }).strict(),
}).strict();
export const paidInsightsCapability = z.object({
  schema_version: z.literal('paid-insights-pilot.v1'), profile_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  can_acquire_preview: z.boolean(), can_preview: z.boolean(), can_purchase: z.boolean(), can_process: z.boolean(),
}).strict();
export type PaidInsightsCapability = z.infer<typeof paidInsightsCapability>;
export type RecurringReport = z.infer<typeof reportSchema>;
export type RecurringRow = z.infer<typeof row>;
export type RecurringReportRequest = {
  baseline: Pick<FindingsBaseline, 'account_ref' | 'projection_generation' | 'projection_digest' | 'source_revision'>;
  phraseId: string | null; normalization: string | null;
  rowOffset: number; evidenceOffset: number; limit: number;
};

function check(condition: unknown): asserts condition {
  if (!condition) throw new Error('The saved recurring mentions could not be verified.');
}
// Python ranks normalized spellings by Unicode scalar, including characters outside the BMP.
function phraseBefore(left: string, right: string) {
  const a = Array.from(left), b = Array.from(right);
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    if (a[i] !== b[i]) return a[i].codePointAt(0)! < b[i].codePointAt(0)!;
  }
  return a.length < b.length;
}
export function parseRecurringReport(value: unknown, request: RecurringReportRequest): RecurringReport {
  const report = reportSchema.parse(value);
  const { baseline } = request;
  check(report.account_ref === baseline.account_ref && report.projection_generation === baseline.projection_generation
    && report.projection_digest === baseline.projection_digest && report.source_revision === baseline.source_revision);
  check(report.phrase_id === request.phraseId && (request.normalization === null || report.normalization_sha256 === request.normalization));
  const { requested_window: period, effective_window: assessed } = report;
  check(period.scope === 'requested' && period.start !== null && period.end !== null && assessed.scope === 'effective');
  const lower = instantMicros(period.start), cutoff = instantMicros(period.end);
  check(cutoff - lower === 90n * 86400_000_000n);
  check((assessed.start === null) === (assessed.end === null));
  if (assessed.start !== null && assessed.end !== null) {
    const first = instantMicros(assessed.start), last = instantMicros(assessed.end);
    check(first > lower && last <= cutoff && first <= last);
  }
  check((report.availability === 'available') === (report.normalization_sha256 !== null));
  check((report.availability === 'unavailable') === (report.unavailable_reason !== null));
  check((report.availability === 'available') === (report.coverage !== null));
  if (report.coverage) {
    const c = report.coverage;
    check([c.complete_messages, c.partial_messages, c.pending_messages, c.failed_messages, c.uncertain_messages, c.unsupported_messages]
      .reduce((sum, n) => sum + BigInt(n), 0n) === BigInt(c.eligible_messages));
  }
  const pages = [report.row_page, report.evidence_page];
  for (const [index, page] of pages.entries()) {
    check(page.offset === (index === 0 ? request.rowOffset : request.evidenceOffset) && page.limit === request.limit);
    check(page.offset <= page.total && page.items.length === Math.min(page.limit, page.total - page.offset)
      && page.has_more === (page.offset + page.items.length < page.total));
    if (report.availability === 'unavailable') check(page.total === 0);
  }
  check(new Set(report.row_page.items.map((item) => item.phrase_id)).size === report.row_page.items.length);
  check(new Set(report.row_page.items.map((item) => item.literal_key)).size === report.row_page.items.length);
  for (const [index, item] of report.row_page.items.entries()) {
    check(report.coverage && item.supporting_messages <= report.coverage.complete_messages + report.coverage.partial_messages);
    const previous = report.row_page.items[index - 1];
    if (previous) check(previous.supporting_senders > item.supporting_senders || previous.supporting_senders === item.supporting_senders
      && (previous.supporting_messages > item.supporting_messages || previous.supporting_messages === item.supporting_messages && phraseBefore(previous.literal_key, item.literal_key)));
  }
  const seen = new Set<string>();
  for (const item of report.evidence_page.items) {
    const ref = item.reference, key = JSON.stringify([item.phrase_id, evidenceKey(ref)]);
    check(!seen.has(key) && ref.account_ref === baseline.account_ref && ref.source_revision === baseline.source_revision
      && (request.phraseId === null || item.phrase_id === request.phraseId) && ref.span != null && ref.span.end <= 131_072);
    const sentAt = instantMicros(ref.sent_at);
    check(assessed.start !== null && assessed.end !== null &&
      sentAt >= instantMicros(assessed.start) && sentAt <= instantMicros(assessed.end));
    seen.add(key);
  }
  return report;
}

export function createRecurringMentionsClient(fetcher = globalThis.fetch.bind(globalThis)) {
  const sources = createQuestionApi({ fetcher });
  const get = (url: string, signal: AbortSignal) => fetcher(url, {
    method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
    headers: { Accept: 'application/json' }, signal,
  });
  return {
    evidence: sources.evidence, context: sources.context,
    async capability(signal: AbortSignal): Promise<PaidInsightsCapability | null> {
      const response = await get('/api/v1/nlp/paid-insights', signal);
      if (response.status === 404) return null;
      check(response.ok);
      return paidInsightsCapability.parse(await readBoundedFindingsJson(response));
    },
    async report(request: RecurringReportRequest, signal: AbortSignal): Promise<RecurringReport> {
      const params = new URLSearchParams({
        expected_generation: String(request.baseline.projection_generation), expected_projection_digest: request.baseline.projection_digest,
        row_offset: String(request.rowOffset), row_limit: String(request.limit),
        evidence_offset: String(request.evidenceOffset), evidence_limit: String(request.limit),
      });
      if (request.phraseId !== null) params.set('phrase_id', request.phraseId);
      if (request.normalization !== null) params.set('expected_normalization_sha256', request.normalization);
      const response = await get(`${FINDINGS}/recurring-mentions?${params}`, signal);
      check(response.ok);
      return parseRecurringReport(await readBoundedFindingsJson(response), request);
    },
  };
}
export type RecurringMentionsClient = ReturnType<typeof createRecurringMentionsClient>;

/** Read all pages of the same published report before offering its local download. */
export async function exportRecurringReport(client: RecurringMentionsClient, baseline: RecurringReportRequest['baseline'],
  original: RecurringReport, signal: AbortSignal) {
  check(original.availability === 'available' && original.normalization_sha256 !== null);
  const phrases: RecurringRow[] = [], references: RecurringReport['evidence_page']['items'] = [];
  let rowOffset = 0, evidenceOffset = 0, rowTotal: number | undefined, evidenceTotal: number | undefined;
  while (true) {
    signal.throwIfAborted();
    const page = await client.report({ baseline, phraseId: null, normalization: original.normalization_sha256,
      rowOffset, evidenceOffset, limit: 100 }, signal);
    check(page.snapshot_sha256 === original.snapshot_sha256 && page.availability === 'available');
    check((rowTotal === undefined || rowTotal === page.row_page.total) && (evidenceTotal === undefined || evidenceTotal === page.evidence_page.total));
    rowTotal = page.row_page.total; evidenceTotal = page.evidence_page.total;
    phrases.push(...page.row_page.items); references.push(...page.evidence_page.items);
    rowOffset = phrases.length; evidenceOffset = references.length;
    if (!page.row_page.has_more && !page.evidence_page.has_more) break;
  }
  signal.throwIfAborted();
  check(new Set(phrases.map((item) => item.phrase_id)).size === phrases.length
    && new Set(references.map((item) => JSON.stringify([item.phrase_id, evidenceKey(item.reference)]))).size === references.length);
  const counts = new Map(phrases.map((item) => [item.phrase_id, { occurrences: 0, messages: new Set<string>() }]));
  for (const { phrase_id, reference } of references) {
    const counted = counts.get(phrase_id); check(counted);
    counted.occurrences += 1; counted.messages.add(reference.message_ref);
  }
  for (const item of phrases) {
    const counted = counts.get(item.phrase_id)!;
    check(counted.occurrences === item.occurrences && counted.messages.size === item.supporting_messages);
  }
  return { schema_version: 'recurring-mentions-export.v1', period: original.requested_window, coverage: original.coverage,
    projection_digest: original.projection_digest, snapshot_sha256: original.snapshot_sha256,
    normalization_sha256: original.normalization_sha256, phrases, sources: references };
}
