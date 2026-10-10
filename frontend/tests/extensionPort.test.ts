import { describe, expect, it, vi } from 'vitest';

import { createExtensionPort, EXTENSION_PORT_NAME, type RuntimeLike } from '../src/services/extensionPort';

const EXTENSION_ID = 'lfiompogjmmgnbkacdnikbfoihmlloda';

function fakeRuntime() {
  const ports: Array<{
    options: { name: string };
    sent: unknown[];
    deliver(message: unknown): void;
    drop(): void;
    disconnect: ReturnType<typeof vi.fn>;
  }> = [];
  const runtime: RuntimeLike = {
    connect(_id, options) {
      const message: Array<(value: unknown) => void> = [];
      const disconnect: Array<() => void> = [];
      const port = {
        options, sent: [] as unknown[],
        onMessage: { addListener: (fn: (value: unknown) => void) => message.push(fn) },
        onDisconnect: { addListener: (fn: () => void) => disconnect.push(fn) },
        postMessage(value: unknown) { this.sent.push(value); },
        disconnect: vi.fn(),
        deliver(value: unknown) { message.forEach((fn) => fn(value)); },
        drop() { disconnect.forEach((fn) => fn()); },
      };
      ports.push(port);
      return port;
    },
  };
  return { runtime, ports };
}

describe('extension port', () => {
  const journey = '11111111-1111-4111-8111-111111111111';
  const bridgeUrl = `http://bridge.localhost:17871/#journey=${journey}`;
  const handoffRequest = () => ({ type: 'navigate_setup', version: 1,
    request_id: '22222222-2222-4222-8222-222222222222', journey_id: journey, expected_url: bridgeUrl });
  it('navigates the requesting document once without opening a second tab', () => {
    const { runtime, ports } = fakeRuntime(), location = { href: bridgeUrl, replace: vi.fn() };
    const port = createExtensionPort({ runtime, extensionId: EXTENSION_ID, location });
    port.subscribe(() => {});
    ports[0].deliver({ type: 'state', version: 1, stage: 'needs_terms', attempt: null });
    expect(port.open('setup')).toBe(true);
    ports[0].deliver(handoffRequest()); ports[0].deliver(handoffRequest()); ports[0].drop();
    expect(location.replace).toHaveBeenCalledExactlyOnceWith(`chrome-extension://${EXTENSION_ID}/setup.html#journey=${journey}`);
    expect(ports).toHaveLength(1);
  });
  for (const change of ['unsolicited', 'extra-field', 'url', 'journey', 'invalid-id', 'different-page', 'old-port', 'other-step']) {
    it(`refuses a handoff for ${change}`, () => {
      const { runtime, ports } = fakeRuntime(), location = { href: bridgeUrl, replace: vi.fn() };
      const port = createExtensionPort({ runtime, extensionId: EXTENSION_ID, location });
      port.subscribe(() => {});
      ports[0].deliver({ type: 'state', version: 1, stage: 'needs_terms', attempt: null });
      if (change !== 'unsolicited') port.open(change === 'other-step' ? 'history' : 'setup');
      const request: Record<string, unknown> = handoffRequest();
      if (change === 'extra-field') request.url = 'https://example.com';
      if (change === 'url') request.expected_url = 'https://example.com';
      if (change === 'journey') request.journey_id = '33333333-3333-4333-8333-333333333333';
      if (change === 'invalid-id') request.request_id = 'invalid';
      if (change === 'different-page') location.href = 'https://onlyfans.com/my/chats';
      if (change === 'old-port') ports[0].drop();
      ports[0].deliver(request);
      expect(location.replace).not.toHaveBeenCalled();
    });
  }
  it('discovers a runtime installed after the page subscribed without a polling timer', () => {
    const installed = fakeRuntime();
    let runtime: RuntimeLike | undefined;
    const port = createExtensionPort({ resolveRuntime: () => runtime, extensionId: EXTENSION_ID });
    port.subscribe(() => {});
    expect(port.getState().status).toBe('absent');
    runtime = installed.runtime;
    port.retry();
    expect(installed.ports).toHaveLength(1);
    installed.ports[0].deliver({ type: 'state', version: 1, stage: 'needs_terms', attempt: null });
    expect(port.getState().status).toBe('connected');
  });

  it('is absent without an extension runtime or with an invalid extension ID', () => {
    expect(createExtensionPort({ runtime: undefined, extensionId: EXTENSION_ID }).getState().status).toBe('absent');
    const { runtime, ports } = fakeRuntime();
    const port = createExtensionPort({ runtime, extensionId: 'dev-extension-id' });
    port.subscribe(() => {});
    expect(port.getState().status).toBe('absent');
    expect(ports).toHaveLength(0);
  });

  it('connects on first subscriber and publishes only well-formed pushed state', () => {
    const { runtime, ports } = fakeRuntime();
    const port = createExtensionPort({ runtime, extensionId: EXTENSION_ID });
    const seen: string[] = [];
    port.subscribe(() => seen.push(`${port.getState().status}:${port.getState().stage}`));
    expect(ports[0].options).toEqual({ name: EXTENSION_PORT_NAME });
    ports[0].deliver({ type: 'state', version: 1, stage: 'needs_full', attempt: null, extra: true });
    ports[0].deliver({ type: 'state', version: 1, stage: 'paired', attempt: { state: 'compare', comparison_code: null } });
    expect(port.getState().status).toBe('connecting');
    ports[0].deliver({ type: 'state', version: 1, stage: 'needs_full', attempt: null });
    expect(seen).toEqual(['connected:needs_full']);
    expect(port.open('setup')).toBe(true);
    expect(port.pair()).toBe(true);
    expect(ports[0].sent).toEqual([{ type: 'open', version: 1, step: 'setup' }, { type: 'pair', version: 1 }]);
  });

  it('reconnects after an idle worker drop, becomes absent when nothing answers, and retries only on request', () => {
    const { runtime, ports } = fakeRuntime();
    const port = createExtensionPort({ runtime, extensionId: EXTENSION_ID });
    port.subscribe(() => {});
    ports[0].deliver({ type: 'state', version: 1, stage: 'paired', attempt: null });
    ports[0].drop();
    expect(ports).toHaveLength(2);
    ports[1].drop();
    expect(ports).toHaveLength(2);
    expect(port.getState().status).toBe('absent');
    expect(port.pair()).toBe(false);
    port.retry();
    expect(ports).toHaveLength(3);
    expect(port.getState().status).toBe('connecting');
  });

  it('keeps an ongoing pairing connected across an internal screen transition', () => {
    const { runtime, ports } = fakeRuntime();
    const port = createExtensionPort({ runtime, extensionId: EXTENSION_ID });
    const unsubscribe = port.subscribe(() => {});
    ports[0].deliver({ type: 'state', version: 1, stage: 'ready_to_pair', attempt: null });
    expect(port.pair()).toBe(true);
    unsubscribe();
    expect(ports[0].disconnect).not.toHaveBeenCalled();
    ports[0].deliver({ type: 'state', version: 1, stage: 'pairing', attempt: { state: 'compare', comparison_code: '012345' } });
    expect(ports[0].disconnect).not.toHaveBeenCalled();
    ports[0].deliver({ type: 'state', version: 1, stage: 'paired', attempt: { state: 'paired', comparison_code: null } });
    expect(ports[0].disconnect).toHaveBeenCalledOnce();
    expect(port.getState().status).toBe('connecting');
  });

  it('releases the held port if a pending connection fails', () => {
    const { runtime, ports } = fakeRuntime();
    const port = createExtensionPort({ runtime, extensionId: EXTENSION_ID });
    const unsubscribe = port.subscribe(() => {});
    ports[0].deliver({ type: 'state', version: 1, stage: 'ready_to_pair', attempt: null });
    expect(port.pair()).toBe(true);
    unsubscribe();
    ports[0].deliver({ type: 'state', version: 1, stage: 'ready_to_pair', attempt: { state: 'failed', comparison_code: null } });
    expect(ports[0].disconnect).toHaveBeenCalledOnce();
    expect(port.getState().status).toBe('connecting');
  });

  it('keeps page closure authoritative for an ongoing pairing', () => {
    const { runtime, ports } = fakeRuntime();
    const port = createExtensionPort({ runtime, extensionId: EXTENSION_ID });
    const unsubscribe = port.subscribe(() => {});
    ports[0].deliver({ type: 'state', version: 1, stage: 'ready_to_pair', attempt: null });
    expect(port.pair()).toBe(true);
    unsubscribe();
    ports[0].drop();
    expect(ports[0].disconnect).not.toHaveBeenCalled();
    expect(port.getState().status).toBe('connecting');
    expect(ports).toHaveLength(1);
  });

  it('closes the port when the last subscriber leaves', () => {
    const { runtime, ports } = fakeRuntime();
    const port = createExtensionPort({ runtime, extensionId: EXTENSION_ID });
    const unsubscribe = port.subscribe(() => {});
    unsubscribe();
    expect(ports[0].disconnect).toHaveBeenCalled();
  });
});
