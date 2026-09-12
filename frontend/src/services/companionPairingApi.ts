import { z } from 'zod';

const pairingId = z.string().regex(/^[A-Za-z0-9_-]{43}$/);
const statusSchema = z.strictObject({
  pairing_id: pairingId,
  creator_account_id: z.string().min(1).max(256),
  generation: z.number().int().positive().max(Number.MAX_SAFE_INTEGER),
  version: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
  state: z.enum([
    'open', 'claimed', 'offered', 'awaiting_confirmation', 'confirmed',
    'admitted', 'declined', 'cancelled', 'expired', 'revoked',
  ]),
  expires_at: z.iso.datetime({ offset: true }),
  comparison_code: z.string().regex(/^\d{6}$/).nullable(),
  agent_identity_thumbprint: pairingId.nullable(),
}).refine((value) => value.state !== 'awaiting_confirmation' || (
  value.comparison_code !== null && value.agent_identity_thumbprint !== null
));

export type CompanionPairingStatus = z.infer<typeof statusSchema>;
export type CompanionPairingAction = 'confirm' | 'decline' | 'cancel';

export interface CompanionPairingApi {
  open(creatorAccountId: string, signal?: AbortSignal): Promise<CompanionPairingStatus>;
  get(pairingId: string, signal?: AbortSignal): Promise<CompanionPairingStatus>;
  change(
    pairingId: string,
    action: CompanionPairingAction,
    version: number,
    signal?: AbortSignal,
  ): Promise<CompanionPairingStatus>;
}

export class CompanionPairingApiError extends Error {
  constructor(readonly code: 'csrf' | 'request' | 'response' | 'timeout' | 'cancelled') {
    super('The extension pairing request could not be completed.');
    this.name = 'CompanionPairingApiError';
  }
}

interface ApiOptions {
  fetch?: typeof fetch;
  getCsrfToken?: () => string | null;
}

// This endpoint returns public presentation state only, never grants or key material.
const MAX_RESPONSE_BYTES = 8_192;
const REQUEST_DEADLINE_MS = 10_000;
const ENDPOINT = '/api/v1/companion/pairings';

async function readStatus(response: Response): Promise<CompanionPairingStatus> {
  if (!response.ok) throw new CompanionPairingApiError('request');
  if (!response.body) throw new CompanionPairingApiError('response');
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let length = 0;
  let text = '';
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      length += part.value.byteLength;
      if (length > MAX_RESPONSE_BYTES) throw new CompanionPairingApiError('response');
      text += decoder.decode(part.value, { stream: true });
    }
    text += decoder.decode();
    const parsed = statusSchema.safeParse(JSON.parse(text) as unknown);
    if (!parsed.success) throw new CompanionPairingApiError('response');
    return parsed.data;
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export function createCompanionPairingApi(options: ApiOptions = {}): CompanionPairingApi {
  const request = options.fetch ?? ((...args: Parameters<typeof fetch>) => globalThis.fetch(...args));
  const csrfToken = options.getCsrfToken ?? (() => (
    document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || null
  ));

  const invoke = async (
    path: string,
    body: object | undefined,
    signal?: AbortSignal,
  ): Promise<CompanionPairingStatus> => {
    const controller = new AbortController();
    const cancel = () => controller.abort();
    signal?.addEventListener('abort', cancel, { once: true });
    if (signal?.aborted) controller.abort();
    let timedOut = false;
    const timeout = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, REQUEST_DEADLINE_MS);
    try {
      if (controller.signal.aborted) throw new CompanionPairingApiError('cancelled');
      const csrf = body === undefined ? null : csrfToken();
      if (body !== undefined && !csrf) throw new CompanionPairingApiError('csrf');
      const response = await request(path, {
        method: body === undefined ? 'GET' : 'POST',
        credentials: 'same-origin',
        redirect: 'error',
        cache: 'no-store',
        headers: {
          Accept: 'application/json',
          ...(csrf ? { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf } : {}),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        signal: controller.signal,
      });
      const value = await readStatus(response);
      if (controller.signal.aborted) throw new CompanionPairingApiError('cancelled');
      return value;
    } catch (error) {
      if (timedOut) throw new CompanionPairingApiError('timeout');
      if (signal?.aborted) throw new CompanionPairingApiError('cancelled');
      if (error instanceof CompanionPairingApiError) throw error;
      // Neither native fetch errors nor parser diagnostics are a public API surface.
      throw new CompanionPairingApiError('response');
    } finally {
      clearTimeout(timeout);
      signal?.removeEventListener('abort', cancel);
    }
  };

  const forPairing = async (
    id: string,
    action?: CompanionPairingAction,
    version?: number,
    signal?: AbortSignal,
  ) => {
    if (!pairingId.safeParse(id).success) throw new CompanionPairingApiError('request');
    const value = await invoke(
      `${ENDPOINT}/${id}${action ? `/${action}` : ''}`,
      action ? { version } : undefined,
      signal,
    );
    if (value.pairing_id !== id) throw new CompanionPairingApiError('response');
    return value;
  };

  return {
    async open(creatorAccountId, signal) {
      const value = await invoke(ENDPOINT, { creator_account_id: creatorAccountId }, signal);
      if (value.creator_account_id !== creatorAccountId) {
        throw new CompanionPairingApiError('response');
      }
      return value;
    },
    get: (id, signal) => forPairing(id, undefined, undefined, signal),
    change: (id, action, version, signal) => forPairing(id, action, version, signal),
  };
}

export const companionPairingApi = createCompanionPairingApi();
