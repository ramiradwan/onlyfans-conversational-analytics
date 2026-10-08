import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

// eslint-disable-next-line import/extensions -- Runtime .mjs has a matching .d.mts declaration.
import { createOnboardingClient, type OnboardingClient, type OnboardingState } from '../../shared/onboarding/client.mjs';
import vectors from '../../shared/onboarding/vectors.json';
import { BrowserExtensionControls } from '../src/components/BrowserExtensionControls';
import { CompanionPairingControls } from '../src/components/CompanionPairingControls';
import { CommercialActivationControls } from '../src/components/CommercialActivationControls';
import { validateOnboardingMessage } from '../src/protocol/onboarding';
import type { CompanionPairingApi } from '../src/services/companionPairingApi';
import type { ExtensionPort } from '../src/services/extensionPort';
import { bridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';

const active = vi.hoisted(() => ({ client: null as OnboardingClient | null }));
vi.mock('../src/services/onboardingSession', () => ({
  currentOnboardingClient: () => active.client,
  onboardingView: () => active.client?.getState() ?? null,
  subscribeOnboarding: (listener: () => void) => active.client?.subscribe(listener) ?? (() => {}),
}));
const fixture = (id: string) => structuredClone(vectors.cases.find((value) => value.id === id)!.value);
const state = fixture('extension-snapshot') as OnboardingState;
const port: ExtensionPort = {
  getState: () => portState, subscribe: () => () => {},
  open: vi.fn(() => true), pair: vi.fn(() => true), cancel: vi.fn(() => true), retry: vi.fn(),
};
const portState = { status: 'connected' as const, stage: 'ready_to_pair' as const, attempt: null };
function client() {
  const value = createOnboardingClient({ journeyId: state.journey_id, validate: validateOnboardingMessage });
  active.client = value;
  return value;
}
async function attach(value: OnboardingClient, snapshot: OnboardingState) {
  let receive = (_: unknown) => {};
  const send = vi.fn();
  await act(async () => value.attach(snapshot.source, {
    subscribe(next) { receive = next; return () => {}; },
    async readSnapshot() { return snapshot; }, sendCommand: send,
  }));
  return { send, receive: (message: unknown) => act(() => receive(message)) };
}
afterEach(() => { cleanup(); active.client = null; bridgeTransportStore.reset(); useUserStore.getState().actions.setUserRole(null); vi.clearAllMocks(); });

describe('owner-driven onboarding controls', () => {
  it('allows an explicit retry after an owner commit aborts a pending activation', async () => {
    const value = client();
    const brain = fixture('brain-snapshot') as OnboardingState;
    const owner = await attach(value, brain);
    let rejectPending!: (reason: unknown) => void;
    const redeem = vi.fn()
      .mockImplementationOnce(() => new Promise((_, reject) => { rejectPending = reject; }))
      .mockResolvedValue({ state: 'checking' as const });
    const api = {
      readiness: vi.fn(async () => ({ schema: 'ofca-analysis-readiness/v1' as const,
        commercial_authority: 'required' as const, analysis_admission: 'blocked' as const })),
      redeem,
    };
    render(<ThemeProvider theme={theme}><CommercialActivationControls api={api} /></ThemeProvider>);
    fireEvent.click(await screen.findByRole('button', { name: 'Turn on full analytics' }));
    fireEvent.change(screen.getByLabelText('Activation code'), { target: { value: `clr1.${'A'.repeat(43)}` } });
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Activate' })));
    expect(redeem).toHaveBeenCalledTimes(1);
    const pendingSignal = redeem.mock.calls[0][1] as AbortSignal;

    await act(async () => owner.receive({ ...brain, kind: 'event', revision: brain.revision + 1 }));
    expect(pendingSignal.aborted).toBe(true);
    expect(api.readiness).toHaveBeenCalledTimes(2);
    await act(async () => rejectPending(new DOMException('Aborted', 'AbortError')));
    expect(redeem).toHaveBeenCalledTimes(1);
    expect((screen.getByRole('button', { name: 'Activate' }) as HTMLButtonElement).disabled).toBe(false);

    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Activate' })));
    expect(redeem).toHaveBeenCalledTimes(2);
    expect(api.readiness).toHaveBeenCalledTimes(3);
  });

  it('refreshes activation on owner commits and removes certainty when the owner disconnects', async () => {
    const value = client();
    const brain = fixture('brain-snapshot') as OnboardingState;
    const owner = await attach(value, brain);
    const api = {
      readiness: vi.fn(async () => ({ schema: 'ofca-analysis-readiness/v1' as const,
        commercial_authority: 'active' as const, analysis_admission: 'blocked' as const })),
      redeem: vi.fn(async () => ({ state: 'checking' as const })),
    };
    render(<ThemeProvider theme={theme}><CommercialActivationControls api={api} /></ThemeProvider>);
    await act(async () => {});
    expect(api.readiness).toHaveBeenCalledTimes(1);
    expect(screen.getByText("New messages aren't being analyzed")).toBeTruthy();
    await act(async () => owner.receive({ ...brain, kind: 'event', revision: brain.revision + 1 }));
    expect(api.readiness).toHaveBeenCalledTimes(2);
    await act(async () => value.disconnect('brain'));
    expect(api.readiness).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("New messages aren't being analyzed")).toBeNull();
    expect(screen.getByText("Activation couldn't be checked.")).toBeTruthy();
  });

  it('keeps an unknown pause correlated until its exact committed receipt arrives', async () => {
    const value = client(); const owner = await attach(value, state);
    const legacy = { setCapture: vi.fn(async () => 'delivered' as const) };
    render(<ThemeProvider theme={theme}><BrowserExtensionControls api={legacy} browser={null}
      canManage connection="connected" port={port} /></ThemeProvider>);
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Pause collecting' })));
    expect(legacy.setCapture).not.toHaveBeenCalled();
    const [command, epoch] = owner.send.mock.calls[0];
    expect(epoch).toBe(state.epoch);
    owner.receive({ ...fixture('result-v2-generation-changing-commit'), operation_id: command.operation_id,
      revision: 0, consent_generation: 2, status: 'unknown', reason: 'unconfirmed' });
    expect(screen.getByRole('alert').textContent).toContain("didn't confirm");
    owner.receive({ ...state, kind: 'event', revision: 1, consent_generation: 3,
      facts: { ...state.facts, capture: 'paused' }, reason: 'paused' });
    expect(screen.getByRole('alert').textContent).toContain("didn't confirm");
    owner.receive({ ...fixture('result-v2-generation-changing-commit'), operation_id: command.operation_id, revision: 1 });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByRole('button', { name: 'Resume collecting' })).toBeTruthy();
  });

  it('starts one fresh pairing only after both authoritative owners meet the prerequisites', async () => {
    const value = client();
    const brain = fixture('brain-snapshot') as OnboardingState;
    const extension = await attach(value, { ...state, facts: { ...state.facts, mode: 'full', consent: 'missing' } });
    await attach(value, { ...brain, facts: { ...brain.facts, pairing: 'missing' } });
    useUserStore.getState().actions.setUserRole('operator');
    bridgeTransportStore.bindAccount('creator-1');
    bridgeTransportStore.setConnection('connected');
    const api = { pins: vi.fn(async () => []), open: vi.fn(async () => ({
      pairing_id: 'A'.repeat(43), creator_account_id: 'creator-1', generation: 1, version: 0,
      state: 'open', expires_at: new Date(Date.now() + 300_000).toISOString(), comparison_code: null,
      agent_identity_thumbprint: null,
    })), change: vi.fn(async () => ({})), get: vi.fn(), revoke: vi.fn(), confirmVerified: vi.fn() } as unknown as CompanionPairingApi;
    render(<ThemeProvider theme={theme}><CompanionPairingControls api={api} port={port} /></ThemeProvider>);
    await act(async () => {});
    expect(api.open).not.toHaveBeenCalled();
    await act(async () => extension.receive({ ...state, kind: 'event', revision: 1,
      facts: { ...state.facts, mode: 'full' } }));
    expect(api.open).toHaveBeenCalledTimes(1);
    expect(port.pair).toHaveBeenCalledTimes(1);
    extension.receive({ ...state, kind: 'event', revision: 2, facts: { ...state.facts, mode: 'full' } });
    expect(api.open).toHaveBeenCalledTimes(1);
  });
});
