import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BrowserExtensionControls } from '../src/components/BrowserExtensionControls';
import type { BrowserSurfacePayload } from '../src/protocol';
import { createBrowserControlApi, type BrowserControlApi } from '../src/services/browserControlApi';
import type { ExtensionPort, ExtensionPortState } from '../src/services/extensionPort';
import { theme } from '../src/theme';

const active: BrowserSurfacePayload = {
  capture: 'active', site_access: 'granted', history_permission: 'granted',
  legal_review_required: false, reported_at: '2026-09-29T10:00:00Z',
};

function makePort(status: ExtensionPortState['status'] = 'connected') {
  const state: ExtensionPortState = { status, stage: status === 'connected' ? 'paired' : null, attempt: null };
  return {
    getState: () => state,
    subscribe: vi.fn(() => () => undefined),
    open: vi.fn(() => true), pair: vi.fn(() => true), cancel: vi.fn(() => true), retry: vi.fn(),
  } satisfies ExtensionPort;
}

function makeApi(result: 'delivered' | 'unreachable' = 'delivered'): BrowserControlApi {
  return { setCapture: vi.fn(async () => result) };
}

function mount(props: Partial<Parameters<typeof BrowserExtensionControls>[0]> = {}) {
  const all = { api: makeApi(), browser: active, canManage: true, connection: 'connected' as const, port: makePort(), ...props };
  const view = render(<ThemeProvider theme={theme}><BrowserExtensionControls {...all} /></ThemeProvider>);
  const rerender = (next: Partial<typeof all>) => view.rerender(
    <ThemeProvider theme={theme}><BrowserExtensionControls {...all} {...next} /></ThemeProvider>,
  );
  return { ...all, rerender };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('browser extension controls', () => {
  it('shows only pushed state and settles a pause when the extension reports it', async () => {
    const { api, rerender } = mount();
    expect(screen.getByText('Collecting in the browser.')).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Pause collecting' })));
    expect(api.setCapture).toHaveBeenCalledWith('pause', expect.any(AbortSignal));
    expect(screen.getByRole('button', { name: 'Pausing…' })).toHaveProperty('disabled', true);
    expect(screen.getByText('Collecting in the browser.')).toBeTruthy();
    rerender({ browser: { ...active, capture: 'paused' } });
    expect(screen.getByText('Paused. Nothing new is collected.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Resume collecting' })).toHaveProperty('disabled', false);
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('reports an extension that never confirms the change', async () => {
    mount();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Pause collecting' })));
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(screen.getByRole('alert').textContent).toContain("didn't confirm");
    expect(screen.getByRole('button', { name: 'Pause collecting' })).toHaveProperty('disabled', false);
  });

  it('reports an unreachable extension without changing the shown state', async () => {
    mount({ api: makeApi('unreachable') });
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Pause collecting' })));
    expect(screen.getByRole('alert').textContent).toContain('not connected right now');
    expect(screen.getByText('Collecting in the browser.')).toBeTruthy();
  });

  it('sends resume to the extension review instead when the terms changed', () => {
    const port = makePort();
    const { api } = mount({ browser: { ...active, capture: 'paused', legal_review_required: true }, port });
    expect(screen.queryByRole('button', { name: 'Resume collecting' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Review terms' }));
    expect(port.open).toHaveBeenCalledWith('setup');
    expect(api.setCapture).not.toHaveBeenCalled();
  });

  it('opens the one extension page that owns a missing permission', () => {
    const port = makePort();
    mount({ browser: { ...active, site_access: 'needs_approval', history_permission: 'missing' }, port });
    const [site, history] = screen.getAllByRole('button', { name: 'Allow in extension' });
    fireEvent.click(site);
    fireEvent.click(history);
    expect(port.open.mock.calls).toEqual([['access'], ['history']]);
  });

  it('explains where to act when the extension is in another browser', () => {
    mount({ browser: { ...active, site_access: 'needs_approval' }, port: makePort('absent') });
    expect(screen.queryByRole('button', { name: 'Allow in extension' })).toBeNull();
    // One caption explains where to act, instead of repeating it on every row.
    expect(screen.getAllByText('Change these in the browser where the extension is installed.')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Pause collecting' })).toBeTruthy();
  });

  it('shows status without controls to operators', () => {
    mount({ canManage: false, browser: { ...active, site_access: 'needs_approval' } });
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(screen.getByText('Collecting in the browser.')).toBeTruthy();
  });

  it('waits for a connected extension to report its settings', () => {
    mount({ browser: null });
    expect(screen.getByText('Waiting for the browser extension to report its settings.')).toBeTruthy();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });

  it('adds nothing when the connection issue above already explains the missing extension', () => {
    const { container } = render(
      <ThemeProvider theme={theme}>
        <BrowserExtensionControls api={makeApi()} browser={null} canManage connection="offline" port={makePort()} />
      </ThemeProvider>,
    );
    expect(container.textContent).toBe('');
  });

  it('keeps the review in the paused row and gives controls distinct names from history sync', () => {
    mount({ browser: { ...active, capture: 'paused', legal_review_required: true } });
    expect(screen.getByText('Paused. Review the updated terms in the extension to resume.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /^Pause$|^Resume$/ })).toBeNull();
    expect(screen.getAllByRole('heading', { level: 3 }).map((node) => node.textContent))
      .toEqual(['New messages', 'Site access', 'Message history access']);
  });
});

describe('browser control API', () => {
  const response = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });

  it('sends a CSRF-protected request and reads delivery only', async () => {
    const fetch = vi.fn(async () => response(202, { delivered: 1 }));
    const api = createBrowserControlApi({ fetch, getCsrfToken: () => 'csrf' });
    await expect(api.setCapture('resume')).resolves.toBe('delivered');
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/v1/companion/browser/capture');
    expect(init.body).toBe('{"action":"resume"}');
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBe('csrf');
  });

  it('distinguishes an unreachable extension from a malformed or refused response', async () => {
    const reply = (status: number, body: unknown) => createBrowserControlApi({
      fetch: vi.fn(async () => response(status, body)), getCsrfToken: () => 'csrf',
    }).setCapture('pause');
    await expect(reply(409, { detail: 'browser_unreachable' })).resolves.toBe('unreachable');
    await expect(reply(202, { delivered: 1, extra: true })).rejects.toMatchObject({ code: 'response' });
    await expect(reply(403, { detail: 'pairing_account_refused' })).rejects.toMatchObject({ code: 'request' });
    await expect(createBrowserControlApi({ fetch: vi.fn(), getCsrfToken: () => null }).setCapture('pause'))
      .rejects.toMatchObject({ code: 'csrf' });
  });
});
