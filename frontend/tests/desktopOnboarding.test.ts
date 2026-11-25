import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { DesktopInteractionOutcome, DesktopState } from '../onboarding/client.js';
import { desktopFixture } from './desktopOnboardingFixture';

const mock = vi.hoisted(() => ({ construct: vi.fn() }));
vi.mock('../onboarding/client.js', () => ({ DesktopSetupClient: class {
  constructor(options: unknown) { return mock.construct(options); }
} }));

beforeEach(() => { vi.resetModules(); sessionStorage.clear(); mock.construct.mockReset(); });
afterEach(() => { sessionStorage.clear(); });

async function setup() {
  let publish!: (state: DesktopState) => void;
  const current = desktopFixture().api.getState();
  const accepted: DesktopInteractionOutcome = { state: 'accepted', operationId: null, reason: null };
  const client = { connect: vi.fn(async () => publish(current)), stop: vi.fn(), act: vi.fn(async () => accepted),
    launchBrowser: vi.fn(async () => accepted), reconcile: vi.fn(async () => accepted) };
  mock.construct.mockImplementation((options: { receive(state: DesktopState): void }) => { publish = options.receive; return client; });
  const { desktopOnboardingApi: api } = await import('../src/services/desktopOnboarding');
  return { api, client, current, publish: (state: DesktopState) => publish(state) };
}

describe('native coordinator adapter', () => {
  it('withholds readiness across account changes, disconnect and unauthorized views', async () => {
    const { selectAnalysisReadiness } = await import('../src/services/desktopOnboarding');
    const state = desktopFixture().api.getState();
    expect(selectAnalysisReadiness(state, 'creator-account').value?.commercial_authority).toBe('required');
    expect(selectAnalysisReadiness(state, 'another-account').value).toBeNull();
    expect(selectAnalysisReadiness({ ...state, connected: false }).value).toBeNull();
    expect(selectAnalysisReadiness({ ...state, snapshot: { ...state.snapshot!, analysis_readiness: {
      availability: 'unauthorized', creator_account_id: null, value: null } } }).value).toBeNull();
  });
  it('does not discover a replacement presentation or issue work without its consumed entry locator', async () => {
    const h = await setup(), unsubscribe = h.api.subscribe(vi.fn());
    expect(h.api.getState().error).toBe('session_unavailable');
    expect(mock.construct).not.toHaveBeenCalled();
    await expect(h.api.begin('activation.review')).rejects.toThrow('session_unavailable');
    unsubscribe();
  });

  it('subscribes once and selects the exact activation operation among other pending work', async () => {
    const h = await setup();
    sessionStorage.setItem('desktop-presentation', h.current.snapshot!.generation);
    const first = h.api.subscribe(vi.fn()), second = h.api.subscribe(vi.fn());
    expect(mock.construct).toHaveBeenCalledTimes(1);
    expect(h.client.act).not.toHaveBeenCalled();
    const pairing = { operation_id: '33333333-3333-4333-8333-333333333333', action: 'browser.pair', state: 'waiting', reason: 'none' };
    const activation = { ...pairing, operation_id: '44444444-4444-4444-8444-444444444444', action: 'activation.review' };
    h.publish({ ...h.current, pending: pairing.operation_id,
      snapshot: { ...h.current.snapshot!, allowed_actions: [], pending_operations: [pairing, activation] } });
    await h.api.begin('activation.review');
    expect(h.client.launchBrowser).toHaveBeenCalledExactlyOnceWith(true, activation.operation_id);
    await h.api.reconcile('activation.review');
    expect(h.client.reconcile).toHaveBeenCalledExactlyOnceWith(activation.operation_id);
    expect(h.client.act).not.toHaveBeenCalled();
    first(); expect(h.client.stop).not.toHaveBeenCalled(); second(); expect(h.client.stop).toHaveBeenCalledOnce();
    h.publish(h.current);
    expect(h.api.getState().connected).toBe(false);
  });

  it('reports uncertain browser delivery without automatically trying another action', async () => {
    const h = await setup();
    sessionStorage.setItem('desktop-presentation', h.current.snapshot!.generation);
    const unsubscribe = h.api.subscribe(vi.fn());
    const outcome: DesktopInteractionOutcome = { state: 'unconfirmed', operationId: 'activation', reason: 'browser_unavailable' };
    h.client.act.mockResolvedValue(outcome);
    h.publish({ ...h.current, error: 'profile_mismatch' });
    await expect(h.api.begin('activation.review')).resolves.toEqual(outcome);
    expect(h.client.act).toHaveBeenCalledExactlyOnceWith('activation.review', {});
    expect(h.client.launchBrowser).not.toHaveBeenCalled();
    unsubscribe();
  });

  it('switches only the current authentication while its original operation is pending', async () => {
    const h = await setup();
    sessionStorage.setItem('desktop-presentation', h.current.snapshot!.generation);
    const unsubscribe = h.api.subscribe(vi.fn());
    const parent = '33333333-3333-4333-8333-333333333333';
    const child = '44444444-4444-4444-8444-444444444444';
    const current: DesktopState = { ...h.current, pending: child, snapshot: { ...h.current.snapshot!,
      allowed_actions: ['auth.switch'],
      pending_operations: [{ operation_id: child, action: 'auth.select', state: 'waiting', reason: 'none' }],
      authentication: { operation_id: parent, browser_operation_id: child, state: 'admitted',
        choices: { google: true, 'microsoft-personal': true, telegram: false, tiktok: false, default: true },
        actions: { select: false, cancel: false, open_browser: true, switch_account: true },
        authentication_attempt: { authentication_attempt_id: '55555555-5555-7555-8555-555555555555',
          provider: 'google', mode: 'sign-in' } },
    } };
    h.publish(current);
    await h.api.begin('auth.switch', { provider: 'microsoft-personal' });
    expect(h.client.act).toHaveBeenCalledExactlyOnceWith('auth.switch', { provider: 'microsoft-personal' });
    h.publish({ ...current, pending: '66666666-6666-4666-8666-666666666666' });
    await expect(h.api.begin('auth.switch', { provider: 'default' })).rejects.toThrow('operation_pending');
    expect(h.client.act).toHaveBeenCalledTimes(1);
    unsubscribe();
  });
});

it('admits presentation intent only from the current account, journey and legal state', async () => {
  const { selectFullPresentationIntent } = await import('../src/services/desktopOnboarding');
  const state = desktopFixture().api.getState();
  const intent = { request_id: '77777777-7777-4777-8777-777777777777', journey_id: state.snapshot!.journey_id,
    disclosure_bundle_id: 'a'.repeat(64) };
  const browser = { capture: 'active' as const, site_access: 'granted' as const, history_permission: 'missing' as const,
    legal_review_required: false, reported_at: '2026-11-18T00:00:00Z', full_intent: intent };
  expect(selectFullPresentationIntent(state, 'creator-account', browser)).toEqual(intent);
  expect(selectFullPresentationIntent(state, 'other-creator', browser)).toBeUndefined();
  expect(selectFullPresentationIntent({ ...state, connected: false }, 'creator-account', browser)).toBeUndefined();
  expect(selectFullPresentationIntent(state, 'creator-account', { ...browser, legal_review_required: true })).toBeUndefined();
  expect(selectFullPresentationIntent(state, 'creator-account', { ...browser, full_intent: { ...intent, journey_id: 'other' } })).toBeUndefined();
  expect(selectFullPresentationIntent(state, 'creator-account', { ...browser, capture: 'off' })).toBeUndefined();
  expect(selectFullPresentationIntent({ ...state, snapshot: { ...state.snapshot!, allowed_actions: [] } }, 'creator-account', browser)).toBeUndefined();
});
