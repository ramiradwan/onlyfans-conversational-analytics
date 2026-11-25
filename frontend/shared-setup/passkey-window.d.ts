export function nativePasskeyPresentation(): boolean;
export function passkeyWindow(action: 'passkey_foreground' | 'claim_passkey_unlock' | 'discard_passkey_unlock' | 'open_passkey_settings'): Promise<boolean>;
export const passkeyPermissionMessage: string;
export function openPasskeySettings(): Promise<boolean>;
export function followPasskeyOpen(ready: () => boolean, unlock: () => void): { check(): void; stop(): void };
