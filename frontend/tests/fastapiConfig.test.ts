import { afterEach, describe, expect, it, vi } from 'vitest';

import { getConfig } from '../src/config/fastapiConfig';

function injectConfig(value: Record<string, unknown>) {
  const element = document.createElement('script');
  element.id = 'fastapi-config';
  element.type = 'application/json';
  element.textContent = JSON.stringify(value);
  document.body.append(element);
}

afterEach(() => {
  document.getElementById('fastapi-config')?.remove();
  vi.restoreAllMocks();
});

describe('FastAPI runtime config credential boundary', () => {
  it('exposes the Bridge ticket and drops any injected Agent ticket field', () => {
    injectConfig({
      EXTENSION_ID: 'abcdefghijklmnopabcdefghijklmnop',
      FASTAPI_WS_URL: 'wss://brain.example/ws/bridge',
      API_BASE_URL: 'https://brain.example',
      VERSION: '2.0.0-beta.1',
      BRIDGE_ROLE: 'operator',
      USER_ID: 'creator-user',
      CREATOR_ID: 'creator-1',
      BRIDGE_AUTH_TICKET: 'bridge-session-ticket',
      AGENT_AUTH_TICKET: 'must-never-enter-runtime-config',
    });

    const config = getConfig();

    expect(config).toEqual({
      EXTENSION_ID: 'abcdefghijklmnopabcdefghijklmnop',
      FASTAPI_WS_URL: 'wss://brain.example/ws/bridge',
      API_BASE_URL: 'https://brain.example',
      VERSION: '2.0.0-beta.1',
      BRIDGE_ROLE: 'operator',
      USER_ID: 'creator-user',
      CREATOR_ID: 'creator-1',
      BRIDGE_AUTH_TICKET: 'bridge-session-ticket',
    });
    expect(config).not.toHaveProperty('AGENT_AUTH_TICKET');
  });

  it('does not synthesize either credential when server config is absent', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => undefined);

    const config = getConfig();

    expect(config.BRIDGE_AUTH_TICKET).toBeUndefined();
    expect(config).not.toHaveProperty('AGENT_AUTH_TICKET');
  });
});
