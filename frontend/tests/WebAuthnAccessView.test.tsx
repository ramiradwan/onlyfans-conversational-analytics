import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mockFeedbackOverflow } from './feedbackGeometry';

import { LoginOutcomeError, EnrollmentOutcomeError, WebAuthnStatusError, type PushedAccess, type WebAuthnApi } from '../shared-setup/webauthn';
import { theme } from '../src/theme';
import { WebAuthnAccessView } from '../src/views/WebAuthnAccessView';
import { SavedDataView } from '../src/views/SavedDataView';
import type { DesktopSnapshot, DesktopState } from '../onboarding/client';
import type { AuthorityRecoveryApi } from '../src/services/desktopOnboarding';

/** A promise the test settles, so the in-flight state is observable. */
function deferred() {
  let settle: (() => void) | undefined;
  const promise = new Promise<void>((resolve) => {
    settle = resolve;
  });
  return { promise, resolve: settle as () => void };
}

function renderView(api: WebAuthnApi, onAuthenticated = vi.fn()) {
  render(
    <ThemeProvider theme={theme}>
      <WebAuthnAccessView api={api} onAuthenticated={onAuthenticated} />
    </ThemeProvider>,
  );
  const button = (name: string) =>
    screen.getByRole('button', { name }) as HTMLButtonElement;
  return {
    onAuthenticated,
    enroll: () => button('Set up a passkey'),
    signIn: () => button('Sign in with passkey'),
  };
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); sessionStorage.removeItem('desktop-presentation'); });

const nativeGeneration = '11111111-1111-4111-8111-111111111111';

function nativeOpen(initial = false) {
  let armed = initial;
  sessionStorage.setItem('desktop-presentation', nativeGeneration);
  const invoke = vi.fn(async (command: string, { action }: { action: string }) => {
    expect(command).toBe('desktop_window');
    if (action === 'claim_passkey_unlock' || action === 'discard_passkey_unlock') {
      const pending = armed;
      armed = false;
      return pending;
    }
    return action === 'open_passkey_settings';
  });
  vi.stubGlobal('__TAURI__', { core: { invoke } });
  return { invoke, open() { armed = true; window.dispatchEvent(new Event('dipsy-passkey-open')); } };
}

it.each([false, true])('direct browser points to desktop instead of opening a passkey ceremony (enrolled=%s)', async enrolled => {
  const api = { state: vi.fn(async () => ({ enrolled, authenticated: false, nativeRequired: true as const })),
    enroll: vi.fn(), login: vi.fn() };
  renderView(api);
  expect(await screen.findByRole('heading', { name: 'Open Dipsy' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: /Create passkey|Unlock|Sign in with passkey/ })).toBeNull();
  expect(api.enroll).not.toHaveBeenCalled();
  expect(api.login).not.toHaveBeenCalled();
});

it('prompts once for an explicit native Open and leaves Unlock after a refusal', async () => {
  const handle = nativeOpen(true);
  const login = vi.fn().mockRejectedValue(new LoginOutcomeError('finish', false, 'permission_required'));
  const api = { state: vi.fn(async () => ({ enrolled: true, authenticated: false, nativeRequired: true as const })),
    enroll: vi.fn(), login };
  renderView(api);
  const settings = await screen.findByRole('button', { name: 'Open Windows settings' });
  expect(login).toHaveBeenCalledExactlyOnceWith(expect.any(AbortSignal), true);
  expect(screen.getByRole('alert').textContent).toContain('Passkey access');
  fireEvent.click(settings);
  expect(handle.invoke).toHaveBeenCalledWith('desktop_window', { action: 'open_passkey_settings' });
  await act(async () => {
    window.dispatchEvent(new Event('focus'));
    window.dispatchEvent(new Event('dipsy-passkey-open'));
  });
  expect(login).toHaveBeenCalledOnce();
  await act(async () => { handle.open(); });
  expect(login).toHaveBeenCalledTimes(2);
  expect(screen.getByRole('button', { name: 'Unlock' })).toBeTruthy();
});

it('discards the earlier Open if an admitted session is already live', async () => {
  const handle = nativeOpen(true);
  const login = vi.fn();
  const view = renderView({ enroll: vi.fn(), login, state: vi.fn(async () =>
    ({ enrolled: true, authenticated: true, nativeRequired: true as const })) });
  await waitFor(() => expect(view.onAuthenticated).toHaveBeenCalledOnce());
  expect(handle.invoke).toHaveBeenCalledExactlyOnceWith('desktop_window', { action: 'discard_passkey_unlock' });
  expect(login).not.toHaveBeenCalled();
});

it.each(['Lock saved data', 'Lock Dipsy'])('claims one native Open for saved data and stays locked after returning to that view via %s', async action => {
  const handle = nativeOpen(true);
  const events = new EventTarget();
  const close = vi.fn();
  vi.stubGlobal('EventSource', class {
    constructor(path: string) { expect(path).toBe('/api/v1/saved-data/session/events'); }
    addEventListener = events.addEventListener.bind(events);
    close = close;
  });
  const request = vi.fn(async (path: string) => {
    const replies: Record<string, object> = {
      '/api/v1/saved-data/available': { available: true, authorization_expired: true },
      '/api/v1/desktop/session': { role: 'desktop', stage: 'runtime', generation: nativeGeneration, csrf_token: 'A'.repeat(43) },
      '/api/v1/saved-data/login/begin': { windows_hello: true },
      '/api/v1/saved-data/login/windows-hello': { csrf_token: 'saved-session', expires_in: 60 },
      '/api/v1/saved-data/view': { messages: [{ id: 'fixture', text: 'A saved message', sent_at: '' }], next: null, deletion_pending: false },
      '/api/v1/saved-data/lock': {},
    };
    if (!Object.hasOwn(replies, path)) throw Error('Unexpected request');
    return new Response(JSON.stringify(replies[path]));
  });
  vi.stubGlobal('fetch', request);
  render(<ThemeProvider theme={theme}>
    <script id="fastapi-config" type="application/json">{JSON.stringify({ ACCOUNT_PASSKEY_ENABLED: true })}</script>
    <SavedDataView />
  </ThemeProvider>);
  expect(await screen.findByText('A saved message')).toBeTruthy();
  if (action === 'Lock saved data') {
    fireEvent.click(screen.getByRole('button', { name: 'Lock saved data' }));
  } else {
    await act(async () => { events.dispatchEvent(new MessageEvent('access', { data: '{"access":"locked"}' })); });
  }
  expect(await screen.findByRole('button', { name: 'Unlock saved data' })).toBeTruthy();
  expect(screen.queryByText('A saved message')).toBeNull();
  expect(close).toHaveBeenCalled();
  await act(async () => { window.dispatchEvent(new Event('focus')); window.dispatchEvent(new Event('dipsy-passkey-open')); });
  expect(request.mock.calls.filter(([path]) => path.endsWith('/login/windows-hello'))).toHaveLength(1);
  cleanup();
  const login = vi.fn();
  renderView({ enroll: vi.fn(), login, state: vi.fn(async () => ({
    enrolled: true, authenticated: false, nativeRequired: true as const,
  })) });
  expect(await screen.findByRole('button', { name: 'Unlock' })).toBeTruthy();
  expect(login).not.toHaveBeenCalled();
  await act(async () => { handle.open(); });
  expect(login).toHaveBeenCalledOnce();
});

it('retries transient status reads within a bound without inventing enrollment', async () => {
  vi.useFakeTimers();
  const state = vi.fn().mockRejectedValue(new WebAuthnStatusError('network'));
  const api = { state, enroll: vi.fn(), login: vi.fn() };
  renderView(api);
  for (let attempt = 0; attempt < 4; attempt++) await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(state).toHaveBeenCalledTimes(4);
  expect(screen.getByRole('heading', { name: 'Can’t check your passkey right now' })).toBeTruthy();
  expect(screen.getByRole('status').textContent).toBe('This page updates by itself.');
  expect(screen.getByRole('main').querySelector('.onboarding-surface-body')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(state).toHaveBeenCalledTimes(4);
  expect(screen.queryByRole('button', { name: 'Create passkey' })).toBeNull();
  expect(api.enroll).not.toHaveBeenCalled(); expect(api.login).not.toHaveBeenCalled();
});

it('reports denied status access without retrying or treating it as absent enrollment', async () => {
  vi.useFakeTimers();
  const state = vi.fn().mockRejectedValue(new WebAuthnStatusError('forbidden', 403));
  renderView({ state, enroll: vi.fn(), login: vi.fn() });
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(state).toHaveBeenCalledOnce();
  expect(screen.getByRole('alert').textContent).toContain('Dipsy isn’t allowed to check access on this computer.');
  expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Create passkey' })).toBeNull();
});

async function readFailure() {
  await waitFor(() => expect(screen.queryByRole('alert')).toBeTruthy());
  expect(screen.queryByRole('button', { name: 'Show details' })).toBeNull();
  return screen.findByRole('alert');
}

describe('WebAuthn access view', () => {

  it('uses the confirmed first-passkey state without an initial sign-in action', async () => {
    const api = { state: vi.fn(async () => ({ enrolled: false, authenticated: false })), enroll: vi.fn(async () => {}), login: vi.fn(async () => {}) };
    const view = renderView(api);
    const create = await screen.findByRole('button', { name: 'Create passkey' });
    expect(screen.getByRole('heading', { name: 'Protect your analytics on this computer' })).toBeTruthy();
    expect(screen.getByText('Create a passkey. Your computer will ask you to confirm it’s you, so only you can open your analytics here.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Sign in with passkey' })).toBeNull();
    fireEvent.click(create);
    await waitFor(() => expect(view.onAuthenticated).toHaveBeenCalledTimes(1));
    expect(api.enroll).toHaveBeenCalledTimes(1);
    expect(api.login).not.toHaveBeenCalled();
    expect(api.state).toHaveBeenCalledTimes(1);
  });

  it('skips both ceremonies when the owner confirms the existing local session', async () => {
    const api = { state: vi.fn(async () => ({ enrolled: true, authenticated: true })), enroll: vi.fn(async () => {}), login: vi.fn(async () => {}) };
    const view = renderView(api);
    await waitFor(() => expect(view.onAuthenticated).toHaveBeenCalledTimes(1));
    expect(api.enroll).not.toHaveBeenCalled();
    expect(api.login).not.toHaveBeenCalled();
  });

  it.each([
    [new Error('Finish response lost'), 'Passkey setup didn’t finish.'],
    [{ name: 'NotAllowedError' }, 'Passkey setup didn’t finish.'],
  ])('retains an unconfirmed enrollment without starting another ceremony', async (failure, message) => {
    const api = { enroll: vi.fn(() => Promise.reject(failure)), login: vi.fn(async () => {}) };
    const view = renderView(api);
    fireEvent.click(view.enroll());
    expect((await readFailure()).textContent).toBe(message);
    expect(api.enroll).toHaveBeenCalledTimes(1);
    expect(api.login).not.toHaveBeenCalled();
    expect(view.onAuthenticated).not.toHaveBeenCalled();
  });

  it('has one primary sign-in action and one product header outside the task landmark', () => {
    const view = renderView({ enroll: vi.fn(), login: vi.fn() });
    const main = screen.getByRole('main');
    expect(main.querySelectorAll('[data-visual="brand-tile"]')).toHaveLength(0);
    expect(document.querySelectorAll('[data-visual="brand-tile"]')).toHaveLength(1);
    expect(screen.getByRole('banner').querySelector('[data-visual="brand-tile"]')).not.toBeNull();
    expect(main.querySelectorAll('.MuiButton-contained')).toHaveLength(1);
    expect(view.signIn().classList.contains('MuiButton-contained')).toBe(true);
    expect(view.enroll().tagName).toBe('BUTTON');
    expect(view.enroll().type).toBe('button');
    expect(screen.queryByText('Already set up on this computer?')).toBeNull();
  });

  it('reports the first authenticated session after one creation ceremony', async () => {
    const order: string[] = [];
    const api: WebAuthnApi = {
      enroll: vi.fn(async () => { order.push('enroll'); }),
      login: vi.fn(async () => { order.push('login'); }),
    };
    const view = renderView(api);

    fireEvent.click(view.enroll());

    await waitFor(() => expect(view.onAuthenticated).toHaveBeenCalledTimes(1));
    expect(order).toEqual(['enroll']);
  });

  it('signs in an enrolled device without enrolling again', async () => {
    const api: WebAuthnApi = { enroll: vi.fn(), login: vi.fn(async () => {}) };
    const view = renderView(api);

    fireEvent.click(view.signIn());

    await waitFor(() => expect(view.onAuthenticated).toHaveBeenCalledTimes(1));
    expect(api.enroll).not.toHaveBeenCalled();
    expect(api.login).toHaveBeenCalledTimes(1);
  });

  it('does not report a session when the ceremony fails and keeps technical detail out', async () => {
    const api: WebAuthnApi = {
      enroll: vi.fn(),
      login: vi.fn(async () => { throw new Error('WebAuthn request failed (500)'); }),
    };
    const view = renderView(api);

    fireEvent.click(view.signIn());

    const alert = await readFailure();
    expect(alert.textContent).toBe('Sign-in didn’t finish.');
    expect(alert.querySelector('.onboarding-status')?.getAttribute('data-tone')).toBe('attention');
    expect(view.onAuthenticated).not.toHaveBeenCalled();
  });

  it('does not infer why the browser refused a ceremony', async () => {
    const rejection = { name: 'NotAllowedError' };
    const api: WebAuthnApi = {
      enroll: vi.fn(),
      login: vi.fn(() => Promise.reject(rejection)),
    };
    const view = renderView(api);

    fireEvent.click(view.signIn());

    const alert = await readFailure();
    expect(alert.textContent).toBe('Sign-in didn’t finish.');
    expect(alert.closest('[data-visual="passkey-card"]')).not.toBeNull();
    expect(view.onAuthenticated).not.toHaveBeenCalled();
  });

  it('describes a failed passkey setup as setup, not sign-in', async () => {
    const api: WebAuthnApi = {
      enroll: vi.fn(async () => { throw new Error('No passkey was created.'); }),
      login: vi.fn(),
    };
    const view = renderView(api);

    fireEvent.click(view.enroll());

    const alert = await readFailure();
    expect(alert.textContent).toBe('Passkey setup didn’t finish.');
    expect(api.login).not.toHaveBeenCalled();
    expect(view.onAuthenticated).not.toHaveBeenCalled();
  });

  it('refuses a second ceremony while one is in flight', async () => {
    const pending = deferred();
    const api: WebAuthnApi = { enroll: vi.fn(), login: vi.fn(() => pending.promise) };
    const view = renderView(api);

    fireEvent.click(view.signIn());

    expect(await screen.findByText('Waiting for your passkey…')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Sign in with passkey' })).toBeNull();
    expect(view.enroll().disabled).toBe(true);

    fireEvent.click(view.enroll());
    expect(api.enroll).not.toHaveBeenCalled();

    pending.resolve();
    await waitFor(() => expect(view.onAuthenticated).toHaveBeenCalledTimes(1));
  });

  it('clears a previous failure when the next attempt starts', async () => {
    mockFeedbackOverflow();
    const pending = deferred();
    const login = vi.fn()
      .mockImplementationOnce(async () => { throw new Error('No passkey was selected.'); })
      .mockImplementationOnce(() => pending.promise);
    const view = renderView({ enroll: vi.fn(), login });

    fireEvent.click(view.signIn());
    expect(await readFailure()).not.toBeNull();
    expect(screen.queryByRole('dialog')).toBeNull();

    fireEvent.click(view.signIn());
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());

    pending.resolve();
    await waitFor(() => expect(view.onAuthenticated).toHaveBeenCalledTimes(1));
  });

  it('keeps recovery visible without a details step even when text exceeds the old reserved height', async () => {
    mockFeedbackOverflow();
    const pending = deferred();
    const failure = new Error('No passkey was selected.');
    const login = vi.fn()
      .mockRejectedValueOnce(failure)
      .mockImplementationOnce(() => pending.promise.then(() => { throw failure; }));
    const view = renderView({ enroll: vi.fn(), login });

    fireEvent.click(view.signIn());
    const alert = await readFailure();
    const message = alert.textContent;
    expect(screen.queryByRole('dialog')).toBeNull();

    fireEvent.click(view.signIn());
    expect(screen.queryByRole('button', { name: 'Show details' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Sign in with passkey' })).toBeNull();
    pending.resolve();
    expect((await readFailure()).textContent).toBe(message);
    expect(login).toHaveBeenCalledTimes(2);
    expect(view.onAuthenticated).not.toHaveBeenCalled();
  });
});

it('recovers transient passkey status automatically without presenting a retry action', async () => {
  vi.useFakeTimers();
  const state = vi.fn().mockRejectedValueOnce(new WebAuthnStatusError('unavailable', 503))
    .mockResolvedValueOnce({ enrolled: true, authenticated: false });
  const api = { state, enroll: vi.fn(), login: vi.fn() };
  renderView(api);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Sign in with passkey' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Set up a passkey' })).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(screen.getByRole('button', { name: 'Sign in with passkey' })).toBeTruthy();
  expect(state).toHaveBeenCalledTimes(2); expect(api.enroll).not.toHaveBeenCalled(); expect(api.login).not.toHaveBeenCalled();
});
it('stage-specific begin refusal does not claim the user cancelled a passkey', async () => {
  const view = renderView({ enroll: vi.fn(), login: vi.fn(async () => { throw new LoginOutcomeError('begin'); }) });
  fireEvent.click(view.signIn());
  expect((await readFailure()).textContent).toBe('Sign-in didn’t finish.');
});
it('an unmounted sign-in cannot navigate when its stale success resolves', async () => {
  const pending = deferred(); let signal: AbortSignal | undefined;
  const view = renderView({ enroll: vi.fn(), login: vi.fn((incoming?: AbortSignal) => { signal = incoming; return pending.promise; }) });
  fireEvent.click(view.signIn()); cleanup();
  expect(signal?.aborted).toBe(true);
  pending.resolve(); await pending.promise;
  expect(view.onAuthenticated).not.toHaveBeenCalled();
});

it('suppresses a pending passkey completion when native authority becomes blocked', async () => {
  const pending = deferred();
  const api = { state: vi.fn(async () => ({ enrolled: true, authenticated: false })), enroll: vi.fn(),
    login: vi.fn().mockImplementationOnce(() => pending.promise).mockResolvedValueOnce(undefined) };
  const onAuthenticated = vi.fn();
  let desktop: DesktopState = { snapshot: null, pending: null, connected: true, error: null };
  const listeners = new Set<() => void>();
  const recoveryApi: AuthorityRecoveryApi = { getState: () => desktop,
    subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener); }; }, act: vi.fn() };
  const show = (snapshot: DesktopSnapshot) => act(() => {
    desktop = { ...desktop, snapshot }; listeners.forEach(listener => listener());
  });
  render(<ThemeProvider theme={theme}><WebAuthnAccessView api={api} recoveryApi={recoveryApi} onAuthenticated={onAuthenticated}
    sessionNotice="Your session ended. Sign in to continue." /></ThemeProvider>);
  const signIn = await screen.findByRole('button', { name: 'Sign in with passkey' });
  expect(api.login).not.toHaveBeenCalled();
  fireEvent.click(signIn);
  expect(api.login).toHaveBeenCalledOnce();
  const blocked: DesktopSnapshot = { journey_id: '11111111-1111-4111-8111-111111111111',
    generation: '22222222-2222-4222-8222-222222222222', revision: 0, facts: {}, next_task: 'account-connection',
    authority_recovery: 'blocked', allowed_actions: [], pending_operations: [], local_ready: false, full_ready: false,
    analysis_readiness: { availability: 'unauthorized', creator_account_id: null, value: null } };
  show(blocked);
  expect(screen.queryByRole('button', { name: 'Sign in with passkey' })).toBeNull();
  await act(async () => pending.resolve());
  expect(onAuthenticated).not.toHaveBeenCalled();
  expect(recoveryApi.act).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: 'Sign in with passkey' })).toBeNull();
  show({ ...blocked, authority_recovery: undefined });
  const fresh = await screen.findByRole('button', { name: 'Sign in with passkey' });
  expect(onAuthenticated).not.toHaveBeenCalled();
  fireEvent.click(fresh);
  await waitFor(() => expect(onAuthenticated).toHaveBeenCalledOnce());
  expect(api.login).toHaveBeenCalledTimes(2);
});
/** An access stream the test pushes into. */
function pushedStream() {
  let deliver: ((access: PushedAccess) => void) | undefined;
  let opened: AbortSignal | undefined;
  const watch = vi.fn((signal: AbortSignal, receive: (access: PushedAccess) => void) => {
    opened = signal; deliver = receive;
    return new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }));
  });
  return { watch, signal: () => opened, push: (access: PushedAccess) => act(() => { deliver?.(access); }) };
}
const lapsed = (access: 'renewing' | 'expired' | 'removed') => new WebAuthnStatusError('access', 409, access);

describe('lapsed access on this computer', () => {
  it.each([
    ['renewing', 'Renewing access on this computer', 'Renewal needs an internet connection.'],
    ['expired', 'Account authorization has expired', null],
    ['removed', 'Access removed from this computer', null],
  ] as const)('names %s access with guidance and no action to retry', async (access, heading, guidance) => {
    vi.useFakeTimers();
    const api = { state: vi.fn().mockRejectedValue(lapsed(access)), enroll: vi.fn(), login: vi.fn() };
    renderView(api);
    await act(async () => { await vi.advanceTimersByTimeAsync(10 * 60_000); });
    expect(screen.getByRole('heading', { level: 1, name: heading })).toBeTruthy();
    if (guidance) expect(screen.getByRole('status').textContent).toBe(guidance);
    else expect(screen.getByText(access === 'expired' ? 'New capture and analysis are paused on this computer.'
      : 'This computer can no longer open analytics.')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(api.state).toHaveBeenCalledOnce();
    expect(api.enroll).not.toHaveBeenCalled(); expect(api.login).not.toHaveBeenCalled();
  });

  it('follows pushed changes with one quiet read each and never polls while renewing', async () => {
    vi.useFakeTimers();
    const stream = pushedStream();
    let answer: ((value: { enrolled: boolean; authenticated: boolean }) => void) | undefined;
    const state = vi.fn()
      .mockRejectedValueOnce(lapsed('renewing'))
      .mockImplementationOnce(() => new Promise((resolve) => { answer = resolve; }));
    renderView({ state, watch: stream.watch, enroll: vi.fn(), login: vi.fn() });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    stream.push('renewing');
    await act(async () => { await vi.advanceTimersByTimeAsync(10 * 60_000); });
    expect(state).toHaveBeenCalledOnce();
    expect(screen.getByRole('heading', { level: 1, name: 'Renewing access on this computer' })).toBeTruthy();
    stream.push('available');
    expect(state).toHaveBeenCalledTimes(2);
    // The read keeps the current screen rather than flashing the checking state.
    expect(screen.getByRole('heading', { level: 1, name: 'Renewing access on this computer' })).toBeTruthy();
    await act(async () => { answer?.({ enrolled: true, authenticated: false }); await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByRole('heading', { level: 1, name: 'Confirm it’s you' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Sign in with passkey' })).toBeTruthy();
    stream.push('available');
    expect(state).toHaveBeenCalledTimes(2);
    expect(stream.watch).toHaveBeenCalledOnce();
  });

  it('confirms the first read with the first pushed value without reading again', async () => {
    const stream = pushedStream();
    const state = vi.fn(async () => ({ enrolled: false, authenticated: false }));
    renderView({ state, watch: stream.watch, enroll: vi.fn(), login: vi.fn() });
    stream.push('available');
    await screen.findByRole('button', { name: 'Create passkey' });
    stream.push('available');
    expect(state).toHaveBeenCalledOnce();
    cleanup();
    expect(stream.signal()?.aborted).toBe(true);
  });

  it('reads a change pushed during a ceremony once the ceremony ends', async () => {
    const stream = pushedStream();
    const pending = deferred();
    const state = vi.fn()
      .mockResolvedValueOnce({ enrolled: true, authenticated: false })
      .mockRejectedValueOnce(lapsed('removed'));
    const login = vi.fn(() => pending.promise.then(() => { throw new LoginOutcomeError('begin'); }));
    const view = renderView({ state, watch: stream.watch, enroll: vi.fn(), login });
    await screen.findByRole('button', { name: 'Sign in with passkey' });
    stream.push('available');
    fireEvent.click(view.signIn());
    stream.push('removed');
    expect(state).toHaveBeenCalledOnce();
    pending.resolve();
    await screen.findByRole('heading', { level: 1, name: 'Access removed from this computer' });
    expect(state).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByText('This computer can no longer open analytics.')).toBeTruthy();
    expect(view.onAuthenticated).not.toHaveBeenCalled();
  });
});

it('a confirmed created passkey with no session offers sign-in rather than another creation', async () => {
  const view = renderView({ state: vi.fn(async () => ({ enrolled: false, authenticated: false })),
    enroll: vi.fn(async () => { throw new EnrollmentOutcomeError(true); }), login: vi.fn() });
  fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }));
  await screen.findByRole('button', { name: 'Sign in with passkey' });
  expect(screen.getByText('Passkey created. Sign in to continue.')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Create passkey' })).toBeNull();
  expect(view.onAuthenticated).not.toHaveBeenCalled();
});

it('keeps an uncertain creation distinct from sign-in and waits for a fresh gesture', async () => {
  const state = vi.fn().mockResolvedValueOnce({ enrolled: false, authenticated: false })
    .mockResolvedValueOnce({ enrolled: true, authenticated: false });
  const enroll = vi.fn().mockRejectedValue(new EnrollmentOutcomeError(null));
  const login = vi.fn().mockRejectedValue(new LoginOutcomeError('begin'));
  renderView({ state, enroll, login });
  fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }));
  const signIn = await screen.findByRole('button', { name: 'Sign in with passkey' });
  expect((await readFailure()).textContent).toBe('Passkey setup didn’t finish.');
  expect(enroll).toHaveBeenCalledOnce();
  expect(login).not.toHaveBeenCalled();
  fireEvent.click(signIn);
  expect((await readFailure()).textContent).toBe('Sign-in didn’t finish.');
  expect(login).toHaveBeenCalledOnce();
});

it('requires a fresh gesture after a fatal session even if the cookie remains current', async () => {
  const api = {
    state: vi.fn(async () => ({ enrolled: true, authenticated: true })),
    enroll: vi.fn(),
    login: vi.fn().mockRejectedValueOnce(new LoginOutcomeError('finish', true)).mockResolvedValueOnce(undefined),
  };
  const onAuthenticated = vi.fn();
  render(<ThemeProvider theme={theme}><WebAuthnAccessView api={api} onAuthenticated={onAuthenticated}
    sessionNotice="Your session ended. Sign in to continue." /></ThemeProvider>);
  const signIn = await screen.findByRole('button', { name: 'Sign in with passkey' });
  expect(onAuthenticated).not.toHaveBeenCalled();
  expect(api.login).not.toHaveBeenCalled();
  fireEvent.click(signIn);
  expect((await readFailure()).textContent).toBe('Sign-in didn’t finish.');
  expect(onAuthenticated).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Sign in with passkey' }));
  await waitFor(() => expect(onAuthenticated).toHaveBeenCalledOnce());
  expect(api.login).toHaveBeenCalledTimes(2);
  expect(api.enroll).not.toHaveBeenCalled();
});

it('reconciles a lost registration finish without another passkey ceremony', async () => {
  vi.useFakeTimers();
  const state = vi.fn().mockResolvedValueOnce({ enrolled: false, authenticated: false })
    .mockRejectedValueOnce(new WebAuthnStatusError('network'))
    .mockResolvedValueOnce({ enrolled: true, authenticated: true });
  const enroll = vi.fn().mockRejectedValue(new EnrollmentOutcomeError(null));
  const login = vi.fn();
  const view = renderView({ state, enroll, login });
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  fireEvent.click(screen.getByRole('button', { name: 'Create passkey' }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.queryByRole('button', { name: 'Create passkey' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
  expect(view.onAuthenticated).not.toHaveBeenCalled();
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(view.onAuthenticated).toHaveBeenCalledOnce();
  expect(enroll).toHaveBeenCalledOnce();
  expect(login).not.toHaveBeenCalled();
});

it('reconciles a lost sign-in finish before offering another ceremony', async () => {
  vi.useFakeTimers();
  const state = vi.fn().mockResolvedValueOnce({ enrolled: true, authenticated: false })
    .mockRejectedValueOnce(new WebAuthnStatusError('network'))
    .mockResolvedValueOnce({ enrolled: true, authenticated: false });
  const login = vi.fn().mockRejectedValue(new LoginOutcomeError('finish', true));
  const enroll = vi.fn();
  const view = renderView({ state, enroll, login });
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  fireEvent.click(view.signIn());
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.queryByRole('button', { name: 'Sign in with passkey' })).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(view.signIn().disabled).toBe(false);
  expect(screen.getByRole('alert').textContent).toBe('Sign-in didn’t finish.');
  expect(view.onAuthenticated).not.toHaveBeenCalled();
  expect(login).toHaveBeenCalledOnce();
  expect(enroll).not.toHaveBeenCalled();
});

it('reconciles a later pushed access change after the status recovery budget ends', async () => {
  vi.useFakeTimers();
  const stream = pushedStream();
  const state = vi.fn().mockRejectedValue(new WebAuthnStatusError('network'));
  const api = { state, watch: stream.watch, enroll: vi.fn(), login: vi.fn() };
  renderView(api);
  for (let i = 0; i < 4; i++) await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(state).toHaveBeenCalledTimes(4);
  expect(screen.queryAllByRole('button')).toHaveLength(0);
  state.mockResolvedValue({ enrolled: true, authenticated: false });
  stream.push('available');
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(state).toHaveBeenCalledTimes(5);
  expect(screen.getByRole('button', { name: 'Sign in with passkey' })).toBeTruthy();
  expect(api.enroll).not.toHaveBeenCalled(); expect(api.login).not.toHaveBeenCalled();
});

it('does not retry malformed status or an unknown status failure', async () => {
  vi.useFakeTimers();
  const state = vi.fn().mockRejectedValue(new Error('Unknown status failure'));
  renderView({ state, enroll: vi.fn(), login: vi.fn() });
  await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
  expect(state).toHaveBeenCalledOnce();
  expect(screen.queryAllByRole('button')).toHaveLength(0);
  expect(screen.getByRole('alert').textContent).toBe('Dipsy can’t confirm passkey status on this computer.');
});

it('cancels scheduled recovery when the runtime passkey page closes', async () => {
  vi.useFakeTimers();
  const state = vi.fn().mockResolvedValueOnce({ enrolled: true, authenticated: false })
    .mockRejectedValue(new WebAuthnStatusError('network'));
  const login = vi.fn().mockRejectedValue(new LoginOutcomeError('finish', true));
  const view = renderView({ state, enroll: vi.fn(), login });
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  fireEvent.click(view.signIn());
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(state).toHaveBeenCalledTimes(2);
  cleanup();
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(state).toHaveBeenCalledTimes(2);
  expect(login).toHaveBeenCalledOnce();
  expect(view.onAuthenticated).not.toHaveBeenCalled();
});

it('does not repeat a definite refusal when the same access hint is pushed again', async () => {
  vi.useFakeTimers();
  const stream = pushedStream();
  const state = vi.fn().mockRejectedValue(new WebAuthnStatusError('forbidden', 403));
  const api = { state, watch: stream.watch, enroll: vi.fn(), login: vi.fn() };
  renderView(api);
  stream.push('available');
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(state).toHaveBeenCalledOnce();
  stream.push('available'); stream.push('available');
  await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
  expect(state).toHaveBeenCalledOnce();
  expect(screen.queryAllByRole('button')).toHaveLength(0);
  state.mockRejectedValue(lapsed('removed'));
  stream.push('removed');
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(state).toHaveBeenCalledTimes(2);
  expect(screen.getByRole('heading', { name: 'Access removed from this computer' })).toBeTruthy();
  expect(api.login).not.toHaveBeenCalled();
});

it('retires a stalled status read and ignores a late answer after recovery', async () => {
  vi.useFakeTimers();
  let late!: (value: { enrolled: boolean; authenticated: boolean }) => void;
  const state = vi.fn().mockImplementationOnce(() => new Promise(resolve => { late = resolve; }))
    .mockResolvedValueOnce({ enrolled: true, authenticated: false });
  const api = { state, enroll: vi.fn(), login: vi.fn() };
  const view = renderView(api);
  await act(async () => { await vi.advanceTimersByTimeAsync(8000); });
  expect(state.mock.calls[0][0].aborted).toBe(true);
  expect(screen.queryAllByRole('button')).toHaveLength(0);
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(view.signIn().disabled).toBe(false);
  await act(async () => { late({ enrolled: true, authenticated: true }); });
  expect(view.onAuthenticated).not.toHaveBeenCalled();
  expect(state).toHaveBeenCalledTimes(2);
  expect(api.login).not.toHaveBeenCalled();
});

it('disables sign-in while a pushed access change is being verified', async () => {
  const stream = pushedStream();
  let answer!: (value: { enrolled: boolean; authenticated: boolean }) => void;
  const state = vi.fn().mockResolvedValueOnce({ enrolled: true, authenticated: false })
    .mockImplementationOnce(() => new Promise(resolve => { answer = resolve; }))
    .mockRejectedValue(lapsed('removed'));
  const login = vi.fn();
  const view = renderView({ state, watch: stream.watch, enroll: vi.fn(), login });
  await screen.findByRole('button', { name: 'Sign in with passkey' });
  stream.push('available');
  stream.push('removed');
  expect(view.signIn().disabled).toBe(true);
  fireEvent.click(view.signIn());
  expect(login).not.toHaveBeenCalled();
  await act(async () => { answer({ enrolled: true, authenticated: false }); });
});

it('renders protected native recovery instead of passkey access and returns only after a fresh access read', async () => {
  const operation = '22222222-2222-4222-8222-222222222222';
  const generation = '11111111-1111-4111-8111-111111111111';
  const attempt = '0198a1b2-c3d4-7300-8000-000000000063';
  const auth = { operation_id: operation, browser_operation_id: null, state: 'selection-required' as const,
    choices: { default: true, google: false, 'microsoft-personal': false, telegram: false, tiktok: false },
    actions: { select: true, cancel: false, open_browser: false, switch_account: false }, authentication_attempt: null };
  const initial: DesktopSnapshot = { journey_id: operation, generation, revision: 0, facts: {}, local_ready: false, full_ready: false,
    analysis_readiness: { availability: 'unauthorized', creator_account_id: null, value: null }, next_task: 'account-connection',
    authority_recovery: 'available', allowed_actions: ['account.connect'], pending_operations: [] };
  let desktop: DesktopState = { snapshot: initial, connected: true, pending: null, error: null };
  const listeners = new Set<() => void>();
  const recoveryApi: AuthorityRecoveryApi = { getState: () => desktop,
    subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener); }; }, act: vi.fn(async () => {}) };
  const show = (snapshot: DesktopSnapshot, connected = true) => act(() => {
    desktop = { ...desktop, connected, snapshot }; for (const listener of listeners) listener();
  });
  const onAuthenticated = vi.fn();
  const api = { state: vi.fn(async () => ({ enrolled: true, authenticated: false })), enroll: vi.fn(), login: vi.fn() };
  render(<ThemeProvider theme={theme}><WebAuthnAccessView api={api} recoveryApi={recoveryApi} onAuthenticated={onAuthenticated} /></ThemeProvider>);
  expect(await screen.findByRole('heading', { name: 'Sign in to open your analytics' })).toBeTruthy();
  expect(screen.getByText('Use the same sign-in as before.')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Sign in with passkey' })).toBeNull();
  expect(recoveryApi.act).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Sign in', exact: true }));
  await waitFor(() => expect(recoveryApi.act).toHaveBeenCalledWith('account.connect'));
  show({ ...initial, allowed_actions: [] });
  expect(screen.queryByRole('button', { name: 'Sign in', exact: true })).toBeNull();
  show({ ...initial, authority_recovery: 'preparing', allowed_actions: [] });
  expect(screen.queryByRole('button', { name: 'Sign in', exact: true })).toBeNull();
  show({ ...initial, authority_recovery: 'sign-in-required', authentication: auth, allowed_actions: ['auth.select'] });
  fireEvent.click(screen.getByRole('button', { name: 'Sign in', exact: true }));
  await waitFor(() => expect(recoveryApi.act).toHaveBeenCalledWith('auth.select'));
  const selected: DesktopSnapshot = { ...initial, authority_recovery: 'sign-in-required', authority_recovery_browser: 'ready',
    allowed_actions: ['auth.cancel'], authentication: { ...auth, state: 'selected',
      authentication_attempt: { authentication_attempt_id: attempt, provider: 'default', mode: 'sign-in' },
      actions: { select: false, cancel: true, open_browser: true, switch_account: false } } };
  show(selected);
  expect(screen.getByRole('button', { name: 'Cancel sign-in' }).classList.contains('MuiButton-text')).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: 'Open browser' }));
  await waitFor(() => expect(recoveryApi.act).toHaveBeenCalledWith('browser.open'));
  show({ ...selected, authority_recovery_browser: 'launch-claimed' });
  expect(screen.queryByRole('button', { name: 'Open browser' })).toBeNull();
  show({ ...selected, authority_recovery_browser: 'handed-off' });
  expect(screen.getByText('Waiting for sign-in\u2026').closest('.onboarding-action-content')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Cancel sign-in' }));
  await waitFor(() => expect(recoveryApi.act).toHaveBeenCalledWith('auth.cancel'));
  show({ ...selected, authority_recovery_browser: 'unconfirmed' });
  expect(screen.getByRole('button', { name: 'Cancel sign-in' }).classList.contains('MuiButton-contained')).toBe(true);
  expect(screen.getByText('Sign-in isn’t confirmed.').closest('.onboarding-status')?.getAttribute('data-tone')).toBe('attention');
  show({ ...initial, authority_recovery: 'sign-in-required', authentication: { ...auth, state: 'retired' }, allowed_actions: ['auth.select'] });
  expect(screen.getByRole('button', { name: 'Sign in', exact: true })).toBeTruthy();
  show({ ...initial, authority_recovery: 'restoring', allowed_actions: [] });
  expect(screen.getByText('Signed in. Getting this computer ready…').closest('.onboarding-action-content')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Sign in with passkey' })).toBeNull();
  expect(onAuthenticated).not.toHaveBeenCalled();
  const before = api.state.mock.calls.length;
  const restored = { ...initial, authority_recovery: undefined, local_ready: true, allowed_actions: [] };
  show(restored);
  await waitFor(() => expect(api.state.mock.calls.length).toBeGreaterThan(before));
  expect(await screen.findByRole('button', { name: 'Sign in with passkey' })).toBeTruthy();
  expect(onAuthenticated).not.toHaveBeenCalled();
  expect(api.enroll).not.toHaveBeenCalled();
  expect(api.login).not.toHaveBeenCalled();
});

it('shows the blocked protected recovery result while the original command is pending', async () => {
  const operationId = '22222222-2222-4222-8222-222222222222';
  const command = { operation_id: operationId, action: 'account.connect' as const, state: 'accepted' as const, reason: 'none' as const };
  const snapshot: DesktopSnapshot = { journey_id: operationId, generation: '11111111-1111-4111-8111-111111111111',
    revision: 1, facts: {}, local_ready: false, full_ready: false, analysis_readiness: { availability: 'unauthorized', creator_account_id: null, value: null },
    next_task: 'account-connection', authority_recovery: 'preparing', allowed_actions: [], pending_operations: [command] };
  let desktop: DesktopState = { snapshot, pending: operationId, pendingAction: 'account.connect', connected: true, error: null };
  const listeners = new Set<() => void>();
  const recoveryApi: AuthorityRecoveryApi = { getState: () => desktop,
    subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    act: vi.fn(async () => {}) };
  render(<ThemeProvider theme={theme}><WebAuthnAccessView recoveryApi={recoveryApi} api={{ enroll: vi.fn(), login: vi.fn() }} /></ThemeProvider>);
  expect(await screen.findByText('Preparing sign-in…')).toBeTruthy();
  act(() => {
    desktop = { ...desktop, snapshot: { ...snapshot, revision: 2, authority_recovery: 'blocked',
      pending_operations: [{ ...command, state: 'outcome_unknown', reason: 'operation_unconfirmed' }] } };
    for (const listener of listeners) listener();
  });
  expect(await screen.findByRole('heading', { name: 'Can’t open your analytics right now' })).toBeTruthy();
  expect(screen.getByText('This page updates by itself if anything changes.')).toBeTruthy();
  expect(screen.queryByText('Preparing sign-in…')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Sign in', exact: true })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Sign in with passkey' })).toBeNull();
  expect(recoveryApi.act).not.toHaveBeenCalled();
});
