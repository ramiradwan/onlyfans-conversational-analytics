import type { FastAPIConfig } from '@/types/config';

/**
 * Reads config injected by FastAPI into index.html via:
 * <script id="fastapi-config" type="application/json">{...}</script>
 *
 * Identity and tickets never receive production fallbacks.
 */
export function getConfig(): FastAPIConfig {
  const el = document.getElementById('fastapi-config');

  if (!el) {
    console.warn('[CONFIG] fastapi-config element not found');
    return {
      EXTENSION_ID: 'dev-extension-id',
      FASTAPI_WS_URL: 'ws://bridge.localhost:17871/ws/bridge',
      API_BASE_URL: 'http://bridge.localhost:17871',
      VERSION: 'dev',
      BRIDGE_ROLE: undefined,
      USER_ID: undefined,
      CREATOR_ID: undefined,
      BRIDGE_AUTH_TICKET: undefined,
      SETUP_TRANSFER_RECEIVER_URL: undefined,
    };
  }

  try {
    const injected = JSON.parse(el.textContent || '{}') as Partial<FastAPIConfig>;
    return {
      EXTENSION_ID: injected.EXTENSION_ID ?? 'dev-extension-id',
      FASTAPI_WS_URL:
        injected.FASTAPI_WS_URL ?? 'ws://bridge.localhost:17871/ws/bridge',
      API_BASE_URL: injected.API_BASE_URL ?? 'http://bridge.localhost:17871',
      VERSION: injected.VERSION ?? 'dev',
      BRIDGE_ROLE:
        injected.BRIDGE_ROLE === 'creator' || injected.BRIDGE_ROLE === 'operator'
          ? injected.BRIDGE_ROLE
          : undefined,
      USER_ID: injected.USER_ID ?? undefined,
      CREATOR_ID: injected.CREATOR_ID ?? undefined,
      BRIDGE_AUTH_TICKET: injected.BRIDGE_AUTH_TICKET ?? undefined,
      SETUP_TRANSFER_RECEIVER_URL: setupTransferReceiverUrl(injected.SETUP_TRANSFER_RECEIVER_URL),
    };
  } catch (err) {
    console.error('[CONFIG] Failed to parse injected FastAPI config', err);
    throw err;
  }
}

/** Only the explicit transfer receiver can be exposed as a receiving link. */
function setupTransferReceiverUrl(value: unknown): string | undefined {
  if (typeof value !== 'string' || value === '') return undefined;
  try {
    const url = new URL(value);
    return url.protocol === 'https:'
      && !url.username && !url.password && !url.search && !url.hash
      && url.pathname === '/public/onboarding/setup'
      ? url.href : undefined;
  } catch {
    return undefined;
  }
}
