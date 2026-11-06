// eslint-disable-next-line import/extensions -- Runtime .mjs has a matching .d.mts declaration.
import { parseOnboardingJson } from '../../shared/onboarding/json.mjs';
import { validateEnrollment } from './generated-enrollment.js';

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

/** Why the local service keeps this computer from opening analytics. */
export type AccessState = 'renewing' | 'expired' | 'removed';
/** The closed access values that the local service pushes to open pages. */
export type PushedAccess = 'available' | AccessState | 'unavailable';
type RefusedAccess = Exclude<PushedAccess, 'available'>;

const REFUSED_ACCESS: readonly RefusedAccess[] = ['renewing', 'expired', 'removed', 'unavailable'];
const PUSHED_ACCESS: readonly PushedAccess[] = ['available', ...REFUSED_ACCESS];
const ACCESS_EVENTS = '/api/v1/webauthn/session-state/events';

function oneOf<T extends string>(values: readonly T[], value: unknown): value is T {
  return typeof value === 'string' && (values as readonly string[]).includes(value);
}

export interface WebAuthnApi {
  enroll(signal?: AbortSignal): Promise<void>;
  login(signal?: AbortSignal): Promise<void>;
  state?(signal: AbortSignal): Promise<{ enrolled: boolean; authenticated: boolean }>;
  /** Receives each pushed access change until the signal ends. Never rejects. */
  watch?(signal: AbortSignal, receive: (access: PushedAccess) => void): Promise<void>;
}

export class EnrollmentOutcomeError extends Error {
  constructor(readonly enrolled: boolean | null) {
    super('Passkey setup could not be confirmed.');
    this.name = 'EnrollmentOutcomeError';
  }
}

interface WebAuthnApiOptions {
  fetch?: typeof fetch;
  getCsrfToken?: () => string | null;
  setCsrfToken?: (value: string | null) => void;
  requestTimeoutMs?: number;
}

function csrfToken(): string | null {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || null;
}

function saveCsrfToken(value: string | null) {
  let meta = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]');
  if (value === null) { meta?.remove(); return; }
  if (!meta) { meta = document.createElement('meta'); meta.name = 'csrf-token'; document.head.append(meta); }
  meta.content = value;
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

export class WebAuthnStatusError extends Error {
  constructor(
    readonly kind: 'network' | 'session' | 'forbidden' | 'invalid' | 'unavailable' | 'access',
    readonly status?: number,
    /** The closed value a 409 named. Kind 'access' always carries a lapsed state. */
    readonly access: RefusedAccess | null = null,
  ) {
    super('Passkey status could not be read');
    this.name = 'WebAuthnStatusError';
  }
}

/** A 409 names why access is closed with one value from a closed set. */
async function refusal(response: Response): Promise<WebAuthnStatusError> {
  let value: unknown = null;
  try { value = parseOnboardingJson(await response.text()); } catch { /* A malformed refusal stays unavailable. */ }
  if (typeof value === 'object' && value !== null && Object.keys(value).length === 2
    && 'detail' in value && typeof value.detail === 'string' && 'access' in value) {
    const access = value.access;
    if (oneOf(REFUSED_ACCESS, access)) return new WebAuthnStatusError(access === 'unavailable' ? 'unavailable' : 'access', 409, access);
  }
  return new WebAuthnStatusError('unavailable', 409);
}

/** The lapsed state a failed status read reported, or null. */
export function lapsedAccess(error: unknown): AccessState | null {
  return error instanceof WebAuthnStatusError && error.access !== null && error.access !== 'unavailable' ? error.access : null;
}

/**
 * Asks for one status read for each pushed access value that the last read did not observe.
 * Reads and ceremonies hold it, so a value pushed meanwhile is compared when they end.
 */
export class AccessFollower {
  private observed: PushedAccess | null = null;
  private pushed: PushedAccess | null = null;
  private holds = 0;
  private pending = false;

  constructor(private readonly reread: () => void) {}

  push(value: PushedAccess) { this.pushed = value; this.pending = true; this.settle(); }

  hold() { this.holds += 1; }

  /** A read passes what it observed. A ceremony passes nothing, so the last pushed value is compared again. */
  release(outcome?: PushedAccess | null) {
    this.holds = Math.max(0, this.holds - 1);
    if (outcome === undefined) this.pending = true;
    else this.observed = outcome;
    this.settle();
  }

  /** Ends a hold whose read was retired before it observed anything. */
  abandon() { this.holds = Math.max(0, this.holds - 1); this.pending = true; }

  private settle() {
    if (this.holds > 0 || !this.pending) return;
    this.pending = false;
    if (this.pushed !== null && this.pushed !== this.observed) this.reread();
  }
}

function pushedFrame(data: string): PushedAccess {
  let value: unknown;
  try { value = parseOnboardingJson(data); } catch { throw new WebAuthnStatusError('invalid'); }
  if (typeof value !== 'object' || value === null || Object.keys(value).length !== 1 || !('access' in value)) {
    throw new WebAuthnStatusError('invalid');
  }
  const access = value.access;
  if (!oneOf(PUSHED_ACCESS, access)) throw new WebAuthnStatusError('invalid');
  return access;
}

/** Resolves after the delay, or at once when the signal ends. */
function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve(); };
    const timer = setTimeout(finish, signal.aborted ? 0 : ms);
    signal.addEventListener('abort', finish, { once: true });
  });
}

/** Reads one access stream until it ends, goes quiet or carries an invalid frame. */
async function readAccessEvents(request: typeof fetch, parent: AbortSignal, receive: (access: PushedAccess) => void): Promise<void> {
  if (parent.aborted) return;
  const connection = new AbortController();
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const stop = () => { connection.abort(); void reader?.cancel().catch(() => undefined); };
  // Keepalives arrive at least every 15 seconds, so a silent stream is gone.
  const arm = (ms: number) => { clearTimeout(timer); timer = setTimeout(stop, ms); };
  parent.addEventListener('abort', stop, { once: true });
  try {
    arm(10_000);
    let response: Response;
    try {
      response = await request(ACCESS_EVENTS, {
        signal: connection.signal, credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: { Accept: 'text/event-stream' },
      });
    } catch { throw new WebAuthnStatusError('network'); }
    if (!response.ok || !response.body || response.headers.get('content-type')?.split(';')[0] !== 'text/event-stream') {
      throw new WebAuthnStatusError(response.status === 403 ? 'forbidden' : 'unavailable', response.status);
    }
    reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8', { fatal: true });
    let pending = '', data = '', event = '';
    arm(45_000);
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done || connection.signal.aborted) return;
      arm(45_000);
      pending += decoder.decode(chunk.value, { stream: true });
      let end: number;
      while ((end = pending.indexOf('\n')) >= 0) {
        const line = pending.slice(0, end).replace(/\r$/u, '');
        pending = pending.slice(end + 1);
        if (line.length > 1024) throw new WebAuthnStatusError('invalid');
        if (!line) {
          if (event === 'access' && data) receive(pushedFrame(data));
          data = ''; event = '';
        } else if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data += (data ? '\n' : '') + line.slice(5).replace(/^ /u, '');
        if (data.length > 256) throw new WebAuthnStatusError('invalid');
      }
      if (pending.length > 1024) throw new WebAuthnStatusError('invalid');
    }
  } finally {
    clearTimeout(timer);
    parent.removeEventListener('abort', stop);
    connection.abort();
    if (reader) { await reader.cancel().catch(() => undefined); reader.releaseLock(); }
  }
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
  const setCsrfToken = options.setCsrfToken ?? saveCsrfToken;
  const deadline = options.requestTimeoutMs ?? 8000;
  const state = async (signal?: AbortSignal) => {
    try { return await bounded(async (requestSignal) => {
    const response = await request('/api/v1/webauthn/session-state', {
      signal: requestSignal, credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: { Accept: 'application/json' },
    });
    if (response.status === 409) throw await refusal(response);
    if (!response.ok) throw new WebAuthnStatusError(response.status === 401 ? 'session'
      : response.status === 403 ? 'forbidden' : 'unavailable', response.status);
    let value: unknown;
    try { value = parseOnboardingJson(await response.text()); }
    catch { throw new WebAuthnStatusError('invalid'); }
    if (typeof value !== 'object' || value === null || Object.keys(value).length !== 3
      || !('authenticated' in value) || !('enrolled' in value) || !('csrf_token' in value)
      || typeof value.authenticated !== 'boolean' || typeof value.enrolled !== 'boolean'
      || value.authenticated && !value.enrolled
      || (value.authenticated ? typeof value.csrf_token !== 'string' || !value.csrf_token : value.csrf_token !== null)) throw new WebAuthnStatusError('invalid');
    setCsrfToken(value.csrf_token as string | null);
    return { enrolled: value.enrolled, authenticated: value.authenticated };
    }, signal, deadline); }
    catch (error) {
      if (signal?.aborted || error instanceof WebAuthnStatusError) throw error;
      throw new WebAuthnStatusError('network');
    }
  };

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
      setCsrfToken(result.csrf_token);
    } catch (error) {
      if (signal?.aborted || error instanceof RequestRefused && error.status < 500) throw new LoginOutcomeError('finish');
      // Finish can commit before its reply is lost. Reconcile once by read,
      // never replay the assertion or start another browser ceremony.
      try { if ((await state(signal)).authenticated) return; } catch { /* Keep the outcome unconfirmed. */ }
      throw new LoginOutcomeError('finish');
    }
  };

  // Reconnects after each end. A refused origin is permanent for this page.
  const watch = async (signal: AbortSignal, receive: (access: PushedAccess) => void) => {
    let delay = 1000;
    while (!signal.aborted) {
      const opened = Date.now();
      try { await readAccessEvents(request, signal, receive); }
      catch (error) { if (error instanceof WebAuthnStatusError && error.kind === 'forbidden') return; }
      if (signal.aborted) return;
      if (Date.now() - opened > 60_000) delay = 1000;
      await pause(delay, signal);
      delay = Math.min(delay * 2, 30_000);
    }
  };

  return {
    state,
    watch,
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
        if (!validateEnrollment(result)) {
          throw new Error('Passkey session was not confirmed.');
        }
        setCsrfToken((result as { csrf_token: string }).csrf_token);
      } catch {
        let enrolled: boolean | null = null;
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
