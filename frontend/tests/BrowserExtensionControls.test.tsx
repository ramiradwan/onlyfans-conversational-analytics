import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BrowserExtensionControls } from '../src/components/BrowserExtensionControls';
import type { BrowserSurfacePayload } from '../src/protocol';
import { createBrowserControlApi, type BrowserControlApi } from '../src/services/browserControlApi';
import { theme } from '../src/theme';
import { desktopFixture } from './desktopOnboardingFixture';

const active: BrowserSurfacePayload = {
  capture: 'active', site_access: 'granted', history_permission: 'granted',
  legal_review_required: false, reported_at: '2026-09-29T10:00:00Z',
};

function makeApi(result: 'delivered' | 'unreachable' = 'delivered'): BrowserControlApi {
  return { setCapture: vi.fn(async () => result), open: vi.fn(async () => result) };
}

function mount(props: Partial<Parameters<typeof BrowserExtensionControls>[0]> = {}) {
  const all = { api: makeApi(), onboarding: desktopFixture().api, browser: active, canManage: true, connection: 'connected' as const, ...props };
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
    expect(screen.getByText('New message collection is enabled.')).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Pause collecting' })));
    expect(api.setCapture).toHaveBeenCalledWith('pause', expect.any(AbortSignal));
    expect(screen.getByRole('button', { name: 'Pausing…' })).toHaveProperty('disabled', true);
    expect(screen.getByText('New message collection is enabled.')).toBeTruthy();
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
    expect(screen.getByText('New message collection is enabled.')).toBeTruthy();
  });

  it('sends resume to the extension review instead when the terms changed', () => {
    const { api, onboarding } = mount({ browser: { ...active, capture: 'paused', legal_review_required: true } });
    expect(screen.queryByRole('button', { name: 'Resume collecting' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Review terms' }));
    expect(onboarding.begin).toHaveBeenCalledWith('browser.access');
    expect(api.setCapture).not.toHaveBeenCalled();
  });

  it('opens the one extension page that owns a missing permission', () => {
    const { api, onboarding } = mount({ browser: { ...active, site_access: 'needs_approval', history_permission: 'missing' } });
    const site = screen.getByRole('button', { name: 'Allow in extension' });
    fireEvent.click(site);
    expect(onboarding.begin).toHaveBeenCalledWith('browser.access');
    expect(api.open).not.toHaveBeenCalled();
    expect(screen.queryByText('Message history access (optional)')).toBeNull();
  });

  it('explains where to act when the extension is disconnected', () => {
    mount({ browser: { ...active, site_access: 'needs_approval' }, connection: 'offline' });
    expect(screen.queryByRole('button', { name: 'Allow in extension' })).toBeNull();
    // One caption explains where to act, instead of repeating it on every row.
    expect(screen.queryByText('Change these in the browser where the extension is installed.')).toBeNull();
    expect(screen.getByRole('button', { name: 'Pause collecting' })).toBeTruthy();
  });

  it('shows status without controls to operators', () => {
    mount({ canManage: false, browser: { ...active, site_access: 'needs_approval' } });
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(screen.getByText('New message collection is enabled.')).toBeTruthy();
  });

  it('waits for a connected extension to report its settings', () => {
    mount({ browser: null });
    expect(screen.getByText('Waiting for extension settings.')).toBeTruthy();
    expect(screen.getByText("Site access hasn't been checked.")).toBeTruthy();
    expect(screen.queryByText('Waiting for browser permissions.')).toBeNull();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });

  it('retains the compact facts rows while the connection is missing without offering stale controls', () => {
    render(
      <ThemeProvider theme={theme}>
        <BrowserExtensionControls api={makeApi()} browser={null} canManage connection="offline" />
      </ThemeProvider>,
    );
    expect(screen.getAllByRole('heading', { level: 3 }).map(node => node.textContent)).toEqual(['New messages', 'Site access']);
    expect(screen.getByText('The browser extension is not connected.')).toBeTruthy();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });

  it('keeps the review in the paused row and gives controls distinct names from history sync', () => {
    mount({ browser: { ...active, capture: 'paused', legal_review_required: true } });
    expect(screen.getByText('Paused. Terms need review.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /^Pause$|^Resume$/ })).toBeNull();
    expect(screen.getAllByRole('heading', { level: 3 }).map((node) => node.textContent))
      .toEqual(['New messages', 'Site access']);
  });

  it('reports feedback to the shared browser section without a second empty reservation', async () => {
    const onFeedback = vi.fn();
    const { rerender } = mount({ api: makeApi('unreachable'), onFeedback });
    expect(document.querySelector('[data-reserved-region="browser-feedback"]')).toBeNull();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Pause collecting' })));
    expect(onFeedback).toHaveBeenLastCalledWith('The browser extension is not connected right now.');
    rerender({ browser: null });
    expect(onFeedback).toHaveBeenLastCalledWith(null);
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

  it('opens fixed browser destinations through Brain without Chrome APIs', async () => {
    const fetch = vi.fn(async () => response(202, { delivered: 1 }));
    const api = createBrowserControlApi({ fetch, getCsrfToken: () => 'csrf' });
    await expect(api.open('history')).resolves.toBe('delivered');
    expect(fetch).toHaveBeenCalledWith('/api/v1/companion/browser/open', expect.objectContaining({ body: '{"action":"history"}' }));
  });
});

it('retains the feedback reservation while pushed browser facts disappear', () => {
  const { rerender } = mount();
  expect(document.querySelector('[data-reserved-region="browser-feedback"]')).not.toBeNull();
  rerender({ browser: null, connection: 'offline' });
  expect(document.querySelector('[data-reserved-region="browser-feedback"]')).not.toBeNull();
  rerender({ browser: null, connection: 'connected' });
  expect(document.querySelector('[data-reserved-region="browser-feedback"]')).not.toBeNull();
});
