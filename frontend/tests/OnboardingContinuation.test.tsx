import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

// eslint-disable-next-line import/extensions -- Runtime .mjs has a matching .d.mts declaration.
import { createOnboardingClient, type OnboardingClient, type OnboardingState } from '../../shared/onboarding/client.mjs';
import vectors from '../../shared/onboarding/vectors.json';
import { OnboardingContinuation } from '../src/components/OnboardingContinuation';
import { validateOnboardingMessage } from '../src/protocol/onboarding';

const active = vi.hoisted(() => ({ client: null as OnboardingClient | null }));
vi.mock('../src/services/onboardingSession', () => ({
  journeyFromHash: (hash: string) => hash.startsWith('#journey=') ? hash.slice(9) : null,
  onboardingView: () => active.client?.getState() ?? null,
  subscribeOnboarding: (listener: () => void) => active.client?.subscribe(listener) ?? (() => {}),
}));
vi.mock('../src/components/CompanionPairingControls', () => ({ CompanionPairingControls: () => <div>Pairing control</div> }));
vi.mock('../src/components/CommercialActivationControls', () => ({ CommercialActivationControls: () => <div>Activation control</div> }));
const brain = vectors.cases.find((value) => value.id === 'brain-snapshot')!.value as OnboardingState;
afterEach(() => { cleanup(); active.client = null; window.history.replaceState({}, '', '/'); });

it('normal app access does not reopen setup', () => {
  render(<OnboardingContinuation><div>Analytics</div></OnboardingContinuation>);
  expect(screen.getByText('Analytics')).toBeTruthy();
  expect(screen.queryByText('Pairing control')).toBeNull();
});

it('a new journey cannot inherit completion from the previous active client', async () => {
  window.history.replaceState({}, '', '/#journey=22222222-2222-4222-8222-222222222222');
  const client = createOnboardingClient({ journeyId: brain.journey_id, validate: validateOnboardingMessage });
  active.client = client;
  await client.attach('brain', {
    subscribe() { return () => {}; },
    readSnapshot: async () => ({ ...brain, facts: {
      installation: 'verified', enrollment: 'verified', pairing: 'verified', activation: 'verified', analysis: 'verified',
    } }),
    sendCommand() { throw Error('read only'); },
  });
  render(<OnboardingContinuation activationReturn={{ state: 'unconfirmed', check: vi.fn() }}><div>Analytics</div></OnboardingContinuation>);
  expect(screen.getByText('Pairing control')).toBeTruthy();
  expect(screen.getByText('Activation couldn’t be confirmed.')).toBeTruthy();
  expect(screen.queryByText('Analytics')).toBeNull();
});

it('the returning workspace mounts remaining controls without a Settings action and waits for owner completion', async () => {
  window.history.replaceState({}, '', `/#journey=${brain.journey_id}`);
  const client = createOnboardingClient({ journeyId: brain.journey_id, validate: validateOnboardingMessage });
  active.client = client;
  let push: (value: unknown) => void = () => {};
  await client.attach('brain', {
    subscribe(receive) { push = receive; return () => {}; },
    readSnapshot: async () => ({ ...brain, facts: { ...brain.facts, pairing: 'missing' } }),
    sendCommand() { throw Error('read only'); },
  });
  render(<OnboardingContinuation><div>Analytics</div></OnboardingContinuation>);
  expect(screen.getByText('Pairing control')).toBeTruthy();
  expect(screen.queryByText('Analytics')).toBeNull();
  await act(async () => push({ ...brain, kind: 'event', revision: brain.revision + 1,
    facts: { installation: 'verified', enrollment: 'verified', pairing: 'verified', activation: 'verified', analysis: 'missing' } }));
  expect(screen.getByText('Analytics')).toBeTruthy();
  expect(screen.queryByText('Pairing control')).toBeNull();
  await act(async () => client.disconnect('brain'));
  expect(screen.getByText('Analytics')).toBeTruthy();
});
