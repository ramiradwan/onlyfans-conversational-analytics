import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { OnboardingPasskeyTask } from '../src/components/OnboardingPasskeyTask';
import { theme } from '../src/theme';

const originalFonts = Object.getOwnPropertyDescriptor(document, 'fonts');
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  if (originalFonts) Object.defineProperty(document, 'fonts', originalFonts);
  else Reflect.deleteProperty(document, 'fonts');
});

function installFonts(load: unknown) {
  Object.defineProperty(document, 'fonts', { configurable: true, value: { load } });
}
function task() {
  const authenticate = vi.fn(async () => {});
  const view = render(<ThemeProvider theme={theme}><OnboardingPasskeyTask busy={false} checking={false}
    enrolled={true} error={null} authenticate={authenticate} /></ThemeProvider>);
  return { ...view, authenticate, state: () => view.container.querySelector('[data-passkey-font]')?.getAttribute('data-passkey-font') };
}
const loaded = [{ status: 'loaded' }];

describe('passkey task typography readiness', () => {
  it('reveals the task once both actual faces load, without starting authentication', async () => {
    let finish!: (faces: typeof loaded) => void;
    const pending = new Promise<typeof loaded>(resolve => { finish = resolve; });
    const load = vi.fn(() => pending);
    installFonts(load);
    const view = task();
    expect(screen.getByRole('status').textContent).toBe('Loading…');
    expect(screen.queryByRole('heading', { name: 'Sign in on this computer' })).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    await act(async () => { finish(loaded); });
    expect(view.state()).toBe('ready');
    expect(getComputedStyle(screen.getByRole('heading', { name: 'Sign in on this computer' })).visibility).toBe('visible');
    expect(view.authenticate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Sign in with passkey' }));
    expect(view.authenticate).toHaveBeenCalledTimes(1);
  });

  it.each(['empty', 'rejected', 'throws', 'missing'])('keeps the task usable with a pinned fallback when fonts are %s', async result => {
    installFonts(result === 'missing' ? undefined : vi.fn(() => {
      if (result === 'throws') throw new Error('Font loading failed');
      return result === 'empty' ? Promise.resolve([]) : Promise.reject(new Error('Font loading failed'));
    }));
    const view = task();
    await waitFor(() => expect(view.state()).toBe('fallback'));
    expect(getComputedStyle(screen.getByRole('heading', { name: 'Sign in on this computer' })).visibility).toBe('visible');
    expect(screen.getAllByRole('button', { name: 'Sign in with passkey' })).toHaveLength(1);
    expect(getComputedStyle(screen.getByRole('heading', { name: 'Sign in on this computer' })).fontFamily).toContain('Arial');
    expect(view.authenticate).not.toHaveBeenCalled();
  });

  it('keeps fallback typography and focused action after a timed-out font later succeeds', async () => {
    vi.useFakeTimers();
    let finish!: (faces: typeof loaded) => void;
    const pending = new Promise<typeof loaded>(resolve => { finish = resolve; });
    installFonts(vi.fn(() => pending));
    const view = task();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(view.state()).toBe('fallback');
    const action = screen.getByRole('button', { name: 'Sign in with passkey' });
    action.focus();
    await act(async () => { finish(loaded); });
    expect(view.state()).toBe('fallback');
    expect(document.activeElement).toBe(action);
    expect(screen.getByRole('button', { name: 'Sign in with passkey' })).toBe(action);
    expect(view.authenticate).not.toHaveBeenCalled();
  });

  it('retires the font deadline and pending completion when the task closes', async () => {
    vi.useFakeTimers();
    let finish!: (faces: typeof loaded) => void;
    const pending = new Promise<typeof loaded>(resolve => { finish = resolve; });
    installFonts(vi.fn(() => pending));
    const view = task();
    await act(async () => {});
    expect(vi.getTimerCount()).toBe(1);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => { finish(loaded); });
    expect(view.container.childElementCount).toBe(0);
    expect(view.authenticate).not.toHaveBeenCalled();
  });
});
