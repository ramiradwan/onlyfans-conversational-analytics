import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const transport = vi.hoisted(() => ({
  connect: vi.fn(),
  disconnect: vi.fn(),
}));

vi.mock('../src/services/websocketService', () => ({
  websocketService: transport,
}));

vi.mock('../src/routing/AppRouter', () => ({
  AppRouter: () => <main>Application routes</main>,
}));

import { App } from '../src/App';
import { useUserStore } from '../src/store/userStore';

function injectConfig(role: unknown) {
  const element = document.createElement('script');
  element.id = 'fastapi-config';
  element.type = 'application/json';
  element.textContent = JSON.stringify({
    EXTENSION_ID: 'dev-extension-id',
    FASTAPI_WS_URL: 'ws://bridge.localhost:17871/ws/bridge',
    API_BASE_URL: 'http://bridge.localhost:17871',
    VERSION: '0.7.5',
    BRIDGE_ROLE: role,
    CREATOR_ID: 'creator-1',
    BRIDGE_AUTH_TICKET: 'bridge-ticket',
  });
  document.body.append(element);
}

afterEach(() => {
  cleanup();
  document.getElementById('fastapi-config')?.remove();
  useUserStore.getState().actions.setUserRole(null);
  vi.clearAllMocks();
});

describe('signed Bridge role bootstrap', () => {
  it('maps the authenticated operator role before enabling the Bridge connection', async () => {
    injectConfig('operator');

    render(<App />);

    await waitFor(() => expect(useUserStore.getState().role).toBe('operator'));
    expect(transport.connect).toHaveBeenCalledWith(
      'ws://bridge.localhost:17871/ws/bridge',
      'creator-1',
      'bridge-ticket',
    );

    cleanup();
    expect(useUserStore.getState().role).toBeNull();
  });

  it('fails closed when the injected role is not a signed Brain role', async () => {
    injectConfig('creator-ceo');
    vi.spyOn(console, 'error').mockImplementation(() => undefined);

    render(<App />);

    await waitFor(() => expect(useUserStore.getState().role).toBeNull());
    expect(transport.connect).not.toHaveBeenCalled();
  });
});
