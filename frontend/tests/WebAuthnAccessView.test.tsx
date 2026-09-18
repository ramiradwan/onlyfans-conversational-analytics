import { ThemeProvider } from '@mui/material/styles';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

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

afterEach(cleanup);

describe('WebAuthn access view', () => {

  it.each([
    [new Error('Login refused'), "Sign-in didn't finish. Try again, or set up a passkey if this is your first time on this computer."],
    [{ name: 'NotAllowedError' }, 'Sign-in was cancelled or timed out. Try again.'],
  ])('reports the login step after enrollment succeeds', async (failure, message) => {
    const api = { enroll: vi.fn(async () => {}), login: vi.fn(() => Promise.reject(failure)) };
    const view = renderView(api);
    fireEvent.click(view.enroll());
    expect((await screen.findByRole('alert')).textContent).toBe(message);
    expect(api.enroll).toHaveBeenCalledTimes(1);
    expect(api.login).toHaveBeenCalledTimes(1);
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

  it('enrolls before signing in and reports the authenticated session', async () => {
    const order: string[] = [];
    const api: WebAuthnApi = {
      enroll: vi.fn(async () => { order.push('enroll'); }),
      login: vi.fn(async () => { order.push('login'); }),
    };
    const view = renderView(api);

    fireEvent.click(view.enroll());

    await waitFor(() => expect(view.onAuthenticated).toHaveBeenCalledTimes(1));
    expect(order).toEqual(['enroll', 'login']);
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

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe("Sign-in didn't finish. Try again, or set up a passkey if this is your first time on this computer.");
    expect(view.onAuthenticated).not.toHaveBeenCalled();
  });

  it('tells the person a closed or timed-out prompt was cancelled', async () => {
    const rejection = { name: 'NotAllowedError' };
    const api: WebAuthnApi = {
      enroll: vi.fn(),
      login: vi.fn(() => Promise.reject(rejection)),
    };
    const view = renderView(api);

    fireEvent.click(view.signIn());

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe('Sign-in was cancelled or timed out. Try again.');
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

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe("Couldn't set up a passkey. Try again, or sign in if you've already set one up on this computer.");
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
    const pending = deferred();
    const login = vi.fn()
      .mockImplementationOnce(async () => { throw new Error('No passkey was selected.'); })
      .mockImplementationOnce(() => pending.promise);
    const view = renderView({ enroll: vi.fn(), login });

    fireEvent.click(view.signIn());
    expect(await screen.findByRole('alert')).not.toBeNull();

    fireEvent.click(view.signIn());
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());

    pending.resolve();
    await waitFor(() => expect(view.onAuthenticated).toHaveBeenCalledTimes(1));
  });
});
