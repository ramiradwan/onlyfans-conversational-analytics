import { z } from 'zod';

import { readBoundedFindingsJson } from '../analytics/findingsClient';
import { textAnalysisUuid } from '../analytics/textAnalysisContract';

const acquisitionKind = z.enum(['preview', 'paid']);
export type AcquisitionKind = z.infer<typeof acquisitionKind>;
const paidTerms = z.object({ payment_mode: z.literal('test'), amount_minor: z.literal(999), currency: z.literal('EUR'),
  licensed_major_version: z.literal(1), update_rights: z.literal(true) }).strict();
const previewTerms = z.object({ execution_limit: z.literal(1) }).strict();
const date = z.string().datetime({ offset: true });
const flow = z.object({
  flow_id: textAnalysisUuid,
  state: z.enum(['prepared', 'awaiting_browser', 'confirmation_required', 'confirming', 'authorized', 'adopted', 'expired', 'cancelled', 'unavailable']),
  expires_at: date.nullable(), navigation_available: z.boolean(),
  review: z.object({ review_id: textAnalysisUuid, expires_at: date, terms: z.union([paidTerms, previewTerms]) }).strict().nullable(),
}).strict();
const saved = z.object({ current: flow.nullable(), eligible: z.boolean(),
  reason: z.enum(['already_acquired', 'account_unavailable', 'disabled']).nullable() }).strict();
export type AcquisitionStatus = z.infer<typeof saved>;
export type AcquisitionFlow = z.infer<typeof flow>;
const code = z.string().regex(/^[A-Za-z0-9_-]{43}$/u);
function check(ok: unknown): asserts ok {
  if (!ok) throw new Error('The acquisition could not be verified.');
}
function verify(view: AcquisitionFlow, kind: AcquisitionKind, id?: string) {
  check((id === undefined || view.flow_id === id) && (!view.navigation_available || view.state === 'awaiting_browser'));
  if (view.review) {
    check(view.state === 'confirmation_required' && view.review.expires_at === view.expires_at);
    (kind === 'paid' ? paidTerms : previewTerms).parse(view.review.terms);
  }
  return view;
}
type OpenEntry = (kind: AcquisitionKind, entryCode: string) => Promise<unknown>;
type NativeCore = { invoke(command: string, args: { kind: AcquisitionKind; entryCode: string }): Promise<unknown> };
const core = () => (globalThis as typeof globalThis & { __TAURI__?: { core?: NativeCore } }).__TAURI__?.core;
export function createTextAnalysisAcquisitionApi(options: {
  fetcher?: typeof fetch; csrf?: () => string | null; openEntry?: OpenEntry;
} = {}) {
  const fetcher = options.fetcher ?? ((...args) => fetch(...args));
  const csrf = options.csrf ?? (() => document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? null);
  async function request(kind: AcquisitionKind, action: string, signal: AbortSignal, body?: object) {
    acquisitionKind.parse(kind);
    signal.throwIfAborted();
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (body) {
      const token = csrf(); check(token); headers['X-CSRF-Token'] = token; headers['Content-Type'] = 'application/json';
    }
    const response = await fetcher(`/api/v1/nlp/text-analysis-acquisition/${kind}${action}`, { method: body ? 'POST' : 'GET',
      credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers,
      body: body && JSON.stringify(body), signal: AbortSignal.any([signal, AbortSignal.timeout(15_000)]) });
    check(response.ok && response.headers.get('Content-Type')?.split(';')[0] === 'application/json');
    const data = await readBoundedFindingsJson(response, 4096);
    signal.throwIfAborted();
    return data;
  }
  async function change(kind: AcquisitionKind, action: string, body: object, signal: AbortSignal, expectedId?: string) {
    const data = z.object({ current: flow }).strict().parse(await request(kind, action, signal, body));
    return verify(data.current, kind, expectedId);
  }
  return {
    supported: () => Boolean(options.openEntry || core()),
    async status(kind: AcquisitionKind, signal: AbortSignal) {
      const status = saved.parse(await request(kind, '', signal));
      check(status.eligible === (status.reason === null));
      if (status.current) verify(status.current, kind);
      return status;
    },
    prepare: (kind: AcquisitionKind, signal: AbortSignal) => change(kind, '/prepare', {}, signal),
    read: (kind: AcquisitionKind, id: string, signal: AbortSignal) =>
      change(kind, `/${textAnalysisUuid.parse(id)}/read`, {}, signal, id),
    submit: (kind: AcquisitionKind, id: string, reviewId: string, signal: AbortSignal) =>
      change(kind, `/${textAnalysisUuid.parse(id)}/submit`, { review_id: textAnalysisUuid.parse(reviewId) }, signal, id),
    async openBrowser(kind: AcquisitionKind, id: string, signal: AbortSignal, active: () => boolean) {
      const entry = z.object({ entry_code: code }).strict()
        .parse(await request(kind, `/${textAnalysisUuid.parse(id)}/browser-entry`, signal, {}));
      check(active() && !signal.aborted);
      if (options.openEntry) await options.openEntry(kind, entry.entry_code);
      else {
        const native = core(); check(native);
        await native.invoke('desktop_text_analysis_acquisition', { kind, entryCode: entry.entry_code });
      }
    },
  };
}
export type TextAnalysisAcquisitionApi = ReturnType<typeof createTextAnalysisAcquisitionApi>;
export const textAnalysisAcquisitionApi = createTextAnalysisAcquisitionApi();
