export const CAPABILITY_LICENSE_CONTINUATION_PATTERN = /^clr1\.[A-Za-z0-9_-]{43}$/u;

export type CommercialAuthorityReadiness = 'required' | 'active' | 'unavailable';
export type AnalysisAdmissionReadiness = 'blocked' | 'admitted';

export interface CapabilityLicenseReadiness {
  schema: 'ofca-analysis-readiness/v1';
  commercial_authority: CommercialAuthorityReadiness;
  analysis_admission: AnalysisAdmissionReadiness;
}

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
  readiness(signal?: AbortSignal): Promise<CapabilityLicenseReadiness>;
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
    throw new CapabilityLicenseApiError("Activation couldn't be checked. Try again.");
  }
  const documentValue = value as Record<string, unknown>;
  const keys = Object.keys(documentValue).sort();
  const expected = [...fields].sort();
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
    throw new CapabilityLicenseApiError("Activation couldn't be checked. Try again.");
  }
  return documentValue;
}

function parseReadiness(value: unknown): CapabilityLicenseReadiness {
  const root = exactObject(value, ['schema', 'commercial_authority', 'analysis_admission']);
  const commercial = root.commercial_authority;
  const admission = root.analysis_admission;
  if (
    root.schema !== 'ofca-analysis-readiness/v1'
    || !['required', 'active', 'unavailable'].includes(String(commercial))
    || !['blocked', 'admitted'].includes(String(admission))
    || (admission === 'admitted' && commercial !== 'active')
  ) {
    throw new CapabilityLicenseApiError("Activation couldn't be checked. Try again.");
  }
  return root as unknown as CapabilityLicenseReadiness;
}

function parseRedemption(value: unknown): CapabilityLicenseRedemptionAccepted {
  const root = exactObject(value, ['state']);
  if (root.state !== 'checking') {
    throw new CapabilityLicenseApiError("Activation couldn't be checked. Try again.");
  }
  return { state: 'checking' };
}

const NEW_CODE = 'In secure setup, choose Activate Full to get a new code, then paste it here.';
const WRONG_SETUP = 'This code was made for a different computer or account. '
  + 'Sign in to secure setup with the account you used for this computer and get a new code there.';
const RESTART = "The desktop app couldn't finish activating. Restart it, then paste a new code from secure setup.";
const UNREACHABLE = "Activation couldn't be confirmed right now. Nothing has changed. Try again in a moment.";

const REFUSAL_MESSAGES: Record<string, string> = {
  redemption_expired: `This code has expired. ${NEW_CODE}`,
  redemption_invalid: "This code wasn't recognized. Copy the whole code from secure setup again and paste it here.",
  redemption_conflict: `This code has already been used. If Full analytics isn't on yet, get a new one. ${NEW_CODE}`,
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
    return new CapabilityLicenseApiError('Your session on this page has ended. Reload the page and try again.', status);
  }
  if (status === 409) {
    return new CapabilityLicenseApiError(`Activation couldn't be finished with this code. ${NEW_CODE}`, status);
  }
  if (status === 503) return new CapabilityLicenseApiError(UNREACHABLE, status);
  return new CapabilityLicenseApiError("Activation couldn't be checked. Try again.", status);
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
    async readiness(signal) {
      let response: Response;
      try {
        response = await request(`${endpoint}/readiness`, {
          cache: 'no-store',
          credentials: 'same-origin',
          headers: { Accept: 'application/json' },
          method: 'GET',
          signal,
        });
      } catch (error) {
        if (signal?.aborted) throw error;
        throw new CapabilityLicenseApiError("Activation couldn't be checked. Try again.");
      }
      if (!response.ok) {
        throw new CapabilityLicenseApiError("Activation couldn't be checked. Try again.", response.status);
      }
      try {
        return parseReadiness(await response.json());
      } catch (error) {
        if (error instanceof CapabilityLicenseApiError) throw error;
        throw new CapabilityLicenseApiError("Activation couldn't be checked. Try again.");
      }
    },

    async redeem(continuation, signal) {
      if (!CAPABILITY_LICENSE_CONTINUATION_PATTERN.test(continuation)) {
        throw new CapabilityLicenseApiError('Enter the full activation code and try again.');
      }
      const csrf = await getCsrfToken();
      if (!csrf) {
        throw new CapabilityLicenseApiError("Activation isn't available in this browser. Reload the page and try again.");
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
        throw new CapabilityLicenseApiError("Activation couldn't be checked. Try again.");
      }
    },
  };
}

export const capabilityLicenseApi = createCapabilityLicenseApi();
