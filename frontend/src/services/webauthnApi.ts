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
      await post('/registration/finish', registrationCredential(credential));
    },
    login,
  };
}

export const webauthnApi = createWebAuthnApi();
