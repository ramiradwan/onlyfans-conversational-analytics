import { afterEach, describe, expect, it, vi } from 'vitest';

// eslint-disable-next-line import/extensions -- Runtime .mjs has a matching .d.mts declaration.
import { createOnboardingClient } from '../../shared/onboarding/client.mjs';
import vectors from '../../shared/onboarding/vectors.json';
import { validateOnboardingMessage } from '../src/protocol/onboarding';
import { createOnboardingExtensionAdapter, ONBOARDING_PORT_NAME } from '../src/services/onboardingExtension';

const fixture = (id: string) => structuredClone(vectors.cases.find((value) => value.id === id)!.value);
const snapshot = fixture('extension-snapshot');
const capability = { type: 'capabilities', capabilities: ['local-onboarding.v1', 'persistent-workspace.v1', 'local-onboarding.command-result.v2'] };
const id = 'a'.repeat(32);
const tick = async () => { await Promise.resolve(); await Promise.resolve(); };
function setup() {
  let receive = (_: unknown) => {};
  let disconnect = () => {};
  const port = { onMessage: { addListener: (listener: typeof receive) => { receive = listener; } },
    onDisconnect: { addListener: (listener: typeof disconnect) => { disconnect = listener; } },
    postMessage: vi.fn(), disconnect: vi.fn(() => disconnect()) };
  const runtime = { connect: vi.fn(() => port) };
  const adapter = createOnboardingExtensionAdapter({ runtime, extensionId: id });
  const client = createOnboardingClient({ journeyId: snapshot.journey_id!, validate: validateOnboardingMessage });
  client.attach('extension', adapter);
  return { client, port, runtime, receive: (value: unknown) => receive(value), disconnect: () => disconnect() };
}
afterEach(() => vi.useRealTimers());

describe('extension onboarding authenticated port adapter', () => {
  it('negotiates on the exact extension before accepting the subscribed snapshot', async () => {
    const run = setup();
    expect(run.runtime.connect).toHaveBeenCalledWith(id, { name: ONBOARDING_PORT_NAME });
    run.receive(snapshot);
    await tick();
    expect(run.client.getState().sources.extension).toBeUndefined();
    run.receive(capability);
    expect(run.port.postMessage).toHaveBeenCalledWith({ type: 'snapshot' });
    run.receive(snapshot);
    await tick();
    expect(run.client.getState().sources.extension?.certain).toBe(true);
  });
  it('ignores another owner and clears readiness on disconnect', async () => {
    const run = setup(); run.receive(capability); run.receive(snapshot); await tick();
    run.receive({ ...snapshot, source: 'brain', revision: 1 });
    expect(run.client.getState().sources.extension?.revision).toBe(0);
    run.disconnect();
    expect(run.client.getState().sources.extension?.certain).toBe(false);
  });
  it('fails bounded negotiation without falling back to a legacy capability', async () => {
    vi.useFakeTimers();
    const run = setup();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(run.port.disconnect).toHaveBeenCalledTimes(1);
    expect(run.client.getState().sources.extension).toBeUndefined();
    expect(run.runtime.connect).toHaveBeenCalledTimes(1);
  });
  it('does not send commands until a valid owner snapshot is present', async () => {
    const run = setup(); run.receive(capability);
    const command = fixture('extension-command');
    // Fixture parsing is tested independently by the canonical validator.
    expect(await run.client.command(command as Parameters<typeof run.client.command>[0])).toBe(false);
    expect(run.port.postMessage.mock.calls.every(([value]) => value.type === 'snapshot')).toBe(true);
    run.disconnect(); await tick();
  });
  it('binds mutations to the authenticated worker epoch', async () => {
    const run = setup(); run.receive(capability); run.receive(snapshot); await tick();
    const command = fixture('extension-command');
    expect(await run.client.command(command as Parameters<typeof run.client.command>[0])).toBe(true);
    expect(run.port.postMessage).toHaveBeenCalledWith({ type: 'command', epoch: snapshot.epoch, command });
    run.disconnect();
  });
});
