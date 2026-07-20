import { z } from 'zod';

import {
  AnalyticsContractError,
  parseAnalyticsUpdate,
  type AnalyticsUpdateDocument,
} from './analyticsContract';
import type { AnalyticsDateRange } from './analyticsReadModel';
import { INSIGHTS_FULL } from '../config/endpoints';

const errorDetailSchema = z
  .object({
    code: z.string().min(1),
    message: z.string().min(1),
    availability: z.string().nullable().optional(),
    retryable: z.boolean().nullable().optional(),
  })
  .strict();
const errorEnvelopeSchema = z.object({ detail: errorDetailSchema }).strict();

export type AnalyticsHttpStatus = 401 | 403 | 404 | 422 | 503;

export class AnalyticsClientError extends Error {
  readonly status: AnalyticsHttpStatus | null;
  readonly code: string;
  readonly availability: string | null;
  readonly retryable: boolean;

  constructor(options: {
    status?: AnalyticsHttpStatus;
    code: string;
    message: string;
    availability?: string | null;
    retryable?: boolean;
  }) {
    super(options.message);
    this.name = 'AnalyticsClientError';
    this.status = options.status ?? null;
    this.code = options.code;
    this.availability = options.availability ?? null;
    this.retryable = options.retryable ?? false;
  }
}

function utcBoundary(date: string, boundary: 'start' | 'end'): string {
  const suffix = boundary === 'start' ? 'T00:00:00.000Z' : 'T23:59:59.999Z';
  return new Date(`${date}${suffix}`).toISOString();
}

export function buildAnalyticsUrl(baseUrl: string, range?: AnalyticsDateRange): string {
  const base = baseUrl.replace(/\/$/, '');
  const url = new URL(`${base}${INSIGHTS_FULL}`, window.location.origin);
  if (range?.startDate) url.searchParams.set('start_date', utcBoundary(range.startDate, 'start'));
  if (range?.endDate) url.searchParams.set('end_date', utcBoundary(range.endDate, 'end'));
  return url.toString();
}

async function parseError(response: Response): Promise<AnalyticsClientError> {
  let detail: z.infer<typeof errorDetailSchema> | null = null;
  try {
    const parsed = errorEnvelopeSchema.safeParse(await response.json());
    if (parsed.success) detail = parsed.data.detail;
  } catch {
    detail = null;
  }

  const status = [401, 403, 404, 422, 503].includes(response.status)
    ? (response.status as AnalyticsHttpStatus)
    : null;
  const defaults: Record<AnalyticsHttpStatus, { code: string; message: string }> = {
    401: { code: 'authentication_failed', message: 'The session could not be authenticated.' },
    403: { code: 'account_binding_mismatch', message: 'The session is not authorized for these analytics.' },
    404: { code: 'analytics_unavailable', message: 'Canonical analytics are not available for this account.' },
    422: { code: 'analytics_request_invalid', message: 'The analytics range is invalid.' },
    503: { code: 'analytics_unavailable', message: 'Canonical analytics are still being prepared.' },
  };
  const fallback = status === null
    ? { code: 'analytics_http_error', message: 'Canonical analytics could not be loaded.' }
    : defaults[status];
  return new AnalyticsClientError({
    status: status ?? undefined,
    code: detail?.code ?? fallback.code,
    message: detail?.message ?? fallback.message,
    availability: detail?.availability ?? null,
    retryable: detail?.retryable ?? status === 503,
  });
}

export interface FetchAnalyticsUpdateOptions {
  baseUrl?: string;
  range?: AnalyticsDateRange;
  signal?: AbortSignal;
  fetcher?: typeof fetch;
}

/**
 * Fetches the account-bound analytics snapshot from the bridge-session-authenticated
 * REST surface. Authority comes solely from the same-origin session cookie the browser
 * attaches automatically (mirrors `messageApi`/`historySettingsApi`); no ticket, header,
 * or account identifier is sent, and none is read, logged, or persisted here.
 */
export async function fetchAnalyticsUpdate(
  options: FetchAnalyticsUpdateOptions = {},
): Promise<AnalyticsUpdateDocument> {
  const request = options.fetcher ?? globalThis.fetch.bind(globalThis);
  let response: Response;
  try {
    response = await request(buildAnalyticsUrl(options.baseUrl ?? '', options.range), {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      method: 'GET',
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new AnalyticsClientError({
      code: 'analytics_network_error',
      message: 'Canonical analytics could not be reached.',
      retryable: true,
    });
  }
  if (!response.ok) throw await parseError(response);
  let document: unknown;
  try {
    document = await response.json();
  } catch {
    throw new AnalyticsContractError('The analytics response was not valid JSON.');
  }
  return parseAnalyticsUpdate(document);
}
