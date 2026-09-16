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
    expect(alert.textContent).toBe("Sign-in didn't finish. Try again.");
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
    expect(alert.textContent).toBe("Couldn't set up a passkey. Try again.");
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
