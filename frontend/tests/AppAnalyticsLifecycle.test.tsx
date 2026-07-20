import { cleanup, render } from '@testing-library/react';
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const lifecycle = vi.hoisted(() => ({
  analytics: {
    activate: vi.fn().mockResolvedValue(undefined),
    deactivate: vi.fn(),
  },
  websocket: {
    connect: vi.fn(),
    disconnect: vi.fn(),
  },
}));

vi.mock('../src/config/fastapiConfig', () => ({
  getConfig: () => ({
    API_BASE_URL: 'https://api.example.test',
    FASTAPI_WS_URL: 'wss://bridge.example.test/ws/bridge',
    BRIDGE_AUTH_TICKET: 'bridge-ticket',
    BRIDGE_ROLE: 'creator',
    CREATOR_ID: 'requested-account',
    EXTENSION_ID: 'dev-extension-id',
  }),
}));
vi.mock('../src/services/websocketService', () => ({ websocketService: lifecycle.websocket }));
vi.mock('../src/store/analyticsStore', () => ({ analyticsStoreActions: lifecycle.analytics }));
vi.mock('../src/routing/AppRouter', () => ({ AppRouter: () => <div>router</div> }));

import { App } from '../src/App';

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => cleanup());

describe('App analytics lifecycle', () => {
  it('connects the Bridge socket and activates the session-bound analytics store on mount', () => {
    render(<App />);
    expect(lifecycle.websocket.connect).toHaveBeenCalledWith(
      'wss://bridge.example.test/ws/bridge',
      'requested-account',
      'bridge-ticket',
    );
    expect(lifecycle.analytics.activate).toHaveBeenCalledTimes(1);
  });

  it('disconnects and deactivates analytics on unmount', () => {
    const view = render(<App />);
    view.unmount();
    expect(lifecycle.websocket.disconnect).toHaveBeenCalledTimes(1);
    expect(lifecycle.analytics.deactivate).toHaveBeenCalledTimes(1);
  });

  it('is StrictMode-safe and cleans up analytics before the socket disconnects', () => {
    const calls: string[] = [];
    lifecycle.analytics.deactivate.mockImplementation(() => calls.push('deactivate'));
    lifecycle.websocket.disconnect.mockImplementation(() => calls.push('disconnect'));
    const view = render(<React.StrictMode><App /></React.StrictMode>);
    expect(lifecycle.websocket.connect).toHaveBeenCalledTimes(2);

    view.unmount();
    expect(calls.slice(-2)).toEqual(['disconnect', 'deactivate']);
  });
});
