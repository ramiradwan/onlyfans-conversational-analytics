import { parseFindings, parseGroupedFindings, FindingsContractError, type FindingsRequest, type FindingsResponse } from './findingsContract';
import type { EvidenceContextRequest, EvidenceContext, QuestionEvidence, SourceEvidence } from './questionContract';
import { findingsRecovery, recoveryClass } from '../../shared-setup/recovery.js';
import { FINDINGS } from '../config/endpoints';
import { createQuestionApi } from '../services/questionApi';

export class FindingsClientError extends Error {
  constructor(readonly state: 'building' | 'unavailable' | 'error', message: string) { super(message); }
}
export interface FindingsClient {
  read(request: FindingsRequest, signal?: AbortSignal): Promise<FindingsResponse>;
  context(request: EvidenceContextRequest, source: SourceEvidence, signal?: AbortSignal): Promise<EvidenceContext>;
  evidence(reference: QuestionEvidence, signal?: AbortSignal): Promise<SourceEvidence>;
}
export function buildFindingsUrl(request: FindingsRequest): string {
  const params = new URLSearchParams({ expected_generation: String(request.baseline.projection_generation),
    expected_projection_digest: request.baseline.projection_digest, direction: request.direction,
    evidence_offset: String(request.evidence_offset), evidence_limit: String(request.evidence_limit),
    conversation_offset: String(request.conversation_offset), conversation_limit: String(request.conversation_limit) });
  if (request.conversation_ref !== null) params.set('conversation_ref', request.conversation_ref);
  if (request.category_filter) {
    params.set('category_task', request.category_filter.task);
    params.set('category_id', request.category_filter.category_id);
  }
  if (request.view === 'grouped') params.set('view', 'grouped');
  if (request.expected_navigation_sha256 != null) params.set('expected_navigation_sha256', request.expected_navigation_sha256);
  if (request.baseline.requested_window.start) params.set('start_date', request.baseline.requested_window.start);
  if (request.baseline.requested_window.end) params.set('end_date', request.baseline.requested_window.end);
  return `${FINDINGS}?${params}`;
}
async function boundedJson(response: Response): Promise<unknown> {
  if (!response.body) throw new FindingsContractError();
  const reader = response.body.getReader(); const chunks: Uint8Array[] = []; let size = 0;
  try {
    while (true) {
      const next = await reader.read(); if (next.done) break;
      size += next.value.byteLength;
      if (size > 1_048_576) { await reader.cancel(); throw new FindingsContractError(); }
      chunks.push(next.value);
    }
    const data = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.byteLength; }
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(data));
  } finally { reader.releaseLock(); }
}
export function createFindingsClient(options: { fetcher?: typeof fetch; csrf?: () => string | null } = {}): FindingsClient {
  const fetcher = options.fetcher ?? globalThis.fetch.bind(globalThis);
  const sources = createQuestionApi(options);
  return {
    evidence: sources.evidence, context: sources.context,
    async read(request, signal) {
      const controller = new AbortController(); const abort = () => controller.abort();
      if (signal?.aborted) controller.abort();
      signal?.addEventListener('abort', abort, { once: true });
      const timer = setTimeout(abort, 12_000);
      try {
        const result = await fetcher(buildFindingsUrl(request), { method: 'GET', credentials: 'same-origin',
          cache: 'no-store', redirect: 'error', headers: { Accept: 'application/json' }, signal: controller.signal });
        const value = await boundedJson(result);
        if (!result.ok) {
          const code = value && typeof value === 'object' && 'detail' in value && value.detail
            && typeof value.detail === 'object' && 'code' in value.detail ? value.detail.code : null;
          const classification = recoveryClass(findingsRecovery, code);
          if (classification === 'operation_pending') throw new FindingsClientError('building', 'Saved findings aren’t ready yet.');
          if (result.status === 401 || result.status === 403) throw new FindingsClientError('unavailable', 'Your session changed. Reload the app to sign in again.');
          if (classification === 'invariant_failed') throw new FindingsClientError('error', 'The saved text analysis could not be verified.');
          if (classification === 'owner_unavailable' || result.status === 503 && classification === null) throw new FindingsClientError('unavailable', 'Text analysis is unavailable for this saved analysis. Other analytics remain available.');
          throw new FindingsClientError('error', 'Text analysis results could not be loaded.');
        }
        const parsed = request.view === 'grouped' ? await parseGroupedFindings(value, request) : parseFindings(value, request);
        if (controller.signal.aborted) throw new DOMException('Cancelled', 'AbortError');
        return parsed;
      } catch (error) {
        if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
        if (error instanceof FindingsClientError || error instanceof FindingsContractError) throw error;
        throw new FindingsClientError('error', 'Dipsy could not be reached.');
      } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort); }
    },
  };
}
