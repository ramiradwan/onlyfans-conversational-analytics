import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mockFeedbackOverflow } from './feedbackGeometry';

import { LoginOutcomeError, EnrollmentOutcomeError, WebAuthnStatusError, type PushedAccess, type WebAuthnApi } from '../shared-setup/webauthn';
import { theme } from '../src/theme';
import { WebAuthnAccessView } from '../src/views/WebAuthnAccessView';

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

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

it('retries transient status reads within a bound without inventing enrollment', async () => {
  vi.useFakeTimers();
  const state = vi.fn().mockRejectedValue(new WebAuthnStatusError('network'));
  const api = { state, enroll: vi.fn(), login: vi.fn() };
  renderView(api);
  for (let attempt = 0; attempt < 4; attempt++) await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(state).toHaveBeenCalledTimes(4);
  expect(screen.getByRole('alert').textContent).toContain('Dipsy can’t check access on this computer right now.');
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
    [new Error('Finish response lost'), 'Passkey setup could not be confirmed.'],
    [{ name: 'NotAllowedError' }, 'Passkey setup could not be confirmed.'],
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
    expect(alert.textContent).toBe('Sign-in did not finish.');
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
    expect(alert.textContent).toBe('Sign-in did not finish.');
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
    expect(alert.textContent).toBe('Passkey setup could not be confirmed.');
    expect(api.login).not.toHaveBeenCalled();
    expect(view.onAuthenticated).not.toHaveBeenCalled();
  });

  it('refuses a second ceremony while one is in flight', async () => {
    const pending = deferred();
    const api: WebAuthnApi = { enroll: vi.fn(), login: vi.fn(() => pending.promise) };
    const view = renderView(api);

    fireEvent.click(view.signIn());

    await waitFor(() => expect(view.signIn().disabled).toBe(true));
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
    expect(view.signIn().disabled).toBe(true);
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
  expect((await readFailure()).textContent).toBe('Sign-in could not start.');
});
it('an unmounted sign-in cannot navigate when its stale success resolves', async () => {
  const pending = deferred(); let signal: AbortSignal | undefined;
  const view = renderView({ enroll: vi.fn(), login: vi.fn((incoming?: AbortSignal) => { signal = incoming; return pending.promise; }) });
  fireEvent.click(view.signIn()); cleanup();
  expect(signal?.aborted).toBe(true);
  pending.resolve(); await pending.promise;
  expect(view.onAuthenticated).not.toHaveBeenCalled();
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
    ['expired', 'Access on this computer expired', 'Contact support to restore access.'],
    ['removed', 'Access removed from this computer', 'Contact support if you did not expect this.'],
  ] as const)('names %s access with guidance and no action to retry', async (access, heading, guidance) => {
    vi.useFakeTimers();
    const api = { state: vi.fn().mockRejectedValue(lapsed(access)), enroll: vi.fn(), login: vi.fn() };
    renderView(api);
    await act(async () => { await vi.advanceTimersByTimeAsync(10 * 60_000); });
    expect(screen.getByRole('heading', { level: 1, name: heading })).toBeTruthy();
    expect(screen.getByRole('status').textContent).toBe(guidance);
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
    expect(screen.getByRole('heading', { level: 1, name: 'Sign in on this computer' })).toBeTruthy();
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
    expect(screen.getByText('Contact support if you did not expect this.')).toBeTruthy();
    expect(view.onAuthenticated).not.toHaveBeenCalled();
  });
});

it('a confirmed created passkey with no session offers sign-in rather than another creation', async () => {
  const view = renderView({ state: vi.fn(async () => ({ enrolled: false, authenticated: false })),
    enroll: vi.fn(async () => { throw new EnrollmentOutcomeError(true); }), login: vi.fn() });
  fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }));
  await screen.findByRole('button', { name: 'Sign in with passkey' });
  expect(screen.queryByRole('button', { name: 'Create passkey' })).toBeNull();
  expect(view.onAuthenticated).not.toHaveBeenCalled();
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
  expect(screen.getByRole('alert').textContent).toBe('Sign-in did not finish.');
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
