import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../src/routing/AppRouter', () => ({
  AppRouter: () => <main>Application routes {protectedRoute ?? <dialog open><button>Privileged action</button></dialog>}</main>,
}));

vi.mock('../shared-setup/webauthn', async () => {
  const actual = await vi.importActual<typeof import('../shared-setup/webauthn')>('../shared-setup/webauthn');
  return { ...actual, webauthnApi: { ...actual.webauthnApi,
    state: vi.fn(async () => ({ enrolled: true, authenticated: false })), login: vi.fn(),
  } };
});

import { App } from '../src/App';
import { webauthnApi } from '../shared-setup/webauthn';
import { BrowserExtensionControls } from '../src/components/BrowserExtensionControls';
import { CommercialActivationControls } from '../src/components/CommercialActivationControls';
import { MessageHistoryControls } from '../src/components/MessageHistoryControls';
import type { HistorySettings } from '../src/protocol';
import type { HistorySettingsApi } from '../src/services/historySettingsApi';
import { analyticsStoreActions } from '../src/store/analyticsStore';
import { websocketService, type WebSocketLike } from '../src/services/websocketService';
import { bridgeTransportStore, createBridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { protocolErrorText } from '../src/utils/statusCopy';
import { desktopFixture } from './desktopOnboardingFixture';

let protectedRoute: ReactNode = null;

const protocolError = JSON.parse(readFileSync('../shared/fixtures/protocol/v2/protocol.error.json', 'utf8'));
const endedSession = { ...protocolError.payload, fatal: true, code: 'unauthorized' };
const expire = (socket: WebSocketLike) => act(() => {
  socket.onmessage?.({ data: JSON.stringify({ ...protocolError, payload: endedSession }) });
});

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
  protectedRoute = null;
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

  it('reports a version mismatch without restarting the application or prompting to reload', () => {
    injectConfig('creator');
    const { socket, connect } = mockTransport();
    const startUrl = window.location.href;
    const previousDocument = document;
    const versionError = { ...endedSession, code: 'unsupported_version' };
    expect(protocolErrorText({ code: 'unsupported_version', fatal: true })).toBe('The app and this page use different versions.');
    render(<App />);
    const { virtualConsole } = jsdom;
    const navigation = vi.fn();
    virtualConsole.on('jsdomError', navigation);
    try {
      act(() => socket.onmessage?.({ data: JSON.stringify({ ...protocolError, payload: versionError }) }));
    } finally {
      virtualConsole.off('jsdomError', navigation);
    }
    expect(navigation).not.toHaveBeenCalled();
    expect(window.location.href).toBe(startUrl);
    expect(document).toBe(previousDocument);
    expect(connect).toHaveBeenCalledOnce();
    expect(screen.queryByRole('button', { name: /reload|sign in with passkey/i })).toBeNull();
    expect(webauthnApi.login).not.toHaveBeenCalled();
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

  it('retires a protected form and ignores its pending completion after expiry', async () => {
    injectConfig('creator');
    const { socket, connect } = mockTransport();
    const activate = vi.spyOn(analyticsStoreActions, 'activate');
    const deactivate = vi.spyOn(analyticsStoreActions, 'deactivate');
    let settle!: (value: { state: 'checking' }) => void;
    const redeem = vi.fn((_code: string, _signal?: AbortSignal) => new Promise<{ state: 'checking' }>(resolve => { settle = resolve; }));
    const fixture = desktopFixture();
    protectedRoute = <CommercialActivationControls api={{ redeem }} onboarding={fixture.api} />;
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: 'Use an activation code' }));
    const field = await screen.findByRole('textbox', { name: 'Activation code' });
    fireEvent.change(field, { target: { value: 'clr1.' + 'A'.repeat(43) } });
    fireEvent.click(screen.getByRole('button', { name: 'Activate', exact: true }));
    expect(redeem).toHaveBeenCalledOnce();
    expire(socket);
    await screen.findByRole('button', { name: 'Sign in with passkey' });
    expect(redeem.mock.calls[0][1]?.aborted).toBe(true);
    await act(async () => settle({ state: 'checking' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(field.isConnected).toBe(false);
    expect(screen.queryByText('Application routes')).toBeNull();
    expect(activate).toHaveBeenCalledOnce();
    expect(deactivate).toHaveBeenCalledOnce();
    expect(connect).toHaveBeenCalledOnce();
    expect(redeem).toHaveBeenCalledOnce();
  });

  it('retires a pending browser review after expiry without sending a consent or follow-up action', async () => {
    injectConfig('creator');
    const { socket } = mockTransport();
    const fixture = desktopFixture();
    fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!, allowed_actions: ['browser.access'] } });
    let settle!: () => void;
    const begin = vi.fn(() => new Promise<void>(resolve => { settle = resolve; }));
    const applyCapture = vi.fn(async () => 'delivered' as const);
    const openBrowser = vi.fn(async () => 'delivered' as const);
    protectedRoute = <BrowserExtensionControls api={{ setCapture: applyCapture, open: openBrowser }}
      browser={{ capture: 'active', site_access: 'granted', history_permission: 'missing', legal_review_required: true,
        reported_at: '2026-11-23T07:00:00Z' }} canManage connection="connected" continuationCreatorId="creator-account"
      onboarding={{ ...fixture.api, begin }} />;
    render(<App />);
    expect(begin).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Review in browser' }));
    expect(begin).toHaveBeenCalledExactlyOnceWith('browser.access');
    expect(screen.getByText('Opening browser…')).toBeTruthy();
    expire(socket);
    await screen.findByRole('button', { name: 'Sign in with passkey' });
    await act(async () => settle());
    expect(screen.queryByText('Application routes')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Review in browser' })).toBeNull();
    expect(begin).toHaveBeenCalledOnce();
    expect(applyCapture).not.toHaveBeenCalled();
    expect(openBrowser).not.toHaveBeenCalled();
    expect(webauthnApi.login).not.toHaveBeenCalled();
  });

  it('prevents a stale history consent response from requesting browser permission after expiry', async () => {
    injectConfig('creator');
    const { socket } = mockTransport();
    const fixture = desktopFixture();
    const store = createBridgeTransportStore();
    const creator = 'creator-account';
    const when = '2026-11-23T07:00:00Z';
    const { payload } = JSON.parse(readFileSync('../shared/fixtures/protocol/v2/agent.state.json', 'utf8'));
    store.bindAccount(creator);
    store.setConnection('connected');
    store.setAgent({ ...payload, status: 'connected', creator_account_id: creator,
      applied_config_revision: payload.required_config_revision, browser: { capture: 'active', site_access: 'granted',
        history_permission: 'missing', legal_review_required: false, reported_at: when } });
    const settings: HistorySettings = { creator_account_id: creator, settings_revision: 1,
      consent_policy_version: 'history-consent-v1', consent_revision: null, authorized_platform_creator_id: null,
      desired_state: 'not_started', effective_state: 'not_applied', effective_config_revision: null,
      recent_window_days: 30, page_size: 50, pages_per_wake: 2, request_interval_ms: 1000, retry_limit: 3, updated_at: when };
    let settle!: (value: HistorySettings) => void;
    const update = vi.fn<HistorySettingsApi['update']>(() => new Promise(resolve => { settle = resolve; }));
    const open = vi.fn(async () => 'delivered' as const);
    protectedRoute = <MessageHistoryControls store={store} desktopApi={fixture.api}
      api={{ get: async () => settings, update, revoke: vi.fn() }} browserApi={{ open, setCapture: vi.fn() }} />;
    render(<App />);
    const consent = await screen.findByRole('checkbox', { name: 'I allow read-only syncing of my older messages to this computer.' });
    const allow = screen.getByRole('button', { name: 'Turn on message history' });
    expect(allow).toHaveProperty('disabled', true);
    fireEvent.click(consent);
    fireEvent.click(allow);
    expect(update).toHaveBeenCalledOnce();
    expire(socket);
    await screen.findByRole('button', { name: 'Sign in with passkey' });
    expect(update.mock.calls[0][2]?.aborted).toBe(true);
    await act(async () => settle({ ...settings, settings_revision: 2, consent_revision: 'synthetic-consent',
      authorized_platform_creator_id: creator, desired_state: 'running', effective_state: 'running' }));
    expect(open).not.toHaveBeenCalled();
    expect(update).toHaveBeenCalledOnce();
    expect(screen.queryByText('Application routes')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Allow browser access' })).toBeNull();
  });
});
