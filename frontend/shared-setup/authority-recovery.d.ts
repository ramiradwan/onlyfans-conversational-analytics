import type { DesktopState, SignInProvider } from '../onboarding/client';
export type RecoveryAction = 'account.connect' | 'auth.select' | 'auth.cancel' | 'browser.open';
export interface RecoveryPresentation {
  title: string; description: string; status: string; tone: 'neutral' | 'progress' | 'warning' | 'error';
  action: RecoveryAction | null; label: string | null; cancel: boolean; cancelPrimary?: boolean; disabled: boolean;
}
export function authorityRecoveryView(state: DesktopState, acting?: RecoveryAction | null): RecoveryPresentation | null;
export function authorityRecoveryOptions(action: RecoveryAction): { recover_authority: true; provider?: SignInProvider };
