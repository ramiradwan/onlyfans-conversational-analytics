export type ReissueState = 'prepared' | 'awaiting_authorization' | 'authorized' | 'adopted' | 'expired';
export interface ReissueStatus { flow_id: string; state: ReissueState; navigation_available: boolean }
export interface ReissueStatusView { current: ReissueStatus | null }
export interface TextAnalysisReissueApi {
  status(signal?: AbortSignal): Promise<ReissueStatusView>;
  begin(signal?: AbortSignal): Promise<ReissueStatusView>;
  resume(flow: string, signal?: AbortSignal): Promise<ReissueStatusView>;
  openBrowser(flow: string, signal?: AbortSignal): Promise<void>;
}
export class TextAnalysisReissueApiError extends Error {
  constructor(readonly code: 'unavailable' | 'unauthorized' | 'refused' | 'unknown') {
    super('Licence recovery could not be verified.');
  }
}
const FLOW = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const STATES: readonly string[] = ['prepared', 'awaiting_authorization', 'authorized', 'adopted', 'expired'];
const fail = () => { throw new TextAnalysisReissueApiError('unknown'); };
function exact(value: unknown, fields: string[]): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return fail();
  const result = value as Record<string, unknown>;
  if (Object.keys(result).length !== fields.length || fields.some(field => !Object.hasOwn(result, field))) return fail();
  return result;
}
function flowKey(value: string) { if (!FLOW.test(value)) return fail(); return value; }
export function parseReissueStatus(value: unknown): ReissueStatusView {
  const root = exact(value, ['current']);
  if (root.current === null) return { current: null };
  const row = exact(root.current, ['flow_id', 'state', 'navigation_available']);
  if (typeof row.flow_id !== 'string' || typeof row.state !== 'string' || !STATES.includes(row.state)
      || typeof row.navigation_available !== 'boolean'
      || (row.navigation_available && row.state !== 'awaiting_authorization')) return fail();
  return { current: { flow_id: flowKey(row.flow_id), state: row.state as ReissueState, navigation_available: row.navigation_available } };
}
async function readDocument(response: Response): Promise<unknown> {
  if (!response.body || response.headers.get('content-type')?.split(';')[0] !== 'application/json'
      || Number(response.headers.get('content-length')) > 4096) return fail();
  const reader = response.body.getReader(), chunks: Uint8Array[] = [];
  let bytes = 0;
  try {
    for (;;) {
      const item = await reader.read();
      if (item.done) break;
      bytes += item.value.byteLength;
      if (bytes > 4096) { await reader.cancel(); return fail(); }
      chunks.push(item.value);
    }
  } finally { reader.releaseLock(); }
  const raw = new Uint8Array(bytes); let offset = 0;
  for (const chunk of chunks) { raw.set(chunk, offset); offset += chunk.byteLength; }
  try { return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw)); } catch { return fail(); }
}
async function openNativeEntry(entryCode: string) {
  const host = globalThis as typeof globalThis & { __TAURI__?: { core?: { invoke(command: string, args: { entryCode: string }): Promise<unknown> } } };
  if (!host.__TAURI__?.core) throw new TextAnalysisReissueApiError('unavailable');
  await host.__TAURI__.core.invoke('desktop_text_analysis_reissue', { entryCode });
}
export function createTextAnalysisReissueApi(options: {
  fetcher?: typeof fetch; csrf?: () => string | null | Promise<string | null>; openEntry?: (entryCode: string) => Promise<void>;
} = {}): TextAnalysisReissueApi {
  const fetcher = options.fetcher ?? ((...args) => globalThis.fetch(...args));
  const csrf = options.csrf ?? (() => globalThis.document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? null);
  const endpoint = '/api/v1/nlp/license-reissue';
  async function request(method: 'GET' | 'POST', suffix: string, signal?: AbortSignal) {
    try {
      const headers: Record<string, string> = { Accept: 'application/json' };
      if (method === 'POST') {
        const token = await csrf();
        if (!token) throw new TextAnalysisReissueApiError('unauthorized');
        headers['Content-Type'] = 'application/json'; headers['X-CSRF-Token'] = token;
      }
      const response = await fetcher(endpoint + suffix, { method, credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        headers, body: method === 'POST' ? '{}' : undefined, signal });
      if (!response.ok) throw new TextAnalysisReissueApiError(response.status === 401 || response.status === 403 ? 'unauthorized'
        : response.status === 503 ? 'unavailable' : response.status === 409 ? 'refused' : 'unknown');
      return await readDocument(response);
    } catch (error) {
      if (signal?.aborted || error instanceof TextAnalysisReissueApiError) throw error;
      throw new TextAnalysisReissueApiError('unknown');
    }
  }
  return {
    status: async signal => parseReissueStatus(await request('GET', '', signal)),
    begin: async signal => parseReissueStatus(await request('POST', '', signal)),
    resume: async (flow, signal) => {
      const result = parseReissueStatus(await request('POST', `/${flowKey(flow)}/resume`, signal));
      if (result.current?.flow_id !== flow) return fail();
      return result;
    },
    openBrowser: async (flow, signal) => {
      const result = exact(await request('POST', `/${flowKey(flow)}/browser-entry`, signal), ['entry_code']);
      if (typeof result.entry_code !== 'string' || !/^[A-Za-z0-9_-]{43}$/u.test(result.entry_code)) return fail();
      if (!signal?.aborted) await (options.openEntry ?? openNativeEntry)(result.entry_code);
    },
  };
}
export const textAnalysisReissueApi = createTextAnalysisReissueApi();
