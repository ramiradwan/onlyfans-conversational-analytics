import { ThemeProvider } from '@mui/material/styles';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mockFeedbackOverflow } from './feedbackGeometry';

import type { WebAuthnApi } from '../src/services/webauthnApi';
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

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

async function readFailure() {
  await waitFor(() => expect(screen.queryByRole('alert')).toBeTruthy());
  expect(screen.queryByRole('button', { name: 'Show details' })).toBeNull();
  return screen.findByRole('alert');
}

describe('WebAuthn access view', () => {

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

  it('has one primary sign-in action, a header outside the main landmark and a compact card brand', () => {
    const view = renderView({ enroll: vi.fn(), login: vi.fn() });
    const main = screen.getByRole('main');
    expect(main.querySelectorAll('[data-visual="brand-tile"]')).toHaveLength(1);
    expect(main.querySelector('[data-visual="passkey-card"] [data-visual="passkey-brand"] [data-visual="brand-tile"]')).not.toBeNull();
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
    expect(alert.closest('[data-visual="passkey-card"]')).toBeNull();
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
