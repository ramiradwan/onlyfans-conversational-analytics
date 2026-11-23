import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../src/routing/AppRouter', () => ({
  AppRouter: () => <main>Application routes <dialog open><button>Privileged action</button></dialog></main>,
}));

vi.mock('../shared-setup/webauthn', async () => {
  const actual = await vi.importActual<typeof import('../shared-setup/webauthn')>('../shared-setup/webauthn');
  return { ...actual, webauthnApi: { ...actual.webauthnApi,
    state: vi.fn(async () => ({ enrolled: true, authenticated: false })), login: vi.fn(),
  } };
});

import { App } from '../src/App';
import { webauthnApi } from '../shared-setup/webauthn';
import { websocketService, type WebSocketLike } from '../src/services/websocketService';
import { bridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { protocolErrorText } from '../src/utils/statusCopy';

const protocolError = JSON.parse(readFileSync('../shared/fixtures/protocol/v2/protocol.error.json', 'utf8'));
const endedSession = { ...protocolError.payload, fatal: true, code: 'unauthorized' };

function mockTransport() {
  const socket: WebSocketLike = {
    readyState: 1, onopen: null, onmessage: null, onerror: null, onclose: null,
    send: vi.fn(), close: vi.fn(),
  };
  vi.stubGlobal('WebSocket', vi.fn(function () { return socket; }));
  return { socket, connect: vi.spyOn(websocketService, 'connect') };
}

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

function injectUnauthenticatedConfig() {
  const element = document.createElement('script');
  element.id = 'fastapi-config';
  element.type = 'application/json';
  element.textContent = JSON.stringify({
    EXTENSION_ID: 'dev-extension-id',
    FASTAPI_WS_URL: 'ws://bridge.localhost:17871/ws/bridge',
    API_BASE_URL: 'http://bridge.localhost:17871',
    VERSION: '0.7.5',
    BRIDGE_ROLE: null,
    CREATOR_ID: null,
    BRIDGE_AUTH_TICKET: null,
  });
  document.body.append(element);
}

afterEach(() => {
  cleanup();
  document.getElementById('fastapi-config')?.remove();
  useUserStore.getState().actions.setUserRole(null);
  bridgeTransportStore.reset();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe('signed Bridge role bootstrap', () => {
  it('maps the authenticated operator role before enabling the Bridge connection', async () => {
    injectConfig('operator');
    const transport = mockTransport();

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
    const transport = mockTransport();
    vi.spyOn(console, 'error').mockImplementation(() => undefined);

    render(<App />);

    await waitFor(() => expect(useUserStore.getState().role).toBeNull());
    expect(transport.connect).not.toHaveBeenCalled();
  });

  it('shows passkey access instead of Bridge routes without a session identity', async () => {
    injectUnauthenticatedConfig();
    const transport = mockTransport();

    render(<App />);

    await waitFor(() => expect(useUserStore.getState().role).toBeNull());
    await waitFor(() => expect(screen.getByRole('heading', { level: 1, name: 'Confirm it’s you' })).toBeTruthy());
    expect(document.body.textContent).not.toContain('Application routes');
    expect(transport.connect).not.toHaveBeenCalled();
  });

  it('uses the session-ended copy without a manual reload instruction', () => {
    expect(protocolErrorText(endedSession)).toBe('Your session ended. Sign in to continue.');
  });

  it('replaces a stale privileged route with the in-place passkey gate on fatal authorization', async () => {
    injectConfig('creator');
    const { socket, connect } = mockTransport();
    render(<App />);
    screen.getByRole('button', { name: 'Privileged action' }).focus();

    const receive = (payload: typeof endedSession) => act(() => {
      socket.onmessage?.({ data: JSON.stringify({ ...protocolError, payload }) });
    });
    receive({ ...endedSession, fatal: false });
    expect(screen.getByRole('button', { name: 'Privileged action' })).toBe(document.activeElement);
    receive(endedSession);
    expect(bridgeTransportStore.getState().protocolError?.code).toBe('unauthorized');

    await screen.findByRole('button', { name: 'Sign in with passkey' });
    expect(screen.getByText('Your session ended. Sign in to continue.')).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByText('Application routes')).toBeNull();
    expect(socket.close).toHaveBeenCalledOnce();
    expect(useUserStore.getState().role).toBeNull();
    expect(connect).toHaveBeenCalledOnce();
    expect(webauthnApi.login).not.toHaveBeenCalled();
  });
});
