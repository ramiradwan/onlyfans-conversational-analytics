import { FindingsClientError } from './findingsClient';
import { LiteralsContractError, parseLiterals, type LiteralFindings, type LiteralsRequest } from './literalsContract';
import type { QuestionEvidence, SourceEvidence } from './questionContract';
import { LITERAL_FINDINGS } from '../config/endpoints';
import { createQuestionApi } from '../services/questionApi';

export interface LiteralsClient {
  read(request: LiteralsRequest, signal?: AbortSignal): Promise<LiteralFindings>;
  evidence(reference: QuestionEvidence, signal?: AbortSignal): Promise<SourceEvidence>;
}
export function buildLiteralsUrl(r: LiteralsRequest): string {
  const params = new URLSearchParams({ expected_generation: String(r.baseline.projection_generation), expected_projection_digest: r.baseline.projection_digest,
    direction: r.direction, row_offset: String(r.row_offset), row_limit: String(r.row_limit), evidence_offset: String(r.evidence_offset), evidence_limit: String(r.evidence_limit) });
  if (r.conversation_ref !== null) params.set('conversation_ref', r.conversation_ref);
  if (r.category_filter) { params.set('category_task', r.category_filter.task); params.set('category_id', r.category_filter.category_id); }
  if (r.node_id !== null) params.set('node_id', r.node_id);
  if (r.expected_normalization_sha256 !== null) params.set('expected_normalization_sha256', r.expected_normalization_sha256);
  if (r.baseline.requested_window.start !== null) params.set('start_date', r.baseline.requested_window.start);
  if (r.baseline.requested_window.end !== null) params.set('end_date', r.baseline.requested_window.end);
  return `${LITERAL_FINDINGS}?${params}`;
}
async function boundedJson(r: Response): Promise<unknown> {
  if (!r.body) throw new LiteralsContractError();
  const reader = r.body.getReader(); const chunks: Uint8Array[] = []; let size = 0;
  try {
    while (true) {
      const value = await reader.read(); if (value.done) break;
      size += value.value.byteLength;
      if (size > 1_048_576) { await reader.cancel(); throw new LiteralsContractError(); }
      chunks.push(value.value);
    }
    const bytes = new Uint8Array(size); let at = 0;
    for (const chunk of chunks) { bytes.set(chunk, at); at += chunk.length; }
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  } finally { reader.releaseLock(); }
}
export function createLiteralsClient(options: { fetcher?: typeof fetch; csrf?: () => string | null } = {}): LiteralsClient {
  const fetcher = options.fetcher ?? globalThis.fetch.bind(globalThis); const sources = createQuestionApi(options);
  return { evidence: sources.evidence, async read(request, signal) {
    const controller = new AbortController(); const abort = () => controller.abort();
    if (signal?.aborted) abort(); signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(abort, 12_000);
    try {
      const response = await fetcher(buildLiteralsUrl(request), { method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: { Accept: 'application/json' }, signal: controller.signal });
      const value = await boundedJson(response);
      if (!response.ok) {
        const code = value && typeof value === 'object' && 'detail' in value && value.detail && typeof value.detail === 'object' && 'code' in value.detail ? value.detail.code : null;
        if (code === 'analytics_projection_building') throw new FindingsClientError('building', 'Saved literal values changed. Refresh the analytics view.');
        if (code === 'analytics_literals_invalid') throw new FindingsClientError('error', 'The saved literal values could not be verified. Refresh the analytics view.');
        if (response.status === 401 || response.status === 403) throw new FindingsClientError('unavailable', 'Your session changed. Reload the app to sign in again.');
        if (response.status === 503) throw new FindingsClientError('unavailable', 'Literal values are unavailable for this saved analysis.');
        throw new FindingsClientError('error', 'Literal values could not be loaded. Check the filters and refresh.');
      }
      return parseLiterals(value, request);
    } catch (error) {
      if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
      if (error instanceof FindingsClientError || error instanceof LiteralsContractError) throw error;
      throw new FindingsClientError('error', 'The app could not be reached. Check that it is running and try again.');
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort); }
  } };
}
