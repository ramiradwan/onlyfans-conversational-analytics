import type { AnalysisReadiness } from '../../onboarding/client.js';

export const CAPABILITY_LICENSE_CONTINUATION_PATTERN = /^clr1\.[A-Za-z0-9_-]{43}$/u;

export type CommercialAuthorityReadiness = 'required' | 'active' | 'unavailable';
export type AnalysisAdmissionReadiness = 'blocked' | 'admitted';

export type CapabilityLicenseReadiness = AnalysisReadiness;

export interface CapabilityLicenseRedemptionAccepted {
  state: 'checking';
}

export class CapabilityLicenseApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null = null,
  ) {
    super(message);
    this.name = 'CapabilityLicenseApiError';
  }
}

export interface CapabilityLicenseApi {
  redeem(continuation: string, signal?: AbortSignal): Promise<CapabilityLicenseRedemptionAccepted>;
}

interface CapabilityLicenseApiOptions {
  baseUrl?: string;
  csrfHeaderName?: string;
  fetch?: typeof fetch;
  getCsrfToken?: () => string | null | Promise<string | null>;
}

function defaultCsrfToken(): string | null {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || null;
}

function exactObject(value: unknown, fields: readonly string[]): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new CapabilityLicenseApiError("Activation couldn't be checked.");
  }
  const documentValue = value as Record<string, unknown>;
  const keys = Object.keys(documentValue).sort();
  const expected = [...fields].sort();
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
    throw new CapabilityLicenseApiError("Activation couldn't be checked.");
  }
  return documentValue;
}

function parseRedemption(value: unknown): CapabilityLicenseRedemptionAccepted {
  const root = exactObject(value, ['state']);
  if (root.state !== 'checking') {
    throw new CapabilityLicenseApiError("Activation couldn't be checked.");
  }
  return { state: 'checking' };
}

const WRONG_SETUP = 'This code was made for a different computer or account.';
const RESTART = "The desktop app couldn't finish activating. Restart the app.";
const UNREACHABLE = "Activation couldn't be confirmed.";

const REFUSAL_MESSAGES: Record<string, string> = {
  redemption_expired: 'This code has expired.',
  redemption_invalid: "This code wasn't recognized. Check the full code and try again.",
  redemption_conflict: 'This code has already been used.',
  redemption_unauthorized: WRONG_SETUP,
  redemption_mismatch: WRONG_SETUP,
  local_installation_authority_unavailable: RESTART,
  installation_key_unavailable: RESTART,
  durable_store_unavailable: RESTART,
  hosted_origin_unavailable: UNREACHABLE,
  hosted_unavailable: UNREACHABLE,
};

function redemptionFailure(status: number, detail: unknown): CapabilityLicenseApiError {
  if (typeof detail === 'string' && Object.hasOwn(REFUSAL_MESSAGES, detail)) {
    return new CapabilityLicenseApiError(REFUSAL_MESSAGES[detail], status);
  }
  if (status === 410) return new CapabilityLicenseApiError(REFUSAL_MESSAGES.redemption_expired, status);
  if (status === 404 || status === 422) {
    return new CapabilityLicenseApiError(REFUSAL_MESSAGES.redemption_invalid, status);
  }
  if (status === 401 || status === 403) {
    return new CapabilityLicenseApiError('The activation request could not be verified.', status);
  }
  if (status === 409) {
    return new CapabilityLicenseApiError("Activation couldn't be finished with this code.", status);
  }
  if (status === 503) return new CapabilityLicenseApiError(UNREACHABLE, status);
  return new CapabilityLicenseApiError("Activation couldn't be checked.", status);
}

async function refusalDetail(response: Response): Promise<unknown> {
  try {
    const body: unknown = await response.json();
    return typeof body === 'object' && body !== null ? (body as { detail?: unknown }).detail : undefined;
  } catch {
    return undefined;
  }
}

export function createCapabilityLicenseApi(
  options: CapabilityLicenseApiOptions = {},
): CapabilityLicenseApi {
  const request = options.fetch ?? globalThis.fetch.bind(globalThis);
  const csrfHeaderName = options.csrfHeaderName ?? 'X-CSRF-Token';
  const getCsrfToken = options.getCsrfToken ?? defaultCsrfToken;
  const endpoint = `${(options.baseUrl ?? '').replace(/\/$/, '')}/api/v1/capability-license`;

  return {
    async redeem(continuation, signal) {
      if (!CAPABILITY_LICENSE_CONTINUATION_PATTERN.test(continuation)) {
        throw new CapabilityLicenseApiError('Enter the full activation code and try again.');
      }
      const csrf = await getCsrfToken();
      if (!csrf) {
        throw new CapabilityLicenseApiError("Activation couldn't start on this page.");
      }
      let response: Response;
      try {
        response = await request(`${endpoint}/redeem`, {
          body: JSON.stringify({ continuation }),
          cache: 'no-store',
          credentials: 'same-origin',
          headers: {
            Accept: 'application/json',
            'Content-Type': 'application/json',
            [csrfHeaderName]: csrf,
          },
          method: 'POST',
          signal,
        });
      } catch (error) {
        if (signal?.aborted) throw error;
        throw new CapabilityLicenseApiError(
          UNREACHABLE,
        );
      }
      if (!response.ok) throw redemptionFailure(response.status, await refusalDetail(response));
      try {
        return parseRedemption(await response.json());
      } catch (error) {
        if (error instanceof CapabilityLicenseApiError) throw error;
        throw new CapabilityLicenseApiError("Activation couldn't be checked.");
      }
    },
  };
}

export const capabilityLicenseApi = createCapabilityLicenseApi();
