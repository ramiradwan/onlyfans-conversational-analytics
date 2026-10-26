import { parseSourceEvidence, questionEvidence, type QuestionEvidence, type SourceEvidence } from '../analytics/questionContract';
import {
  clientIntent, enrollmentCommand, parseClientIntentPage, parseClientIntentRecord, parseEnrollmentCommandResult,
  parseEnrollmentView, verifyPreviewOutcome, parseSavedPreview, parseSavedPreviewPage, previewConfirmation, previewPrepare, textAnalysisUuid,
  type ClientIntent, type ClientIntentCursor, type ClientIntentKind, type ClientIntentPage, type ClientIntentRecord,
  type EnrollmentCommand, type EnrollmentCommandResult, type EnrollmentView, type PreviewConfirmation, type PreviewOutcome,
  type PreviewPrepareRequest, type SavedPreview, type SavedPreviewPage,
} from '../analytics/textAnalysisContract';

export class TextAnalysisApiError extends Error {
  constructor(readonly kind: 'unauthorized' | 'conflict' | 'unavailable' | 'unknown', readonly status: number | null = null) {
    super(kind === 'unauthorized' ? 'Your session changed. Sign in again before continuing.'
      : kind === 'conflict' ? 'The saved request or account state changed. Check its status before continuing.'
        : kind === 'unavailable' ? 'Text analysis is currently unavailable.'
          : 'The request outcome could not be checked. Recover its original status before continuing.');
  }
}
export interface TextAnalysisApi {
  current(signal?: AbortSignal): Promise<EnrollmentView>;
  command(body: EnrollmentCommand, signal?: AbortSignal): Promise<EnrollmentCommandResult>;
  commandReceipt(key: string, signal?: AbortSignal): Promise<EnrollmentCommandResult | null>;
  prepare(body: PreviewPrepareRequest, signal?: AbortSignal): Promise<PreviewOutcome>;
  outcome(key: string, signal?: AbortSignal): Promise<PreviewOutcome | null>;
  confirm(key: string, body: PreviewConfirmation, signal?: AbortSignal): Promise<PreviewOutcome>;
  savedList(after?: string, signal?: AbortSignal): Promise<SavedPreviewPage>;
  saved(id: string, signal?: AbortSignal): Promise<SavedPreview>;
  retain(intent: ClientIntent, signal?: AbortSignal): Promise<ClientIntentRecord>;
  retained(kind: ClientIntentKind, key: string, signal?: AbortSignal): Promise<ClientIntentRecord | null>;
  retainedList(after?: ClientIntentCursor, signal?: AbortSignal): Promise<ClientIntentPage>;
  evidence(contributionId: string, reference: QuestionEvidence, signal?: AbortSignal): Promise<SourceEvidence>;
}
async function boundedJson(response: Response): Promise<unknown> {
  if (!response.body) throw new TextAnalysisApiError('unknown');
  const reader = response.body.getReader(); const chunks: Uint8Array[] = []; let size = 0;
  try {
    while (true) {
      const next = await reader.read(); if (next.done) break;
      size += next.value.byteLength;
      if (size > 2_097_152) { await reader.cancel(); throw new TextAnalysisApiError('unknown'); }
      chunks.push(next.value);
    }
    const data = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.byteLength; }
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(data));
  } finally { reader.releaseLock(); }
}
export function createTextAnalysisApi(options: { fetcher?: typeof fetch; csrf?: () => string | null | Promise<string | null> } = {}): TextAnalysisApi {
  const fetcher = options.fetcher ?? globalThis.fetch.bind(globalThis);
  const csrf = options.csrf ?? (() => document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? null);
  async function request(method: 'GET' | 'POST', path: string, body: unknown, signal?: AbortSignal, absentCode?: string): Promise<unknown> {
    const controller = new AbortController(), abort = () => controller.abort();
    if (signal?.aborted) controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(abort, 12_000);
    try {
      const headers: Record<string, string> = { Accept: 'application/json' };
      if (method === 'POST') {
        const token = await csrf(); controller.signal.throwIfAborted();
        if (!token) throw new TextAnalysisApiError('unauthorized');
        headers['X-CSRF-Token'] = token; headers['Content-Type'] = 'application/json';
      }
      controller.signal.throwIfAborted();
      const response = await fetcher(`/api/v1/nlp/${path}`, { method, credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        headers, signal: controller.signal, body: method === 'POST' ? JSON.stringify(body) : undefined });
      const value = await boundedJson(response); controller.signal.throwIfAborted();
      if (!response.ok) {
        const detail = value && typeof value === 'object' && 'detail' in value ? value.detail : null;
        const code = detail && typeof detail === 'object' && 'code' in detail ? detail.code : null;
        if (response.status === 404 && absentCode && code === absentCode) return null;
        throw new TextAnalysisApiError(response.status === 401 || response.status === 403 ? 'unauthorized'
          : response.status === 409 ? 'conflict' : response.status === 503 ? 'unavailable' : 'unknown', response.status);
      }
      return value;
    } catch (error) {
      if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
      if (error instanceof TextAnalysisApiError) throw error;
      throw new TextAnalysisApiError('unknown');
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort); }
  }
  return {
    async evidence(contributionId, reference, signal) {
      textAnalysisUuid.parse(contributionId);
      const input = questionEvidence.parse(reference);
      return parseSourceEvidence(await request('POST', `previews/results/${contributionId}/evidence`, input, signal), input);
    },
    async current(signal) { return parseEnrollmentView(await request('GET', 'execution/view', undefined, signal)); },
    async command(body, signal) {
      const input = enrollmentCommand.parse(body);
      return parseEnrollmentCommandResult(await request('POST', 'execution', input, signal), input.command_key);
    },
    async commandReceipt(key, signal) {
      textAnalysisUuid.parse(key);
      const value = await request('GET', `execution/commands/${key}`, undefined, signal, 'text_analysis_command_unknown');
      return value === null ? null : parseEnrollmentCommandResult(value, key);
    },
    async prepare(body, signal) {
      const input = previewPrepare.parse(body);
      return verifyPreviewOutcome(await request('POST', 'previews', input, signal), input.external_key, input);
    },
    async outcome(key, signal) {
      textAnalysisUuid.parse(key);
      const value = await request('GET', `previews/${key}`, undefined, signal, 'text_analysis_preview_unknown');
      return value === null ? null : verifyPreviewOutcome(value, key);
    },
    async confirm(key, body, signal) {
      textAnalysisUuid.parse(key); const input = previewConfirmation.parse(body);
      if (input.external_key !== key) throw new TextAnalysisApiError('conflict');
      return verifyPreviewOutcome(await request('POST', `previews/${key}/confirm`, input, signal), key);
    },
    async savedList(after, signal) {
      if (after) textAnalysisUuid.parse(after);
      const query = new URLSearchParams({ limit: '64', ...(after ? { after_contribution: after } : {}) });
      return parseSavedPreviewPage(await request('GET', `previews/saved?${query}`, undefined, signal), after);
    },
    async saved(id, signal) {
      textAnalysisUuid.parse(id);
      return parseSavedPreview(await request('GET', `previews/results/${id}/view`, undefined, signal), id);
    },
    async retain(intent, signal) {
      const input = clientIntent.parse(intent);
      return parseClientIntentRecord(await request('POST', 'client-intents', input, signal), input);
    },
    async retained(kind, key, signal) {
      clientIntentKind(kind); textAnalysisUuid.parse(key);
      const value = await request('GET', `client-intents/${kind}/${key}`, undefined, signal, 'text_analysis_client_unknown');
      return value === null ? null : parseClientIntentRecord(value, { kind, key });
    },
    async retainedList(after, signal) {
      if (after) { clientIntentKind(after.kind); textAnalysisUuid.parse(after.key); }
      const query = new URLSearchParams({ limit: '64', ...(after ? { after_kind: after.kind, after_key: after.key } : {}) });
      return parseClientIntentPage(await request('GET', `client-intents?${query}`, undefined, signal), after);
    },
  };
}
function clientIntentKind(value: ClientIntentKind) {
  if (!['enrollment_command', 'preview_prepare', 'preview_confirm'].includes(value)) throw new TextAnalysisApiError('unknown');
}
export const textAnalysisApi = createTextAnalysisApi();
