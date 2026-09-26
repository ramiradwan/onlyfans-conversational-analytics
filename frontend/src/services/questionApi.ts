import {
  parseQuestionResult, parseSourceEvidence, questionCatalog, questionEvidence, questionPlan,
  QuestionContractError, type QuestionCatalog, type QuestionEvidence, type QuestionPlan,
  type QuestionResult, type SourceEvidence,
} from '../analytics/questionContract';

export class QuestionApiError extends Error {
  constructor(readonly state: 'error' | 'building' | 'unavailable' | 'stale', message: string) {
    super(message);
  }
}
export interface QuestionApi {
  catalog(signal?: AbortSignal): Promise<QuestionCatalog>;
  answer(plan: QuestionPlan, signal?: AbortSignal): Promise<QuestionResult>;
  evidence(reference: QuestionEvidence, signal?: AbortSignal): Promise<SourceEvidence>;
  clear(signal?: AbortSignal): Promise<void>;
}
interface Options {
  fetcher?: typeof fetch;
  csrf?: () => string | null;
}
async function boundedJson(response: Response): Promise<unknown> {
  if (!response.body) throw new QuestionContractError();
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 1_048_576) { await reader.cancel(); throw new QuestionContractError(); }
      chunks.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  } finally { reader.releaseLock(); }
}
function httpError(status: number, value: unknown): QuestionApiError {
  const detail = value && typeof value === 'object' && 'detail' in value ? value.detail : null;
  const code = detail && typeof detail === 'object' && 'code' in detail ? detail.code : null;
  const availability = detail && typeof detail === 'object' && 'availability' in detail ? detail.availability : null;
  if (status === 401 || status === 403) return new QuestionApiError('unavailable', 'Your session changed. Reload the app to sign in again.');
  if (code === 'analytics_pricing_not_qualified') return new QuestionApiError('unavailable', 'Pricing discussions are not available yet.');
  if (status === 404 || code === 'analytics_question_cursor_stale' || code === 'analytics_question_cursor_invalid') {
    return new QuestionApiError('stale', 'These results or source messages are no longer available. Run the question again.');
  }
  if (availability === 'building') return new QuestionApiError('building', 'Your latest messages are being analyzed. Try again shortly.');
  if (code === 'analytics_question_limit_exceeded') return new QuestionApiError('unavailable', 'This account has more data than this question can process right now.');
  if (status === 422) return new QuestionApiError('error', 'Choose a valid date range and run the question again.');
  if (availability === 'error') return new QuestionApiError('error', 'The saved analysis could not be prepared. Try again shortly.');
  if (status === 503) return new QuestionApiError('unavailable', 'Conversation questions are not available right now. Try again shortly.');
  return new QuestionApiError('error', 'The question could not be completed. Try again.');
}

export function createQuestionApi(options: Options = {}): QuestionApi {
  const fetcher = options.fetcher ?? globalThis.fetch.bind(globalThis);
  const csrf = options.csrf ?? (() => document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? null);
  async function request(method: string, path = '', body?: unknown, signal?: AbortSignal): Promise<unknown> {
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (method !== 'GET') {
      const token = csrf();
      if (!token) throw new QuestionApiError('unavailable', 'Reload the app to sign in again.');
      headers['X-CSRF-Token'] = token;
    }
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(abort, 12_000);
    try {
      const response = await fetcher(`/api/v1/insights/questions${path}`, {
        method, headers, credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal,
      });
      const value = response.status === 204 ? null : await boundedJson(response);
      if (!response.ok) throw httpError(response.status, value);
      return value;
    } catch (error) {
      if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
      if (error instanceof QuestionApiError || error instanceof QuestionContractError) throw error;
      throw new QuestionApiError('error', 'The app could not be reached. Check that it is running and try again.');
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort); }
  }
  return {
    async catalog(signal) {
      const parsed = questionCatalog.safeParse(await request('GET', '', undefined, signal));
      if (!parsed.success) throw new QuestionContractError();
      return parsed.data;
    },
    async answer(plan, signal) {
      const checked = questionPlan.safeParse(plan);
      if (!checked.success) throw new QuestionContractError();
      return parseQuestionResult(await request('POST', '', checked.data, signal), checked.data);
    },
    async evidence(reference, signal) {
      const checked = questionEvidence.safeParse(reference);
      if (!checked.success) throw new QuestionContractError();
      return parseSourceEvidence(await request('POST', '/evidence', checked.data, signal), checked.data);
    },
    async clear(signal) { await request('DELETE', '/evidence', undefined, signal); },
  };
}
