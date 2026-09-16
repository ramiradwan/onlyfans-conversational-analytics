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
    throw new CapabilityLicenseApiError('Brain returned an invalid activation response.');
  }
  const documentValue = value as Record<string, unknown>;
  const keys = Object.keys(documentValue).sort();
  const expected = [...fields].sort();
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
    throw new CapabilityLicenseApiError('Brain returned an invalid activation response.');
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
    throw new CapabilityLicenseApiError('Brain returned an invalid activation response.');
  }
  return root as unknown as CapabilityLicenseReadiness;
}

function parseRedemption(value: unknown): CapabilityLicenseRedemptionAccepted {
  const root = exactObject(value, ['state']);
  if (root.state !== 'checking') {
    throw new CapabilityLicenseApiError('Brain returned an invalid activation response.');
  }
  return { state: 'checking' };
}

function redemptionFailure(status: number): CapabilityLicenseApiError {
  if (status === 410) {
    return new CapabilityLicenseApiError(
      'This activation continuation has expired. Get a new one and try again.',
      status,
    );
  }
  if (status === 404 || status === 422) {
    return new CapabilityLicenseApiError(
      'This activation continuation is not valid. Check it and try again.',
      status,
    );
  }
  if (status === 401 || status === 403) {
    return new CapabilityLicenseApiError(
      'This activation could not be authorized on this computer. Check your account and try again.',
      status,
    );
  }
  if (status === 409) {
    return new CapabilityLicenseApiError(
      'This activation no longer matches this computer. Get a current continuation and try again.',
      status,
    );
  }
  if (status === 503) {
    return new CapabilityLicenseApiError(
      'Activation could not be confirmed. Try again; existing activation remains unchanged.',
      status,
    );
  }
  return new CapabilityLicenseApiError('Activation could not be checked. Try again.', status);
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
      const response = await request(`${endpoint}/readiness`, {
        cache: 'no-store',
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        method: 'GET',
        signal,
      });
      if (!response.ok) {
        throw new CapabilityLicenseApiError('Activation status could not be checked.', response.status);
      }
      try {
        return parseReadiness(await response.json());
      } catch (error) {
        if (error instanceof CapabilityLicenseApiError) throw error;
        throw new CapabilityLicenseApiError('Brain returned an invalid activation response.');
      }
    },

    async redeem(continuation, signal) {
      if (!CAPABILITY_LICENSE_CONTINUATION_PATTERN.test(continuation)) {
        throw new CapabilityLicenseApiError('Enter the complete activation continuation and try again.');
      }
      const csrf = await getCsrfToken();
      if (!csrf) {
        throw new CapabilityLicenseApiError('Activation is unavailable in this browser session.');
      }
      const response = await request(`${endpoint}/redeem`, {
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
      if (!response.ok) throw redemptionFailure(response.status);
      try {
        return parseRedemption(await response.json());
      } catch (error) {
        if (error instanceof CapabilityLicenseApiError) throw error;
        throw new CapabilityLicenseApiError('Brain returned an invalid activation response.');
      }
    },
  };
}

export const capabilityLicenseApi = createCapabilityLicenseApi();
