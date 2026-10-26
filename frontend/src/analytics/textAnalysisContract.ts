import { z } from 'zod';

import { navigationCanonicalJson } from './findingsContract';
import { questionEvidence } from './questionContract';

export const textAnalysisUuid = z.string().regex(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
const digest = z.string().regex(/^[0-9a-f]{64}$/);
const identifier = z.string().min(1).max(160).regex(/^[A-Za-z0-9_.:-]+$/);
const name = z.string().refine((value) => value.isWellFormed() && Array.from(value).length >= 1 && Array.from(value).length <= 512);
const count = z.number().int().min(0).max(Number.MAX_SAFE_INTEGER);
const instant = z.string().max(64).datetime({ offset: true });
const taskName = z.enum(['message_topics', 'concept_mentions']);
export const textAnalysisPolicy = z.object({ policy_id: identifier, revision: identifier, sha256: digest }).strict();
const scorePolicy = z.object({ kind: z.enum(['probability', 'logit', 'margin', 'none']), semantics_id: identifier,
  calibration: z.enum(['not_calibrated', 'calibrated']), calibration_sha256: digest.nullable() }).strict().refine((value) =>
  (value.calibration === 'calibrated') === (value.calibration_sha256 !== null)
  && (value.kind !== 'none' || value.calibration === 'not_calibrated'));
const taskDescriptor = z.object({ task: taskName, task_contract: z.enum(['message_topics.v1', 'concept_mentions.v1']),
  taxonomy: textAnalysisPolicy, enabled_ids: z.array(identifier).max(64), score_policy: scorePolicy }).strict().refine((value) =>
  value.task_contract === `${value.task}.v1` && new Set(value.enabled_ids).size === value.enabled_ids.length);
const descriptors = z.array(taskDescriptor).min(1).max(2).refine((values) =>
  JSON.stringify(values.map((value) => value.task)) === JSON.stringify(['message_topics', 'concept_mentions'].filter((task) => values.some((value) => value.task === task))));
const languages = z.tuple([z.literal('en')]);

export const enrollmentCommand = z.object({ schema_version: z.literal('text-analysis-enrollment-command.v1').default('text-analysis-enrollment-command.v1'),
  command_key: textAnalysisUuid, expected_enrollment_id: textAnalysisUuid.nullable(), expected_mode_version: count,
  action: z.enum(['enable', 'replace', 'pause', 'resume', 'retire']), expected_profile_sha256: digest.nullable().default(null) }).strict().refine((value) =>
  (['enable', 'replace'].includes(value.action)) === (value.expected_profile_sha256 !== null)
  && (value.action === 'enable') === (value.expected_enrollment_id === null));
export type EnrollmentCommand = z.infer<typeof enrollmentCommand>;
const enrollmentState = z.enum(['enabled', 'paused', 'retired']);
const currentEnrollment = z.object({ installation_id: name, creator_account_id: name, principal_id: name,
  mode_version: count, enrollment_id: textAnalysisUuid.nullable(), state: enrollmentState, capsule_available: z.boolean() }).strict();
const commandReceipt = z.object({ command_key: textAnalysisUuid, meaning_sha256: digest, mode_version: count,
  enrollment_id: textAnalysisUuid.nullable(), state: enrollmentState }).strict();
const commandResult = z.object({ receipt: commandReceipt, current: currentEnrollment }).strict();
const enrollmentProfile = z.object({ profile_sha256: digest, profile_id: identifier, revision: identifier,
  languages, tasks: descriptors, presentation: textAnalysisPolicy }).strict();
const enrollmentView = z.object({ current: currentEnrollment, profile: enrollmentProfile.nullable() }).strict().refine((value) =>
  value.current.capsule_available === (value.profile !== null));
export type EnrollmentView = z.infer<typeof enrollmentView>;
export type EnrollmentCommandResult = z.infer<typeof commandResult>;

export const previewPrepare = z.object({ schema_version: z.literal('text-analysis-preview-prepare.v1').default('text-analysis-preview-prepare.v1'),
  external_key: textAnalysisUuid, expected_profile_sha256: digest }).strict();
export type PreviewPrepareRequest = z.infer<typeof previewPrepare>;
export const previewConfirmation = z.object({ schema_version: z.literal('text-analysis-confirmation.v1'), external_key: textAnalysisUuid,
  job_id: textAnalysisUuid, contribution_id: textAnalysisUuid, spec_sha256: digest, context_sha256: digest, scope_sha256: digest,
  parameter_sha256: digest, input_inventory_sha256: digest, reference_id: name, compact_jws_sha256: digest,
  preview_selection_policy: textAnalysisPolicy }).strict();
export type PreviewConfirmation = z.infer<typeof previewConfirmation>;
const previewScope = z.object({ schema_version: z.literal('text-analysis-preview-scope.v1'), cutoff: instant,
  lower_exclusive: instant, directions: z.literal('both'), message_cap: z.literal(32), source_count: count.max(32),
  earliest: instant.nullable(), latest: instant.nullable(), inventory_sha256: digest }).strict().refine((value) =>
  Date.parse(value.cutoff) - Date.parse(value.lower_exclusive) === 90 * 86400_000
  && (value.source_count === 0 ? value.earliest === null && value.latest === null
    : value.earliest !== null && value.latest !== null && Date.parse(value.lower_exclusive) < Date.parse(value.earliest)
      && Date.parse(value.earliest) <= Date.parse(value.latest) && Date.parse(value.latest) <= Date.parse(value.cutoff)));
const disclosureFields = { profile_sha256: digest, tasks: descriptors, languages, presentation: textAnalysisPolicy,
  selection_policy: textAnalysisPolicy, output_profile: z.literal('urn:bridge-clean:text-analysis-preview-output-policy:v1'), output_version: z.literal(1) };
const previewDisclosure = z.object(disclosureFields).strict();
const savedDisclosure = z.object({ ...disclosureFields, languages }).strict();
const previewReceipt = z.object({ job_id: textAnalysisUuid, contribution_id: textAnalysisUuid,
  state: z.enum(['prepared', 'pending', 'running', 'committed', 'failed', 'result_deleted']),
  terminal_reason: z.enum(['empty', 'incomplete', 'overflow', 'source_retired']).nullable() }).strict();
const previewOutcome = z.object({ request: previewPrepare, receipt: previewReceipt, scope: previewScope.nullable(),
  confirmation: previewConfirmation.nullable(), detailed_scope_available: z.boolean(), disclosure: previewDisclosure.nullable() }).strict();
export type PreviewOutcome = z.infer<typeof previewOutcome>;

const span = z.object({ start: count, end: count.min(1) }).strict().refine((value) => value.start < value.end);
const coverage = z.object({ source_codepoints: count.max(131072), examined: z.array(span).max(256), excluded: z.array(span).max(256),
  limits_reached: z.array(z.enum(['tokens', 'windows', 'candidates', 'output'])).max(4) }).strict().refine((value) => {
  let cursor = 0;
  for (const interval of [...value.examined, ...value.excluded].sort((a, b) => a.start - b.start)) {
    if (interval.start !== cursor || interval.end > value.source_codepoints) return false;
    cursor = interval.end;
  }
  return cursor === value.source_codepoints && new Set(value.limits_reached).size === value.limits_reached.length;
});
const examination = z.object({ ordinal: count.max(4095), task: taskName,
  assessment: z.enum(['assessed', 'partial', 'uncertain', 'unsupported', 'failed', 'unprocessed']),
  reason: z.enum(['none', 'below_threshold', 'unsupported_input', 'dependency_unavailable', 'cancelled', 'deadline', 'worker_failure', 'limit_reached']),
  coverage: coverage.nullable() }).strict().refine((value) => {
  const complete = value.coverage !== null && value.coverage.excluded.length === 0 && value.coverage.limits_reached.length === 0;
  return (value.coverage !== null || value.assessment === 'unprocessed')
    && (value.assessment !== 'assessed' || complete && value.reason === 'none')
    && (value.assessment !== 'partial' || value.coverage !== null && !complete);
});
const taskCoverage = z.object({ task: taskName, eligible: count.max(4096), assessed: count.max(65536), partial: count.max(65536),
  uncertain: count.max(65536), unsupported: count.max(65536), failed: count.max(65536), unprocessed: count.max(65536) }).strict();
const label = z.object({ presentation: textAnalysisPolicy, task_contract: z.enum(['message_topics.v1', 'concept_mentions.v1']),
  taxonomy: textAnalysisPolicy, category_id: identifier, display_label: z.string().refine((value) => value.isWellFormed() && Array.from(value).length >= 1
    && Array.from(value).length <= 256 && Array.from(value).every((character) => {
      const code = character.charCodeAt(0); return code >= 32 && (code < 127 || code >= 160);
    })),
  label_status: z.enum(['resolved', 'id_only']), label_identity_sha256: digest }).strict().refine((value) =>
  value.label_status !== 'id_only' || value.display_label === value.category_id);
const evidence = z.object({ ordinal: count.max(4095), source: z.object({ account_scope: identifier, source_ref: identifier,
  source_version: digest, speaker_ref: identifier }).strict(), reference: questionEvidence, assessment: z.enum(['complete', 'partial']) }).strict();
const summary = z.object({ kind: z.enum(['topic_category', 'mention_category']), label, evidence: z.array(evidence).min(1).max(65536),
  supporting_conversations: count.max(65536), supporting_messages: count.max(65536), occurrences: count.max(65536),
  complete_supporting_messages: count.max(65536), partial_supporting_messages: count.max(65536),
  share_of_complete_messages: z.number().finite().min(0).max(1).nullable(), ranking_scope: z.enum(['frozen_scope', 'assessed_portion']),
  additional_categories: count.max(63).nullable() }).strict();
const contribution = z.object({ schema_version: z.literal('text-analysis-preview.v1'), job_id: textAnalysisUuid, contribution_id: textAnalysisUuid,
  spec_sha256: digest, input_inventory_sha256: digest, profile_sha256: digest, selection_policy: textAnalysisPolicy,
  source_count: count.max(4096), examination: z.array(examination).max(8192), tasks: z.array(taskCoverage).min(1).max(2),
  summaries: z.array(summary).min(1).max(2) }).strict();
const savedPreview = z.object({ schema_version: z.literal('text-analysis-preview-saved.v1'), scope: previewScope,
  disclosure: savedDisclosure, output: contribution }).strict();
export type SavedPreview = z.infer<typeof savedPreview>;
export type PreviewSummary = SavedPreview['output']['summaries'][number];
const savedPage = z.object({ items: z.array(z.object({ contribution_id: textAnalysisUuid }).strict()).max(64), next_after: textAnalysisUuid.nullable() }).strict();
export type SavedPreviewPage = z.infer<typeof savedPage>;

const confirmBody = z.object({ external_key: textAnalysisUuid, confirmation: previewConfirmation }).strict().refine((value) =>
  value.external_key === value.confirmation.external_key);
const intentVariants = [
  z.object({ schema_version: z.literal('text-analysis-client-intent.v1'), kind: z.literal('enrollment_command'), key: textAnalysisUuid, body: enrollmentCommand }).strict(),
  z.object({ schema_version: z.literal('text-analysis-client-intent.v1'), kind: z.literal('preview_prepare'), key: textAnalysisUuid, body: previewPrepare }).strict(),
  z.object({ schema_version: z.literal('text-analysis-client-intent.v1'), kind: z.literal('preview_confirm'), key: textAnalysisUuid, body: confirmBody }).strict(),
] as const;
export const clientIntent = z.discriminatedUnion('kind', intentVariants).refine((value) =>
  value.key === (value.kind === 'enrollment_command' ? value.body.command_key : value.body.external_key));
export type ClientIntent = z.infer<typeof clientIntent>;
export type ClientIntentKind = ClientIntent['kind'];
const recordFields = { schema_version: z.literal('text-analysis-client-intent-record.v1'), body_sha256: digest, retained_at: instant };
const intentRecord = z.discriminatedUnion('kind', [intentVariants[0].extend(recordFields), intentVariants[1].extend(recordFields), intentVariants[2].extend(recordFields)]);
export type ClientIntentRecord = z.infer<typeof intentRecord>;
const cursor = z.object({ kind: z.enum(['enrollment_command', 'preview_prepare', 'preview_confirm']), key: textAnalysisUuid }).strict();
export type ClientIntentCursor = z.infer<typeof cursor>;
export type ClientIntentLocator = ClientIntentCursor;
const intentPage = z.object({ items: z.array(intentRecord).max(64), next_after: cursor.nullable() }).strict();
export type ClientIntentPage = z.infer<typeof intentPage>;

export class TextAnalysisContractError extends Error {
  constructor() { super('The text analysis response could not be verified.'); }
}
function checked<T>(schema: z.ZodType<T>, value: unknown): T {
  const result = schema.safeParse(value);
  if (!result.success) throw new TextAnalysisContractError();
  return result.data;
}
function need(condition: unknown): asserts condition { if (!condition) throw new TextAnalysisContractError(); }
export function textAnalysisCanonicalJson(value: unknown) { return navigationCanonicalJson(value); }
export async function textAnalysisDigest(value: unknown) {
  const bytes = new TextEncoder().encode(textAnalysisCanonicalJson(value));
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), (byte) => byte.toString(16).padStart(2, '0')).join('');
}
export function parseEnrollmentView(value: unknown, account?: string) {
  const result = checked(enrollmentView, value);
  need(account === undefined || result.current.creator_account_id === account);
  return result;
}
export function parseEnrollmentCommandResult(value: unknown, key: string) {
  const result = checked(commandResult, value); need(result.receipt.command_key === key); return result;
}
export function parsePreviewOutcome(value: unknown, key: string, request?: PreviewPrepareRequest) {
  const result = checked(previewOutcome, value);
  need(result.request.external_key === key && (!request || textAnalysisCanonicalJson(request) === textAnalysisCanonicalJson(result.request)));
  need(result.detailed_scope_available === (result.scope !== null && result.disclosure !== null));
  need(result.receipt.state !== 'result_deleted' || result.scope === null && result.disclosure === null && result.confirmation === null);
  if (result.disclosure) need(result.disclosure.profile_sha256 === result.request.expected_profile_sha256);
  if (result.confirmation) {
    const item = result.confirmation;
    need(item.external_key === key && item.job_id === result.receipt.job_id && item.contribution_id === result.receipt.contribution_id
      && item.input_inventory_sha256 === result.scope?.inventory_sha256
      && textAnalysisCanonicalJson(item.preview_selection_policy) === textAnalysisCanonicalJson(result.disclosure?.selection_policy));
  }
  return result;
}
export async function verifyPreviewOutcome(value: unknown, key: string, request?: PreviewPrepareRequest) {
  const result = parsePreviewOutcome(value, key, request);
  if (result.confirmation) need(result.scope !== null && result.confirmation.scope_sha256 === await textAnalysisDigest(result.scope));
  return result;
}
export async function parseClientIntentRecord(value: unknown, expected?: Pick<ClientIntent, 'kind' | 'key'>) {
  const result = checked(intentRecord, value);
  checked(clientIntent, { schema_version: 'text-analysis-client-intent.v1', kind: result.kind, key: result.key, body: result.body });
  need(!expected || result.kind === expected.kind && result.key === expected.key);
  need(new TextEncoder().encode(textAnalysisCanonicalJson(result.body)).length <= 8192 && result.body_sha256 === await textAnalysisDigest(result.body));
  return result;
}
const cursorKey = (value: ClientIntentCursor) => `${value.kind}:${value.key}`;
export async function parseClientIntentPage(value: unknown, after?: ClientIntentCursor) {
  const result = checked(intentPage, value); let previous = after ? cursorKey(after) : '';
  for (const item of result.items) { await parseClientIntentRecord(item); need(cursorKey(item) > previous); previous = cursorKey(item); }
  need(result.next_after === null || result.items.length > 0 && cursorKey(result.next_after) === previous);
  return result;
}
export function parseSavedPreviewPage(value: unknown, after?: string) {
  const result = checked(savedPage, value); let previous = after ?? '';
  for (const item of result.items) { need(item.contribution_id > previous); previous = item.contribution_id; }
  need(result.next_after === null || result.items.length > 0 && result.next_after === previous);
  return result;
}
export async function parseSavedPreview(value: unknown, id: string) {
  const result = checked(savedPreview, value); const { output, scope, disclosure } = result;
  need(new TextEncoder().encode(textAnalysisCanonicalJson(output)).length <= 1_048_576);
  need(output.contribution_id === id && output.source_count === scope.source_count && output.input_inventory_sha256 === scope.inventory_sha256
    && output.profile_sha256 === disclosure.profile_sha256 && textAnalysisCanonicalJson(output.selection_policy) === textAnalysisCanonicalJson(disclosure.selection_policy));
  const tasks = (['message_topics', 'concept_mentions'] as const).filter((task) => disclosure.tasks.some((item) => item.task === task));
  need(textAnalysisCanonicalJson(output.tasks.map((item) => item.task)) === textAnalysisCanonicalJson(tasks));
  need(output.examination.length === output.source_count * tasks.length);
  const exams = new Map<string, SavedPreview['output']['examination'][number]>();
  for (const [index, exam] of output.examination.entries()) {
    need(exam.ordinal === Math.floor(index / tasks.length) && exam.task === tasks[index % tasks.length]);
    exams.set(`${exam.ordinal}:${exam.task}`, exam);
  }
  for (const item of output.tasks) {
    need(item.eligible === output.source_count);
    for (const assessment of ['assessed', 'partial', 'uncertain', 'unsupported', 'failed', 'unprocessed'] as const) {
      need(item[assessment] === output.examination.filter((exam) => exam.task === item.task && exam.assessment === assessment).length);
    }
  }
  let previousKind = -1;
  const sources = new Map<number, string>(), ordinals = new Map<string, number>();
  for (const item of output.summaries) {
    const kind = ['topic_category', 'mention_category'].indexOf(item.kind); need(kind > previousKind); previousKind = kind;
    const task = kind === 0 ? 'message_topics' : 'concept_mentions';
    const descriptor = disclosure.tasks.find((entry) => entry.task === task);
    const totals = output.tasks.find((entry) => entry.task === task); need(descriptor && totals);
    need(item.label.task_contract === `${task}.v1` && descriptor.enabled_ids.includes(item.label.category_id)
      && textAnalysisCanonicalJson(item.label.taxonomy) === textAnalysisCanonicalJson(descriptor.taxonomy)
      && textAnalysisCanonicalJson(item.label.presentation) === textAnalysisCanonicalJson(disclosure.presentation));
    const { label_identity_sha256: labelDigest, ...labelBody } = item.label; need(labelDigest === await textAnalysisDigest(labelBody));
    const resolved = totals.assessed === totals.eligible;
    need(item.ranking_scope === (resolved ? 'frozen_scope' : 'assessed_portion') && (item.additional_categories !== null) === resolved
      && (item.additional_categories === null || item.additional_categories < descriptor.enabled_ids.length));
    need(item.share_of_complete_messages === (totals.assessed ? item.complete_supporting_messages / totals.assessed : null));
    const messages = new Set<number>(), conversations = new Set<string>(), complete = new Set<number>(), partial = new Set<number>(), occurrences = new Set<string>();
    let previousEvidence: [string, string, number, number] | null = null;
    for (const entry of item.evidence) {
      const ref = entry.reference, interval = ref.span, exam = exams.get(`${entry.ordinal}:${task}`);
      need(exam && ['assessed', 'partial'].includes(exam.assessment) && entry.assessment === (exam.assessment === 'assessed' ? 'complete' : 'partial'));
      need(ref.account_ref === entry.source.account_scope && ref.message_ref === entry.source.source_ref && ref.source_version_digest === `sha256:${entry.source.source_version}`
        && Date.parse(ref.sent_at) > Date.parse(scope.lower_exclusive) && Date.parse(ref.sent_at) <= Date.parse(scope.cutoff)
        && scope.earliest !== null && scope.latest !== null && Date.parse(ref.sent_at) >= Date.parse(scope.earliest) && Date.parse(ref.sent_at) <= Date.parse(scope.latest));
      const identity = textAnalysisCanonicalJson({ source: entry.source, reference: { ...ref, span: null } });
      need(!sources.has(entry.ordinal) || sources.get(entry.ordinal) === identity);
      need(!ordinals.has(ref.message_ref) || ordinals.get(ref.message_ref) === entry.ordinal);
      sources.set(entry.ordinal, identity); ordinals.set(ref.message_ref, entry.ordinal);
      need((interval == null) === (kind === 0));
      if (interval) need(exam.coverage?.examined.some((part) => part.start <= interval.start && interval.end <= part.end));
      const order: [string, string, number, number] = [entry.source.source_ref, entry.source.source_version, interval?.start ?? -1, interval?.end ?? -1];
      need(previousEvidence === null || order[0] > previousEvidence[0] || order[0] === previousEvidence[0] && (order[1] > previousEvidence[1]
        || order[1] === previousEvidence[1] && (order[2] > previousEvidence[2] || order[2] === previousEvidence[2] && order[3] > previousEvidence[3])));
      previousEvidence = order;
      const key = JSON.stringify([entry.ordinal, interval?.start ?? -1, interval?.end ?? -1]); need(!occurrences.has(key)); occurrences.add(key);
      messages.add(entry.ordinal); conversations.add(ref.conversation_ref);
      (entry.assessment === 'complete' ? complete : partial).add(entry.ordinal);
    }
    need([...complete].every((ordinal) => !partial.has(ordinal)) && messages.size === item.supporting_messages && conversations.size === item.supporting_conversations
      && occurrences.size === item.occurrences && complete.size === item.complete_supporting_messages && partial.size === item.partial_supporting_messages);
  }
  return result;
}
