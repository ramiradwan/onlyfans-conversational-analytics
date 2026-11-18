import type { HistorySettings, UpdateHistorySettingsRequest } from '../protocol';
import {
  integer,
  isoDateTime,
  literal,
  nonEmptyString,
  nullable,
  object,
} from '../protocol/validation';

const historySettings = object({
  creator_account_id: nonEmptyString,
  settings_revision: integer(0),
  consent_policy_version: nonEmptyString,
  consent_revision: nullable(nonEmptyString),
  authorized_platform_creator_id: nullable(nonEmptyString),
  desired_state: literal('not_started', 'running', 'paused', 'revoked'),
  effective_state: literal('not_applied', 'running', 'paused', 'revoked'),
  effective_config_revision: nullable(nonEmptyString),
  recent_window_days: integer(1),
  page_size: integer(1, 100),
  pages_per_wake: integer(1),
  request_interval_ms: integer(0),
  retry_limit: integer(0),
  updated_at: isoDateTime,
});

// HTTP 412 is the pre-write revision guard; 409 can be returned after
// publication of a saved change. Never infer a mutation outcome from text.
export type HistoryFailure = 'transport' | 'conflict' | 'refused' | 'unknown';

export function historyFailure(cause: unknown): HistoryFailure {
  if (cause instanceof TypeError) return 'transport';
  if (!(cause instanceof HistorySettingsApiError)) return 'unknown';
  if (cause.notSent || [400, 401, 403, 422, 428].includes(cause.status ?? 0)) return 'refused';
  if (cause.status === 412) return 'conflict';
  if ([408, 503].includes(cause.status ?? 0)) return 'transport';
  return 'unknown';
}

export class HistorySettingsApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null = null,
    readonly notSent = false,
  ) {
    super(message);
    this.name = 'HistorySettingsApiError';
  }
}

export interface HistorySettingsApi {
  get(signal?: AbortSignal): Promise<HistorySettings>;
  update(
    revision: number,
    input: UpdateHistorySettingsRequest,
    signal?: AbortSignal,
  ): Promise<HistorySettings>;
  revoke(revision: number, signal?: AbortSignal): Promise<HistorySettings>;
}

interface HistorySettingsApiOptions {
  baseUrl?: string;
  csrfHeaderName?: string;
  fetch?: typeof fetch;
  getCsrfToken?: () => string | null | Promise<string | null>;
}

function defaultCsrfToken(): string | null {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || null;
}

async function parseResponse(response: Response): Promise<HistorySettings> {
  if (!response.ok) {
    const conflict = response.status === 409 || response.status === 412;
    throw new HistorySettingsApiError(
      conflict
        ? 'Message history settings changed.'
        : `History settings request failed (${response.status}).`,
      response.status,
    );
  }
  try {
    const document = (await response.json()) as unknown;
    historySettings(document, '$');
    return document as HistorySettings;
  } catch (error) {
    if (error instanceof HistorySettingsApiError) throw error;
    throw new HistorySettingsApiError('Brain returned invalid history settings.', response.status);
  }
}

export function createHistorySettingsApi(
  options: HistorySettingsApiOptions = {},
): HistorySettingsApi {
  const request = options.fetch ?? globalThis.fetch.bind(globalThis);
  const csrfHeaderName = options.csrfHeaderName ?? 'X-CSRF-Token';
  const getCsrfToken = options.getCsrfToken ?? defaultCsrfToken;
  const endpoint = `${(options.baseUrl ?? '').replace(/\/$/, '')}/api/v1/settings/history`;
  const consentEndpoint = `${endpoint}/consent`;

  const mutationHeaders = async (revision: number): Promise<Record<string, string>> => {
    const csrf = await getCsrfToken();
    if (!csrf) throw new HistorySettingsApiError('The settings request was not sent.', null, true);
    return {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'If-Match': String(revision),
      [csrfHeaderName]: csrf,
    };
  };

  return {
    async get(signal) {
      return parseResponse(
        await request(endpoint, {
          credentials: 'same-origin',
          headers: { Accept: 'application/json' },
          method: 'GET',
          signal,
        }),
      );
    },
    async update(revision, input, signal) {
      return parseResponse(
        await request(endpoint, {
          body: JSON.stringify(input),
          credentials: 'same-origin',
          headers: await mutationHeaders(revision),
          method: 'PUT',
          signal,
        }),
      );
    },
    async revoke(revision, signal) {
      return parseResponse(
        await request(consentEndpoint, {
          credentials: 'same-origin',
          headers: await mutationHeaders(revision),
          method: 'DELETE',
          signal,
        }),
      );
    },
  };
}

export const historySettingsApi = createHistorySettingsApi();
