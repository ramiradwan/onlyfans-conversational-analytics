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
const COLLECTING = 'New messages are saved as they arrive.';
const PAUSED = 'Paused.';

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

const collect = () => screen.getByRole<HTMLInputElement>('switch', { name: 'Collect new messages' });

beforeEach(() => vi.useFakeTimers());
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('browser extension controls', () => {
  it('shows only pushed state and settles a pause when the extension reports it', async () => {
    const { api, rerender } = mount();
    expect(screen.getByText(COLLECTING)).toBeTruthy();
    expect(collect().checked).toBe(true);
    await act(async () => fireEvent.click(collect()));
    expect(api.setCapture).toHaveBeenCalledWith('pause', expect.any(AbortSignal));
    // The switch keeps the confirmed state and is held while the extension applies the change.
    expect(collect().checked).toBe(true);
    expect(collect()).toHaveProperty('disabled', true);
    expect(screen.getByText('Pausing…')).toBeTruthy();
    rerender({ browser: { ...active, capture: 'paused' } });
    expect(screen.getByText(PAUSED)).toBeTruthy();
    expect(collect().checked).toBe(false);
    expect(collect()).toHaveProperty('disabled', false);
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('reports an extension that never confirms the change', async () => {
    mount();
    await act(async () => fireEvent.click(collect()));
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(screen.getByRole('alert').textContent).toContain("hasn't been confirmed");
    expect(collect()).toHaveProperty('disabled', true);
    expect(collect().checked).toBe(true);
  });

  it('reports an unreachable extension without changing the shown state', async () => {
    mount({ api: makeApi('unreachable') });
    await act(async () => fireEvent.click(collect()));
    expect(screen.getByRole('alert').textContent).toContain('not connected right now');
    expect(screen.getByText(COLLECTING)).toBeTruthy();
    expect(collect().checked).toBe(true);
  });

  it('sends resume to the extension review instead when the terms changed', () => {
    const { api, onboarding } = mount({ browser: { ...active, capture: 'paused', legal_review_required: true }, connection: 'offline' });
    expect(screen.queryByRole('switch', { name: 'Collect new messages' })).toBeNull();
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

  it('offers browser permission controls independently of the ingestion link', () => {
    mount({ browser: { ...active, site_access: 'needs_approval' }, connection: 'offline' });
    expect(screen.getByRole('button', { name: 'Allow in extension' })).toBeTruthy();
    expect(collect()).toHaveProperty('disabled', false);
  });

  it('resumes a reporting paused browser once while ingestion is offline', async () => {
    const { api, rerender } = mount({ browser: { ...active, capture: 'paused' }, connection: 'offline' });
    expect(collect()).toHaveProperty('disabled', false);
    expect(collect().checked).toBe(false);
    await act(async () => fireEvent.click(collect()));
    await act(async () => fireEvent.click(collect()));
    expect(api.setCapture).toHaveBeenCalledTimes(1);
    expect(api.setCapture).toHaveBeenCalledWith('resume', expect.any(AbortSignal));
    expect(screen.getByText('Resuming…')).toBeTruthy();
    expect(collect().checked).toBe(false);
    rerender({ browser: { ...active } });
    expect(screen.getByText(COLLECTING)).toBeTruthy();
    expect(collect().checked).toBe(true);
  });

  it('keeps paused browser state when resume delivery is unreachable', async () => {
    const { api } = mount({ api: makeApi('unreachable'), browser: { ...active, capture: 'paused' }, connection: 'offline' });
    await act(async () => fireEvent.click(collect()));
    expect(api.setCapture).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('alert').textContent).toContain('not connected right now');
    expect(screen.getByText(PAUSED)).toBeTruthy();
    expect(collect().checked).toBe(false);
    expect(collect()).toHaveProperty('disabled', true);
  });

  it('removes browser actions when a reporting session disappears', () => {
    const { api, rerender } = mount({ browser: { ...active, capture: 'paused', site_access: 'needs_approval' }, connection: 'offline' });
    expect(collect()).toBeTruthy();
    rerender({ browser: null, connection: 'connected' });
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(screen.queryAllByRole('switch')).toHaveLength(0);
    expect(api.setCapture).not.toHaveBeenCalled();
    expect(api.open).not.toHaveBeenCalled();
  });

  it('shows status without controls to operators', async () => {
    const { api } = mount({ canManage: false, browser: { ...active, site_access: 'needs_approval' } });
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(screen.getByText(COLLECTING)).toBeTruthy();
    expect(collect().checked).toBe(true);
    expect(collect()).toHaveProperty('disabled', true);
    await act(async () => fireEvent.click(collect()));
    expect(api.setCapture).not.toHaveBeenCalled();
  });

  it('waits for a connected extension to report its settings', () => {
    mount({ browser: null });
    expect(screen.getByText('Waiting for extension settings.')).toBeTruthy();
    expect(screen.getByText("Site access hasn't been checked.")).toBeTruthy();
    expect(screen.queryByText('Waiting for browser permissions.')).toBeNull();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(screen.queryAllByRole('switch')).toHaveLength(0);
  });

  it('retains the compact facts rows while the connection is missing without offering stale controls', () => {
    render(
      <ThemeProvider theme={theme}>
        <BrowserExtensionControls api={makeApi()} browser={null} canManage connection="offline" />
      </ThemeProvider>,
    );
    expect(screen.getAllByRole('heading', { level: 3 }).map(node => node.textContent)).toEqual(['Collect new messages', 'Site access']);
    expect(screen.getByText('The browser extension is not connected.')).toBeTruthy();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(screen.queryAllByRole('switch')).toHaveLength(0);
  });

  it('keeps the review in the paused row and gives controls distinct names from history sync', () => {
    mount({ browser: { ...active, capture: 'paused', legal_review_required: true } });
    expect(screen.getByText('Paused until you review the terms.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /^Pause$|^Resume$/ })).toBeNull();
    expect(screen.queryByRole('switch', { name: 'Add older messages' })).toBeNull();
    expect(screen.getAllByRole('heading', { level: 3 }).map((node) => node.textContent))
      .toEqual(['Collect new messages', 'Site access']);
  });

  it('reports feedback to the shared browser section without a second empty reservation', async () => {
    const onFeedback = vi.fn();
    const { rerender } = mount({ api: makeApi('unreachable'), onFeedback });
    expect(document.querySelector('[data-reserved-region="browser-feedback"]')).toBeNull();
    await act(async () => fireEvent.click(collect()));
    expect(onFeedback).toHaveBeenLastCalledWith('The browser extension is not connected right now.');
    rerender({ browser: null });
    expect(onFeedback).toHaveBeenLastCalledWith(null);
  });

  it('keeps one control column width whether the row shows a switch or a review button', () => {
    const { rerender } = mount();
    const reserve = () => [...document.querySelectorAll('[data-setting-action] button[aria-hidden="true"]')].map((node) => node.textContent);
    expect(reserve()).toEqual(['Review terms', 'Open extension', 'Allow in extension']);
    rerender({ browser: { ...active, capture: 'paused', legal_review_required: true } });
    expect(reserve()).toEqual(['Review terms', 'Open extension', 'Allow in extension']);
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

it('retains a lost capture response across views until a pushed effect settles it', async () => {
  const api = makeApi();
  const first = mount({ api, creatorAccountId: 'creator-1' });
  await act(async () => fireEvent.click(collect()));
  await act(async () => vi.advanceTimersByTimeAsync(10_000));
  expect(collect()).toHaveProperty('disabled', true);
  cleanup();
  const second = mount({ api, creatorAccountId: 'creator-1' });
  expect(collect()).toHaveProperty('disabled', true);
  await act(async () => fireEvent.click(collect()));
  expect(api.setCapture).toHaveBeenCalledOnce();
  second.rerender({ browser: { ...active, capture: 'paused' } });
  expect(collect()).toHaveProperty('disabled', false);
  expect(collect().checked).toBe(false);
  expect(screen.queryByRole('alert')).toBeNull();
  expect(first.api).toBe(api);
});

it('does not carry an uncertain capture request into another creator account', async () => {
  const api = makeApi();
  const h = mount({ api, creatorAccountId: 'creator-1' });
  await act(async () => fireEvent.click(collect()));
  await act(async () => vi.advanceTimersByTimeAsync(10_000));
  h.rerender({ creatorAccountId: 'creator-2' });
  expect(collect()).toHaveProperty('disabled', false);
  expect(api.setCapture).toHaveBeenCalledOnce();
});

it('allows a new explicit capture action only after a fresh browser report following unreachable delivery', async () => {
  const h = mount({ api: makeApi('unreachable') });
  await act(async () => fireEvent.click(collect()));
  expect(collect()).toHaveProperty('disabled', true);
  h.rerender({ browser: { ...active, reported_at: '2026-09-29T10:01:00Z' } });
  expect(collect()).toHaveProperty('disabled', false);
  expect(h.api.setCapture).toHaveBeenCalledOnce();
});

it('binds capture to the reported owner revision and confirms only its matching receipt or a retired scope', async () => {
  const before = '00000000-0000-4000-8000-000000000001';
  const after = '00000000-0000-4000-8000-000000000002';
  const scope = { capture: 'active' as const, owner_scope: 'a'.repeat(64), consent_epoch: before, policy_revision: '1', receipt: null };
  const h = mount({ browser: { ...active, capture_control: scope } });
  await act(async () => fireEvent.click(collect()));
  const request = vi.mocked(h.api.setCapture).mock.calls[0][2];
  expect(request).toMatchObject({ owner_scope: 'a'.repeat(64), consent_epoch: before, policy_revision: '1', operation_id: expect.any(String) });
  h.rerender({ browser: { ...active, capture: 'paused' } });
  expect(collect()).toHaveProperty('disabled', true);
  expect(screen.getByText('Pausing…')).toBeTruthy();
  h.rerender({ browser: { ...active, capture: 'paused', capture_control: {
    ...scope, consent_epoch: after, capture: 'paused', receipt: {
      operation_id: request!.operation_id, action: 'pause', previous_consent_epoch: before, outcome: 'applied',
    },
  } } });
  expect(collect()).toHaveProperty('disabled', false);
  expect(collect().checked).toBe(false);
  expect(h.api.setCapture).toHaveBeenCalledOnce();
});

it('reports the exact scoped refusal without claiming that resume happened', async () => {
  const epoch = '00000000-0000-4000-8000-000000000001';
  const scope = { capture: 'paused' as const, owner_scope: 'a'.repeat(64), consent_epoch: epoch, policy_revision: '1', receipt: null };
  const browser = { ...active, capture: 'paused' as const, capture_control: scope };
  const h = mount({ browser });
  await act(async () => fireEvent.click(collect()));
  const request = vi.mocked(h.api.setCapture).mock.calls[0][2]!;
  h.rerender({ browser: { ...browser, capture_control: { ...scope, receipt: {
    operation_id: request.operation_id, action: 'resume', previous_consent_epoch: epoch, outcome: 'refused',
  } } } });
  expect(screen.getByRole('alert').textContent).toContain("couldn't change");
  expect(screen.getByText(PAUSED)).toBeTruthy();
  expect(collect().checked).toBe(false);
  expect(h.api.setCapture).toHaveBeenCalledOnce();
});

it('does not dispatch an unscoped action when a modern browser cannot report its current consent scope', async () => {
  const { api } = mount({ browser: { ...active, capture_control_supported: true, capture_control: null } });
  expect(collect()).toHaveProperty('disabled', true);
  await act(async () => fireEvent.click(collect()));
  expect(api.setCapture).not.toHaveBeenCalled();
});
