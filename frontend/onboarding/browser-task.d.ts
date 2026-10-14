import type { DesktopSetupClient } from './client';
export function openBrowserTask(client: DesktopSetupClient, operationId: string,
  invoke?: (code: string) => Promise<unknown>, current?: () => boolean): Promise<boolean>;
