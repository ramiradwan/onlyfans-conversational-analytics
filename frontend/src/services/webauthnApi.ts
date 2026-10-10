// eslint-disable-next-line import/extensions -- Runtime .mjs has a matching .d.mts declaration.
import { parseOnboardingJson } from '../../../shared/onboarding/json.mjs';
import { validateOnboardingMessage } from '../protocol/onboarding';

interface RegistrationOptions {
  challenge: string;
  rp: PublicKeyCredentialRpEntity;
  user: PublicKeyCredentialUserEntity & { id: string };
  pubKeyCredParams: PublicKeyCredentialParameters[];
  timeout?: number;
  authenticatorSelection?: AuthenticatorSelectionCriteria;
  attestation?: AttestationConveyancePreference;
}

interface LoginOptions {
  challenge: string;
  rpId: string;
  allowCredentials: Array<PublicKeyCredentialDescriptor & { id: string }>;
  timeout?: number;
  userVerification?: UserVerificationRequirement;
}

export interface WebAuthnApi {
  enroll(): Promise<void>;
  login(): Promise<void>;
  state?(signal: AbortSignal): Promise<{ enrolled: boolean; authenticated: boolean }>;
}

export class EnrollmentOutcomeError extends Error {
  constructor(readonly enrolled: boolean) {
    super('Passkey setup could not be confirmed.');
    this.name = 'EnrollmentOutcomeError';
  }
}

interface WebAuthnApiOptions {
  fetch?: typeof fetch;
  getCsrfToken?: () => string | null;
}

function csrfToken(): string | null {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || null;
}

function base64urlToArrayBuffer(value: string): ArrayBuffer {
  const base64 = value.replace(/-/g, '+').replace(/_/g, '/');
  const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, '=');
  const binary = atob(padded);
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  return bytes.buffer;
}

function arrayBufferToBase64url(value: ArrayBuffer): string {
  const bytes = new Uint8Array(value);
  let binary = '';
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function registrationCredential(credential: PublicKeyCredential) {
  const response = credential.response as AuthenticatorAttestationResponse;
  return {
    id: credential.id,
    rawId: arrayBufferToBase64url(credential.rawId),
    type: 'public-key',
    response: {
      clientDataJSON: arrayBufferToBase64url(response.clientDataJSON),
      attestationObject: arrayBufferToBase64url(response.attestationObject),
    },
  };
}

function loginCredential(credential: PublicKeyCredential) {
  const response = credential.response as AuthenticatorAssertionResponse;
  return {
    id: credential.id,
    rawId: arrayBufferToBase64url(credential.rawId),
    type: 'public-key',
    response: {
      clientDataJSON: arrayBufferToBase64url(response.clientDataJSON),
      authenticatorData: arrayBufferToBase64url(response.authenticatorData),
      signature: arrayBufferToBase64url(response.signature),
      userHandle: response.userHandle === null ? null : arrayBufferToBase64url(response.userHandle),
    },
  };
}

export function createWebAuthnApi(options: WebAuthnApiOptions = {}): WebAuthnApi {
  const request = options.fetch ?? globalThis.fetch.bind(globalThis);
  const getCsrfToken = options.getCsrfToken ?? csrfToken;

  const post = async <ResponseBody>(path: string, body?: object): Promise<ResponseBody> => {
    const csrf = getCsrfToken();
    const response = await request(`/api/v1/webauthn${path}`, {
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        ...(body ? { 'Content-Type': 'application/json' } : {}),
        ...(csrf ? { 'X-CSRF-Token': csrf } : {}),
      },
      method: 'POST',
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    if (!response.ok) throw new Error(`WebAuthn request failed (${response.status})`);
    if (path === '/registration/finish') return parseOnboardingJson(await response.text()) as ResponseBody;
    return response.json() as Promise<ResponseBody>;
  };

  const login = async () => {
    const options = await post<LoginOptions>('/login/begin');
    const credential = await navigator.credentials.get({
      publicKey: {
        ...options,
        challenge: base64urlToArrayBuffer(options.challenge),
        allowCredentials: options.allowCredentials.map((allowed) => ({
          ...allowed,
          id: base64urlToArrayBuffer(allowed.id),
        })),
      },
    }) as PublicKeyCredential | null;
    if (credential === null) throw new Error('No passkey was selected.');
    await post('/login/finish', loginCredential(credential));
  };

  return {
    async state(signal) {
      const response = await request('/api/v1/webauthn/session-state', {
        signal, credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: { Accept: 'application/json' },
      });
      const value: unknown = response.ok ? parseOnboardingJson(await response.text()) : null;
      if (typeof value !== 'object' || value === null || Object.keys(value).length !== 2
        || !('authenticated' in value) || !('enrolled' in value)
        || typeof value.authenticated !== 'boolean' || typeof value.enrolled !== 'boolean'
        || value.authenticated && !value.enrolled) throw new Error('Passkey status unavailable');
      return { enrolled: value.enrolled, authenticated: value.authenticated };
    },
    async enroll() {
      const options = await post<RegistrationOptions>('/registration/begin');
      const credential = await navigator.credentials.create({
        publicKey: {
          ...options,
          challenge: base64urlToArrayBuffer(options.challenge),
          user: {
            ...options.user,
            id: base64urlToArrayBuffer(options.user.id),
          },
        },
      }) as PublicKeyCredential | null;
      if (credential === null) throw new Error('No passkey was created.');
      try {
        const result = await post<unknown>('/registration/finish', registrationCredential(credential));
        if (!validateOnboardingMessage(result)
          || (result as { profile: string }).profile !== 'local-first-enrollment-result.v1') {
          throw new Error('Passkey session was not confirmed.');
        }
      } catch {
        // A lost body can follow a committed credential/session. Read once;
        // never repeat finish, mint another session, or silently invoke get().
        let enrolled = false;
        try {
          const response = await request('/api/v1/webauthn/session-state', {
            credentials: 'same-origin', cache: 'no-store', redirect: 'error',
            headers: { Accept: 'application/json' },
          });
          const status: unknown = response.ok ? parseOnboardingJson(await response.text()) : null;
          if (typeof status === 'object' && status !== null
            && Object.keys(status).length === 2 && 'authenticated' in status && 'enrolled' in status
            && typeof status.authenticated === 'boolean' && typeof status.enrolled === 'boolean') {
            if (status.authenticated && status.enrolled) return;
            enrolled = status.enrolled;
          }
        } catch { /* Preserve uncertainty; this read grants no authority. */ }
        throw new EnrollmentOutcomeError(enrolled);
      }
    },
    login,
  };
}

export const webauthnApi = createWebAuthnApi();
