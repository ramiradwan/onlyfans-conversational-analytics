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

  it('closes the port when the last subscriber leaves', () => {
    const { runtime, ports } = fakeRuntime();
    const port = createExtensionPort({ runtime, extensionId: EXTENSION_ID });
    const unsubscribe = port.subscribe(() => {});
    unsubscribe();
    expect(ports[0].disconnect).toHaveBeenCalled();
  });
});
