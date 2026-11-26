import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import AccountView from '../src/views/AccountView';

function renderAccount(enabled: boolean) {
  const config = document.createElement('script');
  config.id = 'fastapi-config';
  config.type = 'application/json';
  config.textContent = JSON.stringify({ ACCOUNT_PASSKEY_ENABLED: enabled });
  document.head.appendChild(config);
  return render(<AccountView />);
}

afterEach(() => {
  cleanup();
  document.getElementById('fastapi-config')?.remove();
  document.querySelector('meta[name="csrf-token"]')?.remove();
  sessionStorage.removeItem('desktop-presentation');
  vi.unstubAllGlobals();
});

describe('Account', () => {
  it('shows local access and lets the user open the hosted manager in the same browser tab', () => {
    renderAccount(true);
    expect(screen.getByText('Dipsy is unlocked on this computer.')).toBeTruthy();
    const link = screen.getByRole('link', { name: 'Manage passkeys' });
    expect(link.getAttribute('href')).toBe('/account/manage-passkeys');
    expect(link.getAttribute('target')).toBeNull();
  });

  it('opens the native management action only on request and offers a retry after a launch failure', async () => {
    const invoke = vi.fn().mockRejectedValue(new Error('chrome_unavailable'));
    vi.stubGlobal('__TAURI__', { core: { invoke } });
    renderAccount(true);
    expect(invoke).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Manage passkeys' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Couldn’t open Chrome'));
    expect(invoke.mock.calls).toEqual([['desktop_account_passkeys']]);
    expect(screen.queryByRole('link', { name: 'Manage passkeys' })).toBeNull();
  });

  it('keeps account passkey controls out of a build without that profile', () => {
    renderAccount(false);
    expect(screen.queryByText('Manage passkeys')).toBeNull();
    expect(screen.queryByText('Lock Dipsy')).toBeNull();
  });

  it('retires an earlier Open before the lock request and reports a failed lock', async () => {
    const calls: string[] = [];
    vi.stubGlobal('__TAURI__', { core: { invoke: vi.fn(async (_command, { action }) => {
      calls.push(action);
      return true;
    }) } });
    sessionStorage.setItem('desktop-presentation', 'test-window');
    const meta = document.createElement('meta');
    meta.name = 'csrf-token';
    meta.content = 'current-session';
    document.head.appendChild(meta);
    const send = vi.fn(async (path: string) => {
      calls.push(path);
      return new Response(null, { status: 503 });
    });
    vi.stubGlobal('fetch', send);
    renderAccount(true);
    fireEvent.click(screen.getByRole('button', { name: 'Lock Dipsy' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Couldn’t confirm'));
    expect(calls).toEqual(['discard_passkey_unlock', '/api/v1/webauthn/lock']);
    expect(send).toHaveBeenCalledWith('/api/v1/webauthn/lock', expect.objectContaining({
      credentials: 'same-origin', method: 'POST', headers: { 'X-CSRF-Token': 'current-session' },
    }));
  });
});
