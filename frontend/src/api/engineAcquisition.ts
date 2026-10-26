import { z } from 'zod';

const safeBytes = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER);
const digest = z.string().regex(/^[a-f0-9]{64}$/);
const operationId = z.string().regex(/^[a-f0-9]{32}$/);
const reason = z.string().regex(/^[a-z_]{1,64}$/);
const selection = z.enum(['module', 'preview']);
export type EngineSelection = z.infer<typeof selection>;
const disk = z.object({
  installed_space_budget_bytes: safeBytes.refine((value) => value > 0),
  temporary_space_budget_bytes: safeBytes,
  rollback_space_budget_bytes: safeBytes,
  required_free_space_bytes: safeBytes,
}).strict().refine((value) => {
  const total = value.installed_space_budget_bytes + value.temporary_space_budget_bytes + value.rollback_space_budget_bytes;
  return Number.isSafeInteger(total) && total === value.required_free_space_bytes;
});
const version = z.string().max(23).refine((value) => /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.test(value)
  && value.split('.').every((part) => Number(part) <= 65535));
const review = z.discriminatedUnion('availability', [
  z.object({ availability: z.literal('available'), explicit_selection: selection, review_digest: digest, engine_package_version: version,
    expected_complete_package_bytes: safeBytes.refine((value) => value > 0), declared_disk: disk }).strict(),
  z.object({ availability: z.literal('unavailable'), reason }).strict(),
]);
const status = z.object({
  operation_id: operationId.nullable(),
  state: z.enum(['not_requested', 'checking', 'acquiring', 'cancelling', 'installed', 'failed', 'recovery_pending']),
  installed: z.boolean(), in_flight: z.boolean(), cancel_requested: z.boolean(), deadline_reached: z.boolean(),
  progress_percent: z.null(), reason: reason.nullable(), hresult: z.number().int().min(-2147483648).max(2147483647).nullable(),
}).strict().refine((value) => (!value.in_flight || (value.operation_id !== null && ['checking', 'acquiring', 'cancelling'].includes(value.state)))
  && (value.state !== 'installed' || value.installed)
  && (!value.installed || ['installed', 'recovery_pending'].includes(value.state)));
export type EngineReview = z.infer<typeof review>;
export type EngineStatus = z.infer<typeof status>;
export interface EngineAcquisitionApi {
  readonly selection: EngineSelection;
  supported(): boolean;
  status(signal?: AbortSignal): Promise<EngineStatus>;
  review(signal?: AbortSignal): Promise<EngineReview>;
  start(reviewDigest: string, signal?: AbortSignal): Promise<EngineStatus>;
  cancel(id: string, signal?: AbortSignal): Promise<EngineStatus>;
}
type Command = 'desktop_engine_acquisition_status' | 'desktop_engine_acquisition_review'
  | 'desktop_engine_acquisition_start' | 'desktop_engine_acquisition_cancel';
type Invoke = (command: Command, args?: Record<string, string>) => Promise<unknown>;
export class EngineAcquisitionError extends Error {
  constructor(readonly kind: 'stale_review' | 'ownership' | 'unavailable') {
    super('Engine setup could not be verified.');
  }
}
function nativeCore() {
  return (globalThis as typeof globalThis & { __TAURI__?: { core?: { invoke: Invoke } } }).__TAURI__?.core;
}
function failure(error: unknown): EngineAcquisitionError {
  if (error === 'optional_quote_stale') return new EngineAcquisitionError('stale_review');
  if (typeof error === 'string' && ['servicing_previous_owner_unsettled', 'servicing_exclusion_busy', 'servicing_exclusion_invalid', 'servicing_exclusion_unavailable',
    'optional_recovery_pending', 'optional_broker_release_changed', 'optional_reconnect_unavailable',
    'update_check_in_progress', 'optional_acquisition_busy'].includes(error)) return new EngineAcquisitionError('ownership');
  return new EngineAcquisitionError('unavailable');
}
export function createEngineAcquisitionApi(options: { selection: EngineSelection; invoke?: Invoke }): EngineAcquisitionApi {
  const selected = selection.parse(options.selection);
  const invoke = options.invoke;
  async function call(command: Command, args?: Record<string, string>, signal?: AbortSignal): Promise<unknown> {
    signal?.throwIfAborted();
    try {
      const core = nativeCore();
      if (!invoke && typeof core?.invoke !== 'function') throw new EngineAcquisitionError('unavailable');
      const result = await (invoke ? invoke(command, args) : core!.invoke(command, args));
      // Presentation cancellation never cancels the retained Windows operation.
      signal?.throwIfAborted();
      return result;
    } catch (error) {
      signal?.throwIfAborted();
      throw failure(error);
    }
  }
  return Object.freeze({
    selection: selected,
    supported: () => Boolean(invoke || typeof nativeCore()?.invoke === 'function'),
    async status(signal?: AbortSignal) { return status.parse(await call('desktop_engine_acquisition_status', undefined, signal)); },
    async review(signal?: AbortSignal) {
      const value = review.parse(await call('desktop_engine_acquisition_review', { explicitSelection: selected }, signal));
      if (value.availability === 'available' && value.explicit_selection !== selected) throw new EngineAcquisitionError('unavailable');
      return value;
    },
    async start(reviewDigest: string, signal?: AbortSignal) {
      digest.parse(reviewDigest);
      return status.parse(await call('desktop_engine_acquisition_start', { explicitSelection: selected, reviewDigest }, signal));
    },
    async cancel(id: string, signal?: AbortSignal) {
      operationId.parse(id);
      const value = status.parse(await call('desktop_engine_acquisition_cancel', { operationId: id }, signal));
      if (value.operation_id !== id) throw new EngineAcquisitionError('unavailable');
      return value;
    },
  });
}
export const moduleEngineAcquisitionApi = createEngineAcquisitionApi({ selection: 'module' });
export const previewEngineAcquisitionApi = createEngineAcquisitionApi({ selection: 'preview' });
export const engineAcquisitionApi = moduleEngineAcquisitionApi;
