import { z } from 'zod';

const bytes = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER);
const digest = z.string().regex(/^[a-f0-9]{64}$/);
const operationId = z.string().regex(/^[a-f0-9]{32}$/);
const resume = z.literal('verified_files_current_file_restarts');
const profile = z.object({
  profile_id: z.string().min(1), revision: z.string().min(1), profile_sha256: digest,
  catalogued: z.boolean(), installed: z.boolean(), prepared: z.boolean(), withdrawn: z.boolean(),
  storage_state: z.string(), languages: z.array(z.string()).nullable(), tasks: z.array(z.string()).nullable(),
  resources: z.record(z.string(), z.unknown()).nullable(),
});
const observation = z.object({
  id: operationId, kind: z.string(), state: z.string(), verified_files: bytes,
  profile_sha256: digest.nullable(), profile_id: z.string().nullable(), revision: z.string().nullable(),
  in_flight: z.boolean(), resume_granularity: resume,
});
const status = z.object({
  configured: z.boolean(), profiles: z.array(profile).max(64), available_profiles: z.array(digest),
  failure: z.string().nullable(), resume_granularity: resume,
  acquisitions: z.array(observation).max(32).default([]),
  selected: z.object({
    profile_id: z.string(), revision: z.string(), profile_sha256: digest,
    mode: z.string(), selection_epoch: bytes, admission_epoch: bytes,
  }).nullable(),
});
const plan = z.object({
  operation_id: operationId, profile_sha256: digest,
  declared_full_artifact_download_bytes: bytes, data_download_bytes: bytes,
  incremental_data_download_bytes: bytes, profile_metadata_bytes: bytes,
  installed_bytes: bytes, rollback_bytes: bytes, acquisition_temporary_bytes: bytes,
  transition_peak_memory_bytes: bytes, inference_threads: z.number().int().positive(),
});
export type NlpDependencyStatus = z.infer<typeof status>;
export type NlpDependencyProfile = z.infer<typeof profile>;
export type NlpDependencyObservation = z.infer<typeof observation>;
export type NlpDependencyPlan = z.infer<typeof plan>;
export type NlpDependencyCommand =
  | { action: 'refresh' | 'rollback' | 'account_pause' | 'account_enable' }
  | { action: 'plan' | 'select'; profile_id: string; revision: string }
  | { action: 'acquire'; profile_id: string; revision: string; operation_id: string }
  | { action: 'pause' | 'cancel' | 'resume'; operation_id: string }
  | { action: 'prepare'; profile_sha256: string }
  | { action: 'remove'; profile_id?: string; revision?: string };
export interface NlpDependencyResult { operation: unknown; status: NlpDependencyStatus }
export interface NlpDependenciesApi {
  get(signal?: AbortSignal): Promise<NlpDependencyStatus>;
  observe(id: string, signal?: AbortSignal): Promise<NlpDependencyObservation>;
  command(input: NlpDependencyCommand, signal?: AbortSignal): Promise<NlpDependencyResult>;
}
export function parseNlpDependencyPlan(value: unknown): NlpDependencyPlan { return plan.parse(value); }

export function createNlpDependenciesApi(options: {
  fetch?: typeof fetch; getCsrfToken?: () => string | null | Promise<string | null>;
} = {}): NlpDependenciesApi {
  const request = options.fetch ?? globalThis.fetch.bind(globalThis);
  const endpoint = '/api/v1/nlp/dependencies';
  const token = options.getCsrfToken ?? (() => document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? null);
  async function read(response: Response): Promise<unknown> {
    if (!response.ok) throw new Error('Text analysis settings could not be updated.');
    try { return await response.json(); } catch { throw new Error('Text analysis settings are unavailable.'); }
  }
  return {
    async get(signal) {
      return status.parse(await read(await request(endpoint, { method: 'GET', credentials: 'same-origin', headers: { Accept: 'application/json' }, signal })));
    },
    async observe(id, signal) {
      operationId.parse(id);
      const result = observation.parse(await read(await request(`${endpoint}?operation_id=${encodeURIComponent(id)}`, { method: 'GET', credentials: 'same-origin', headers: { Accept: 'application/json' }, signal })));
      if (result.id !== id) throw new Error('Text analysis operation could not be verified.');
      return result;
    },
    async command(input, signal) {
      const csrf = await token();
      signal?.throwIfAborted();
      if (!csrf) throw new Error('Sign in again to change text analysis settings.');
      const result = z.object({ operation: z.unknown(), status }).parse(await read(await request(endpoint, {
        method: 'POST', credentials: 'same-origin', signal,
        headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify(input),
      })));
      if (input.action === 'plan') plan.parse(result.operation);
      if (input.action === 'acquire') operationId.parse(result.operation);
      return result;
    },
  };
}
export const nlpDependenciesApi = createNlpDependenciesApi();
