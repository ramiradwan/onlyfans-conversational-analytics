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
  enroll(signal?: AbortSignal): Promise<void>;
  login(signal?: AbortSignal): Promise<void>;
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
  requestTimeoutMs?: number;
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

export class LoginOutcomeError extends Error {
  constructor(readonly stage: 'begin' | 'credential' | 'finish') {
    super('Sign-in did not finish.');
    this.name = 'LoginOutcomeError';
  }
}

class RequestRefused extends Error {
  constructor(readonly status: number) { super('Passkey request refused'); }
}

/** Bound the response body as well as the fetch, and retire all work on unmount. */
async function bounded<T>(operation: (signal: AbortSignal) => Promise<T>, parent: AbortSignal | undefined, ms: number): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (parent?.aborted) throw new DOMException('Operation ended', 'AbortError');
  parent?.addEventListener('abort', abort, { once: true });
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([new Promise<T>((_resolve, reject) => {
      controller.signal.addEventListener('abort', () => reject(new DOMException('Operation ended', 'AbortError')), { once: true });
      timer = setTimeout(abort, ms);
    }), operation(controller.signal)]);
  } finally {
    clearTimeout(timer); parent?.removeEventListener('abort', abort);
  }
}

export function createWebAuthnApi(options: WebAuthnApiOptions = {}): WebAuthnApi {
  const request = options.fetch ?? globalThis.fetch.bind(globalThis);
  const getCsrfToken = options.getCsrfToken ?? csrfToken;
  const deadline = options.requestTimeoutMs ?? 8000;
  const state = (signal?: AbortSignal) => bounded(async (requestSignal) => {
    const response = await request('/api/v1/webauthn/session-state', {
      signal: requestSignal, credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: { Accept: 'application/json' },
    });
    const value: unknown = response.ok ? parseOnboardingJson(await response.text()) : null;
    if (typeof value !== 'object' || value === null || Object.keys(value).length !== 2
      || !('authenticated' in value) || !('enrolled' in value)
      || typeof value.authenticated !== 'boolean' || typeof value.enrolled !== 'boolean'
      || value.authenticated && !value.enrolled) throw new Error('Passkey status unavailable');
    return { enrolled: value.enrolled, authenticated: value.authenticated };
  }, signal, deadline);

  const post = <ResponseBody>(path: string, body?: object, signal?: AbortSignal): Promise<ResponseBody> => bounded(async (requestSignal) => {
    const csrf = getCsrfToken();
    const response = await request(`/api/v1/webauthn${path}`, {
      signal: requestSignal, credentials: 'same-origin', cache: 'no-store', redirect: 'error',
      headers: {
        Accept: 'application/json',
        ...(body ? { 'Content-Type': 'application/json' } : {}),
        ...(csrf ? { 'X-CSRF-Token': csrf } : {}),
      },
      method: 'POST',
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    if (!response.ok) throw new RequestRefused(response.status);
    return parseOnboardingJson(await response.text()) as ResponseBody;
  }, signal, deadline);

  const login = async (signal?: AbortSignal) => {
    let loginOptions: LoginOptions;
    try { loginOptions = await post<LoginOptions>('/login/begin', undefined, signal); }
    catch { throw new LoginOutcomeError('begin'); }
    let credential: PublicKeyCredential;
    try {
      const selected = await bounded((ceremonySignal) => navigator.credentials.get({
        signal: ceremonySignal,
        publicKey: {
          ...loginOptions,
          challenge: base64urlToArrayBuffer(loginOptions.challenge),
          allowCredentials: loginOptions.allowCredentials.map((allowed) => ({
            ...allowed, id: base64urlToArrayBuffer(allowed.id),
          })),
        },
      }), signal, 120_000) as PublicKeyCredential | null;
      if (selected === null) throw new Error('No passkey selected');
      credential = selected;
    } catch { throw new LoginOutcomeError('credential'); }
    try {
      const result = await post<unknown>('/login/finish', loginCredential(credential), signal);
      if (typeof result !== 'object' || result === null || Object.keys(result).length !== 1
        || !('csrf_token' in result) || typeof result.csrf_token !== 'string' || !result.csrf_token) {
        throw new Error('Sign-in response unavailable');
      }
    } catch (error) {
      if (signal?.aborted || error instanceof RequestRefused && error.status < 500) throw new LoginOutcomeError('finish');
      // Finish can commit before its reply is lost. Reconcile once by read,
      // never replay the assertion or start another browser ceremony.
      try { if ((await state(signal)).authenticated) return; } catch { /* Keep the outcome unconfirmed. */ }
      throw new LoginOutcomeError('finish');
    }
  };

  return {
    state,
    async enroll(signal) {
      const registrationOptions = await post<RegistrationOptions>('/registration/begin', undefined, signal);
      const credential = await bounded((ceremonySignal) => navigator.credentials.create({
        signal: ceremonySignal,
        publicKey: {
          ...registrationOptions,
          challenge: base64urlToArrayBuffer(registrationOptions.challenge),
          user: { ...registrationOptions.user, id: base64urlToArrayBuffer(registrationOptions.user.id) },
        },
      }), signal, 120_000) as PublicKeyCredential | null;
      if (credential === null) throw new Error('No passkey was created.');
      try {
        const result = await post<unknown>('/registration/finish', registrationCredential(credential), signal);
        if (!validateOnboardingMessage(result)
          || (result as { profile: string }).profile !== 'local-first-enrollment-result.v1') {
          throw new Error('Passkey session was not confirmed.');
        }
      } catch {
        let enrolled = false;
        if (!signal?.aborted) {
          try {
            const status = await state(signal);
            if (status.authenticated) return;
            enrolled = status.enrolled;
          } catch { /* Preserve uncertainty; this read grants no authority. */ }
        }
        throw new EnrollmentOutcomeError(enrolled);
      }
    },
    login,
  };
}

export const webauthnApi = createWebAuthnApi();
