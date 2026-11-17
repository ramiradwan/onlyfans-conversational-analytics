import type { DesktopSetupClient, DesktopSnapshot } from './client';
export function browserRecoveryTarget(snapshot: DesktopSnapshot | null, reason?: string | null): string | null;
export function openBrowserTask(client: DesktopSetupClient, operationId: string,
  invoke?: (code: string) => Promise<unknown>, current?: () => boolean): Promise<boolean>;
