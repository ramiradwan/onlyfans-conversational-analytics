import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { CommercialActivationControls } from '../src/components/CommercialActivationControls';
import { CapabilityLicenseApiError, type CapabilityLicenseApi, type CapabilityLicenseReadiness } from '../src/services/capabilityLicenseApi';
import { theme } from '../src/theme';
import { desktopFixture } from './desktopOnboardingFixture';
import type { DesktopAuthentication, DesktopDecision } from '../onboarding/client.js';
import type { DesktopOnboardingApi } from '../src/services/desktopOnboarding';

const required: CapabilityLicenseReadiness = { schema: 'ofca-analysis-readiness/v1', commercial_authority: 'required', analysis_admission: 'blocked' };
const active: CapabilityLicenseReadiness = { schema: 'ofca-analysis-readiness/v1', commercial_authority: 'active', analysis_admission: 'admitted' };
const decision: DesktopDecision = { operation_id: '11111111-1111-4111-8111-111111111111', action: 'activate-complimentary', expires_at: '2030-01-01T12:00:00Z', offer: {
  description: 'Full conversation analytics', offer_version_id: 'test-offer', funding: 'complimentary-pilot', pilot_credit_cost: 1,
  capability: 'analysis-run', licensed_major_version: 2, compatible_artifact_family: 'analysis-artifact', update_rights: true, fallback_major_versions: [1],
} };
const authentication: DesktopAuthentication = { operation_id: decision.operation_id, browser_operation_id: '22222222-2222-4222-8222-222222222222',
  state: 'selection-required', choices: { google: true, 'microsoft-personal': true, default: true },
  actions: { select: true, cancel: true, open_browser: false, switch_account: false }, authentication_attempt: null };
type TestApi = CapabilityLicenseApi & { readiness: ReturnType<typeof vi.fn> };
function makeApi(overrides: Partial<TestApi> = {}): TestApi {
  return { readiness: vi.fn(async () => required), redeem: vi.fn(async () => ({ state: 'checking' as const })), ...overrides };
}
function mount(api: CapabilityLicenseApi, onboarding: DesktopOnboardingApi = desktopFixture().api, embedded = false) {
  return render(<ThemeProvider theme={theme}><CommercialActivationControls api={api} onboarding={onboarding} embedded={embedded} /></ThemeProvider>);
}
function withDecision() {
  const fixture = desktopFixture();
  fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!, decision, allowed_actions: ['activation.confirm'] } });
  return fixture;
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

it('keeps offer review in a modal and closing it does not accept or reopen it on passive updates', async () => {
  const fixture = withDecision();
  mount(makeApi(), fixture.api);
  expect(await screen.findByRole('dialog', { name: 'Full conversation analytics' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Not now' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  await act(async () => fixture.publish());
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(fixture.api.begin).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Review activation offer' }));
  expect(screen.getByRole('dialog', { name: 'Full conversation analytics' })).toBeTruthy();
});

it('shows a refused activation result after its operation leaves the pending list', async () => {
  const fixture = desktopFixture();
  fixture.publish({ error: 'activation_unavailable' });
  mount(makeApi(), fixture.api);
  expect(await screen.findByText('Full analytics activation could not be prepared. Try again.')).toBeTruthy();
  expect(fixture.api.begin).not.toHaveBeenCalled();
});

it('observes an unavailable offer on the same operation and presents a later decision without another begin', async () => {
  const fixture = desktopFixture();
  const pending = { operation_id: decision.operation_id, action: 'activation.review', state: 'waiting', reason: 'activation_unavailable' };
  fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!,
    decision_status: { operation_id: decision.operation_id, state: 'unavailable' }, pending_operations: [pending] } });
  mount(makeApi(), fixture.api);
  expect(await screen.findByText('Checking the activation offer.')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Set up Full analytics' })).toBeNull();
  await act(async () => fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!, decision,
    decision_status: { operation_id: decision.operation_id, state: 'confirmation-required' }, allowed_actions: ['activation.confirm'] } }));
  expect(screen.getByRole('dialog', { name: 'Full conversation analytics' })).toBeTruthy();
  expect(fixture.api.begin).not.toHaveBeenCalled();
});

it('does not describe an unknown activation outcome as ongoing offer observation', async () => {
  const fixture = desktopFixture();
  fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!,
    decision_status: { operation_id: decision.operation_id, state: 'unavailable' },
    pending_operations: [{ operation_id: decision.operation_id, action: 'activation.review', state: 'outcome_unknown', reason: 'operation_unconfirmed' }] } });
  mount(makeApi(), fixture.api);
  expect(await screen.findByText('Activation has not been confirmed.')).toBeTruthy();
  expect(screen.queryByText('Checking the activation offer.')).toBeNull();
  expect(fixture.api.begin).not.toHaveBeenCalled();
});

it('receives late owner readiness without polling or replacing accepted work', async () => {
  vi.useFakeTimers();
  const fixture = desktopFixture();
  fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!, decision_status: { operation_id: decision.operation_id, state: 'accepted' },
    pending_operations: [{ operation_id: decision.operation_id, action: 'activation.review', state: 'waiting', reason: 'none' }] } });
  const readiness = vi.fn().mockResolvedValue(required);
  mount(makeApi({ readiness }), fixture.api);
  await act(async () => { await vi.advanceTimersByTimeAsync(90_000); });
  expect(readiness).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: 'Check activation status' })).toBeNull();
  await act(async () => fixture.readiness(active));
  expect(screen.getByText('On')).toBeTruthy();
  expect(fixture.api.begin).not.toHaveBeenCalled();
});

describe('native commercial activation', () => {
  it('opens account-specific activation directly without an extra code-entry step', async () => {
    const fixture = desktopFixture();
    const begin = vi.fn(() => new Promise<void>(() => {}));
    const api = makeApi();
    mount(api, { ...fixture.api, begin });
    const button = await screen.findByRole('button', { name: 'Set up Full analytics' });
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    expect(begin).not.toHaveBeenCalled();
    fireEvent.click(button); fireEvent.click(button);
    expect(begin).toHaveBeenCalledExactlyOnceWith('activation.review');
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByLabelText('Activation code')).toBeNull();
    expect(api.redeem).not.toHaveBeenCalled();
  });

  it.each([false, true])('reviews the actual offer inline and requires one explicit consent click (embedded=%s)', async embedded => {
    const fixture = withDecision();
    const begin = vi.fn(() => new Promise<void>(() => {}));
    const api = makeApi();
    mount(api, { ...fixture.api, begin }, embedded);
    expect(await screen.findByText('Full conversation analytics')).toBeTruthy();
    expect(screen.getByText('Price')).toBeTruthy();
    expect(screen.getByText('Free')).toBeTruthy();
    expect(screen.getByText('Uses 1 pilot credit')).toBeTruthy();
    expect(screen.getByText('2.x')).toBeTruthy();
    expect(screen.getByText('1.x')).toBeTruthy();
    expect(screen.getByText('Included')).toBeTruthy();
    expect(begin).not.toHaveBeenCalled();
    const button = screen.getByRole('button', { name: 'Activate for free' });
    fireEvent.click(button); fireEvent.click(button);
    expect(begin).toHaveBeenCalledExactlyOnceWith('activation.confirm');
    expect(api.redeem).not.toHaveBeenCalled();
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it('never equates accepted activation with canonical Full readiness', async () => {
    const fixture = withDecision();
    const readiness = vi.fn().mockResolvedValueOnce(required).mockResolvedValueOnce(required).mockResolvedValue(active);
    mount(makeApi({ readiness }), fixture.api);
    await screen.findByText('Off');
    await act(async () => fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!, revision: fixture.api.getState().snapshot!.revision + 1, decision: null,
      decision_status: { operation_id: decision.operation_id, state: 'accepted' }, allowed_actions: [],
      pending_operations: [{ operation_id: decision.operation_id, action: 'activation.review', state: 'accepted', reason: 'none' }] } }));
    expect(await screen.findByText('Activation accepted. Checking Full analytics…')).toBeTruthy();
    expect(screen.queryByText('On')).toBeNull();
    await act(async () => fixture.readiness(active));
    expect(await screen.findByText('On')).toBeTruthy();
    expect(screen.queryByText('Activation accepted. Checking Full analytics…')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Check activation status' })).toBeNull();
  });

  it('offers configured providers and sends the selected provider only after a click', async () => {
    const fixture = desktopFixture();
    fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!, authentication: { ...authentication,
      choices: { google: true, 'microsoft-personal': false, default: true } }, allowed_actions: ['auth.select', 'auth.cancel'] } });
    mount(makeApi(), fixture.api);
    expect(await screen.findByRole('button', { name: 'Continue with Google' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Continue with Microsoft' })).toBeNull();
    expect(fixture.api.begin).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Continue with Google' }));
    await waitFor(() => expect(fixture.api.begin).toHaveBeenCalledExactlyOnceWith('auth.select', { provider: 'google' }));
  });

  it('chooses another account explicitly without authorizing activation', async () => {
    const fixture = withDecision();
    fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!, authentication: { ...authentication, state: 'admitted',
      actions: { select: false, cancel: true, open_browser: false, switch_account: true } }, allowed_actions: ['activation.confirm', 'auth.switch', 'auth.cancel'] } });
    mount(makeApi(), fixture.api);
    expect(screen.queryByRole('button', { name: 'Cancel sign-in' })).toBeNull();
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in to Full analytics with another account' }));
    expect(fixture.api.begin).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Activate for free' })).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Continue with Google' }));
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Sign in to Full analytics with another account' }));
    fireEvent.click(screen.getByRole('button', { name: 'Sign in to Full analytics with another account' }));
    fireEvent.click(screen.getByRole('button', { name: 'Continue with Microsoft' }));
    await waitFor(() => expect(fixture.api.begin).toHaveBeenCalledExactlyOnceWith('auth.switch', { provider: 'microsoft-personal' }));
  });

  it('waits for Chrome without redundant launch or cancellation controls', async () => {
    const fixture = desktopFixture();
    fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!, authentication: { ...authentication, state: 'selected',
      actions: { select: false, cancel: true, open_browser: true, switch_account: true } }, allowed_actions: ['auth.switch', 'auth.cancel'] } });
    mount(makeApi(), fixture.api);
    expect(await screen.findByText('Finish signing in in Chrome.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Open Chrome' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Cancel sign-in' })).toBeNull();
    expect(fixture.api.openBrowser).not.toHaveBeenCalled();
    expect(fixture.api.begin).not.toHaveBeenCalled();
  });
  it.each(['browser_unavailable', 'chrome_unavailable'])('shows an asynchronous %s launch failure and retries the same browser operation', async error => {
    const fixture = desktopFixture();
    fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!, authentication: { ...authentication, state: 'selected',
      actions: { select: false, cancel: true, open_browser: true, switch_account: false } }, allowed_actions: [] } });
    mount(makeApi(), fixture.api);
    expect(await screen.findByText('Finish signing in in Chrome.')).toBeTruthy();
    await act(async () => fixture.publish({ error }));
    expect(screen.queryByText('Finish signing in in Chrome.')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(fixture.api.openBrowser).toHaveBeenCalledExactlyOnceWith(authentication.browser_operation_id));
    expect(fixture.api.begin).not.toHaveBeenCalled();
  });

  it('suppresses stale confirmation and auth controls once accepted', async () => {
    const fixture = withDecision();
    fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!, authentication,
      decision_status: { operation_id: decision.operation_id, state: 'accepted' }, allowed_actions: ['activation.confirm', 'auth.select', 'auth.switch', 'auth.cancel'] } });
    mount(makeApi(), fixture.api);
    expect(await screen.findByText('Activation accepted. Checking Full analytics…')).toBeTruthy();
    expect(screen.queryByRole('group', { name: 'Sign-in options' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Activate for free' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Cancel sign-in' })).toBeNull();
    expect(fixture.api.begin).not.toHaveBeenCalled();
  });

  it('retries missing offer preparation through the admitted original browser task', async () => {
    const fixture = desktopFixture();
    fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!, authentication: { ...authentication, state: 'admitted',
      actions: { select: false, cancel: false, open_browser: true, switch_account: false } },
      decision_status: { operation_id: decision.operation_id, state: 'awaiting-authentication' }, allowed_actions: [],
      pending_operations: [{ operation_id: decision.operation_id, action: 'activation.review', state: 'accepted', reason: 'none' }] } });
    mount(makeApi(), fixture.api);
    expect(await screen.findByText('The activation offer is unavailable.')).toBeTruthy();
    expect(screen.queryByText('Finish signing in in Chrome.')).toBeNull();
    expect(screen.queryByRole('group', { name: 'Sign-in options' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(fixture.api.openBrowser).toHaveBeenCalledExactlyOnceWith(authentication.browser_operation_id));
    expect(fixture.api.begin).not.toHaveBeenCalled();
  });

  it('ignores a late browser retry failure after the original decision is accepted', async () => {
    const fixture = desktopFixture();
    let reject!: (reason: Error) => void;
    const openBrowser = vi.fn(() => new Promise<void>((_, rejectPromise) => { reject = rejectPromise; }));
    fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!, authentication: { ...authentication, state: 'admitted',
      actions: { select: false, cancel: true, open_browser: true, switch_account: false } }, allowed_actions: ['auth.cancel'] } });
    mount(makeApi(), { ...fixture.api, openBrowser });
    fireEvent.click(await screen.findByRole('button', { name: 'Try again' }));
    await act(async () => fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!,
      decision_status: { operation_id: decision.operation_id, state: 'accepted' } } }));
    await act(async () => reject(new Error('late browser reply')));
    expect(screen.getByText('Activation accepted. Checking Full analytics…')).toBeTruthy();
    expect(screen.queryByText('Chrome could not be opened. Try again.')).toBeNull();
    expect(openBrowser).toHaveBeenCalledOnce();
  });

  it('keeps optional activation code entry collapsed and validates an explicit paste', async () => {
    const fixture = desktopFixture(); const api = makeApi();
    mount(api, fixture.api);
    const toggle = await screen.findByRole('button', { name: 'Use an activation code' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByLabelText('Activation code')).toBeNull();
    fireEvent.click(toggle);
    const input = screen.getByLabelText('Activation code');
    expect(input.getAttribute('autocomplete')).toBe('off');
    expect(document.activeElement).toBe(input);
    fireEvent.change(input, { target: { value: 'incomplete' } });
    fireEvent.click(screen.getByRole('button', { name: 'Activate', exact: true }));
    expect(screen.getByText('Enter the full activation code and try again.')).toBeTruthy();
    expect(api.redeem).not.toHaveBeenCalled();
    expect(fixture.api.begin).not.toHaveBeenCalled();
  });

  it('preserves the optional code after an uncertain outcome without resubmitting on owner updates', async () => {
    const fixture = desktopFixture();
    const code = 'clr1.' + 'A'.repeat(43);
    const api = makeApi({ redeem: vi.fn().mockRejectedValue(new CapabilityLicenseApiError("Activation couldn't be confirmed.")) });
    mount(api, fixture.api);
    fireEvent.click(await screen.findByRole('button', { name: 'Use an activation code' }));
    fireEvent.change(screen.getByLabelText('Activation code'), { target: { value: code } });
    fireEvent.click(screen.getByRole('button', { name: 'Activate', exact: true }));
    await screen.findByText("Activation couldn't be confirmed.");
    await act(async () => fixture.publish());
    expect(api.redeem).toHaveBeenCalledExactlyOnceWith(code, expect.any(AbortSignal));
    expect((screen.getByLabelText('Activation code') as HTMLInputElement).value).toBe(code);
    fireEvent.click(screen.getByRole('button', { name: 'Use an activation code' }));
    fireEvent.click(screen.getByRole('button', { name: 'Use an activation code' }));
    expect((screen.getByLabelText('Activation code') as HTMLInputElement).value).toBe(code);
    expect(fixture.api.begin).not.toHaveBeenCalled();
  });

  it('allows code recovery after accepted activation without starting another exchange', async () => {
    const fixture = withDecision();
    fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!, decision: null,
      decision_status: { operation_id: decision.operation_id, state: 'accepted' }, allowed_actions: [],
      pending_operations: [{ operation_id: decision.operation_id, action: 'activation.review', state: 'waiting', reason: 'none' }] } });
    const api = makeApi();
    mount(api, fixture.api);
    expect(await screen.findByText('Activation accepted. Checking Full analytics…')).toBeTruthy();
    const toggle = await screen.findByRole('button', { name: 'Use an activation code' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(api.redeem).not.toHaveBeenCalled();
    fireEvent.click(toggle);
    const code = 'clr1.' + 'A'.repeat(43);
    fireEvent.change(screen.getByLabelText('Activation code'), { target: { value: code } });
    const submit = screen.getByRole('button', { name: 'Activate', exact: true });
    fireEvent.click(submit); fireEvent.click(submit);
    await waitFor(() => expect(api.redeem).toHaveBeenCalledExactlyOnceWith(code, expect.any(AbortSignal)));
    expect(fixture.api.begin).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Activate for free' })).toBeNull();
    expect(screen.queryByText('On')).toBeNull();
  });

  it('waits for canonical readiness after code acceptance and prevents duplicate redemption', async () => {
    const fixture = desktopFixture();
    let accept!: () => void;
    const redeem = vi.fn(() => new Promise<{ state: 'checking' }>(resolve => { accept = () => resolve({ state: 'checking' }); }));
    const readiness = vi.fn().mockResolvedValueOnce(required).mockResolvedValueOnce(required).mockResolvedValue(active);
    mount(makeApi({ redeem, readiness }), fixture.api);
    fireEvent.click(await screen.findByRole('button', { name: 'Use an activation code' }));
    fireEvent.change(screen.getByLabelText('Activation code'), { target: { value: 'clr1.' + 'A'.repeat(43) } });
    const submit = screen.getByRole('button', { name: 'Activate', exact: true });
    fireEvent.click(submit); fireEvent.click(submit);
    expect(redeem).toHaveBeenCalledOnce();
    await act(async () => accept());
    expect(screen.queryByText('On')).toBeNull();
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole('button', { name: 'Check activation status' })).toBeNull();
    await act(async () => fixture.readiness(active));
    expect(await screen.findByText('On')).toBeTruthy();
    expect(screen.queryByLabelText('Activation code')).toBeNull();
    expect(redeem).toHaveBeenCalledOnce();
  });

  it('retires a pending code response on presentation replacement and retains explicit retry', async () => {
    const fixture = desktopFixture();
    let reject!: (reason: Error) => void;
    const redeem = vi.fn().mockImplementationOnce(() => new Promise((_, rejectPromise) => { reject = rejectPromise; }))
      .mockResolvedValue({ state: 'checking' });
    mount(makeApi({ redeem }), fixture.api);
    fireEvent.click(await screen.findByRole('button', { name: 'Use an activation code' }));
    const code = 'clr1.' + 'A'.repeat(43);
    fireEvent.change(screen.getByLabelText('Activation code'), { target: { value: code } });
    fireEvent.click(screen.getByRole('button', { name: 'Activate', exact: true }));
    const signal = redeem.mock.calls[0][1] as AbortSignal;
    await act(async () => fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!,
      generation: '33333333-3333-4333-8333-333333333333' } }));
    expect(signal.aborted).toBe(true);
    await act(async () => reject(new Error('late response')));
    expect((screen.getByLabelText('Activation code') as HTMLInputElement).value).toBe(code);
    expect(redeem).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: 'Activate', exact: true }));
    await waitFor(() => expect(redeem).toHaveBeenCalledTimes(2));
  });

  it.each([false, true])('retires code feedback when the displayed creator changes after acceptance=%s', async accepted => {
    const fixture = desktopFixture();
    let accept!: () => void;
    const redeem = vi.fn<CapabilityLicenseApi['redeem']>(() => new Promise<{ state: 'checking' }>(resolve => { accept = () => resolve({ state: 'checking' }); }));
    const api = makeApi({ redeem });
    const view = render(<ThemeProvider theme={theme}><CommercialActivationControls api={api} onboarding={fixture.api} creatorAccountId="creator-account" /></ThemeProvider>);
    fireEvent.click(await screen.findByRole('button', { name: 'Use an activation code' }));
    fireEvent.change(screen.getByLabelText('Activation code'), { target: { value: 'clr1.' + 'A'.repeat(43) } });
    fireEvent.click(screen.getByRole('button', { name: 'Activate', exact: true }));
    if (accepted) await act(async () => accept());
    view.rerender(<ThemeProvider theme={theme}><CommercialActivationControls api={api} onboarding={fixture.api} creatorAccountId="another-creator" /></ThemeProvider>);
    expect((redeem.mock.calls[0][1] as AbortSignal).aborted).toBe(!accepted);
    if (!accepted) await act(async () => accept());
    expect(screen.queryByText('Checking activation…')).toBeNull();
    expect(redeem).toHaveBeenCalledOnce();
    expect(api.readiness).not.toHaveBeenCalled();
  });

  it('keeps an open code form during readiness loss while disabling new work', async () => {
    const fixture = desktopFixture(), api = makeApi();
    mount(api, fixture.api);
    fireEvent.click(await screen.findByRole('button', { name: 'Use an activation code' }));
    const code = 'clr1.' + 'A'.repeat(43);
    fireEvent.change(screen.getByLabelText('Activation code'), { target: { value: code } });
    await act(async () => fixture.publish({ connected: false }));
    expect(screen.getByLabelText('Activation code')).toHaveProperty('value', code);
    expect(screen.getByRole('button', { name: 'Set up Full analytics' })).toHaveProperty('disabled', true);
    expect(screen.getByRole('button', { name: 'Activate', exact: true })).toHaveProperty('disabled', true);
    await act(async () => { fixture.readiness(null); fixture.publish({ connected: true }); });
    expect(screen.getByRole('button', { name: 'Activate', exact: true })).toHaveProperty('disabled', true);
    expect(api.redeem).not.toHaveBeenCalled();
    await act(async () => fixture.readiness(required));
    expect(screen.getByRole('button', { name: 'Activate', exact: true })).toHaveProperty('disabled', false);
    expect(api.redeem).not.toHaveBeenCalled();
  });

  it('renders browser waiting without auto-confirming or offering a code field', async () => {
    const fixture = desktopFixture();
    fixture.publish({ snapshot: { ...fixture.api.getState().snapshot!, decision_status: { operation_id: decision.operation_id, state: 'awaiting-authentication' }, allowed_actions: [],
      pending_operations: [{ operation_id: decision.operation_id, action: 'activation.review', state: 'accepted', reason: 'none' }] } });
    mount(makeApi(), fixture.api);
    expect(await screen.findByText('Finish signing in in Chrome.')).toBeTruthy();
    expect(fixture.api.begin).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Activate for free' })).toBeNull();
    expect(screen.queryByLabelText('Activation code')).toBeNull();
  });

  it('disables a stale offer during local service interruption', async () => {
    const fixture = withDecision();
    mount(makeApi(), fixture.api);
    await screen.findByText('Off');
    await act(async () => fixture.publish({ connected: false }));
    expect((screen.getByRole('button', { name: 'Activate for free' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('Activation status could not be received.')).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/saved data|removed|safe/i);
    expect(fixture.api.begin).not.toHaveBeenCalled();
  });

  it('keeps an uncertain confirmation explicit across passive revisions', async () => {
    const fixture = withDecision();
    const begin = vi.fn().mockRejectedValue(Error('reply lost'));
    mount(makeApi(), { ...fixture.api, begin });
    fireEvent.click(await screen.findByRole('button', { name: 'Activate for free' }));
    expect(await screen.findByText('Activation could not be confirmed.')).toBeTruthy();
    await act(async () => fixture.publish());
    expect(begin).toHaveBeenCalledOnce();
    expect(screen.queryByText('On')).toBeNull();
    expect(screen.getByText('Activation could not be confirmed.')).toBeTruthy();
  });

  it('recovers an unavailable owner view without a component request or explicit mutation', async () => {
    const fixture = desktopFixture();
    fixture.readiness(null);
    const api = makeApi();
    mount(api, fixture.api);
    expect(screen.getByText("Activation couldn't be checked.")).toBeTruthy();
    await act(async () => fixture.readiness(required));
    expect(screen.getByText('Off')).toBeTruthy();
    expect(api.readiness).not.toHaveBeenCalled();
    expect(fixture.api.begin).not.toHaveBeenCalled();
    expect(api.redeem).not.toHaveBeenCalled();
  });

  it('does not poll an unavailable commercial result and observes its later owner update', async () => {
    vi.useFakeTimers();
    const fixture = desktopFixture();
    fixture.readiness({ ...required, commercial_authority: 'unavailable' });
    const api = makeApi();
    mount(api, fixture.api);
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
    expect(api.readiness).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
    await act(async () => fixture.readiness(active));
    expect(screen.getByText('On')).toBeTruthy();
    expect(fixture.api.begin).not.toHaveBeenCalled();
    expect(api.redeem).not.toHaveBeenCalled();
  });

  it('keeps active authority with blocked analysis distinct from Full-ready', async () => {
    const fixture = desktopFixture();
    fixture.readiness({ ...active, analysis_admission: 'blocked' });
    mount(makeApi(), fixture.api);
    expect(await screen.findByText("New messages aren't being analyzed")).toBeTruthy();
    expect(screen.getByText('Needs attention')).toBeTruthy();
    expect(screen.queryByText('On')).toBeNull();
  });

  it('states an unavailable request without prototype recovery or protected identifiers', async () => {
    mount(makeApi(), desktopFixture(false).api, true);
    expect(await screen.findByText('The activation request is unavailable.')).toBeTruthy();
    expect(screen.queryByLabelText('Activation code')).toBeNull();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.body.textContent).not.toMatch(/reference_id|license_id|issuance_id|seat_id|package|organization_id|installation_id|proof|signature|JWS/i);
  });
});
