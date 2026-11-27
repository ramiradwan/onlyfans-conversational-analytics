import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { BrowserExtensionControls } from '../src/components/BrowserExtensionControls';
import { CompanionPairingControls } from '../src/components/CompanionPairingControls';
import { CommercialActivationControls } from '../src/components/CommercialActivationControls';
import { MetricCard } from '../src/components/analytics/MetricCard';
import * as ui from '../src/components/ui';
import type { CompanionPairingApi } from '../src/services/companionPairingApi';
import { bridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { storyWindowSources } from '../src/story-only/analyticsFixtures';
import { theme } from '../src/theme';
import { desktopFixture } from './desktopOnboardingFixture';

afterEach(() => { cleanup(); bridgeTransportStore.reset(); useUserStore.getState().actions.setUserRole(null); vi.clearAllMocks(); });

describe('onboarding presentation primitives', () => {
  it('renders only supplied content with decorative icons and trailing status', () => {
    const { OnboardingAction, OnboardingCard, OnboardingPill, OnboardingSpot, OnboardingStatus } = ui;
    expect(typeof OnboardingCard).toBe('function');
    const view = render(<OnboardingCard title="Heading" composition="trust" heading="h2"
      spot={<OnboardingSpot icon="passkey" />} body="Body"
      actions={<OnboardingAction><button type="button">Continue</button></OnboardingAction>}
      status={<OnboardingStatus tone="pending">Waiting</OnboardingStatus>}>
      <OnboardingPill tone="progress">Ready</OnboardingPill>
      <OnboardingPill tone="error">Stopped</OnboardingPill>
    </OnboardingCard>);
    const card = screen.getByRole('region', { name: 'Heading' });
    expect(screen.getByRole('heading', { name: 'Heading', level: 2 })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Continue' })).toBeTruthy();
    expect(card.lastElementChild).toBe(screen.getByRole('status'));
    expect(card.querySelectorAll('svg[aria-hidden="true"][focusable="false"]')).toHaveLength(2);
    const errorPill = screen.getByText('Stopped').closest('.onboarding-pill');
    expect(errorPill?.getAttribute('data-tone')).toBe('error');
    expect(errorPill?.firstElementChild?.getAttribute('aria-hidden')).toBe('true');
    expect(card.textContent).toBe('HeadingBodyReadyStoppedContinueWaiting');
    view.rerender(<OnboardingCard title="Heading" />);
    expect(screen.getByRole('region', { name: 'Heading' }).children).toHaveLength(1);
  });

  it('renders the opportunity metric icon in measurement jade', () => {
    const { container } = render(<ThemeProvider theme={theme}><MetricCard label="Metric" value="4"
      supportingText="Sample" icon={<svg />} tone="opportunity" windowSource={storyWindowSources.creatorMetrics} /></ThemeProvider>);
    const icon = container.querySelector<HTMLElement>('[aria-hidden="true"]')!;
    expect(getComputedStyle(icon).color).toBe('var(--bridge-palette-measurement-main)');
  });
});

describe('owner-driven onboarding controls', () => {
  it('ignores a late confirmation failure after the owner has accepted activation', async () => {
    const desktop = desktopFixture(false);
    const operationId = '11111111-1111-4111-8111-111111111111';
    desktop.publish({ snapshot: { ...desktop.api.getState().snapshot!, allowed_actions: ['activation.confirm'], decision: {
      operation_id: operationId, action: 'activate-complimentary', expires_at: '2030-01-01T00:00:00Z', offer: {
        description: 'Full analytics', offer_version_id: 'test-offer', funding: 'complimentary-pilot', pilot_credit_cost: 1,
        capability: 'analysis-run', licensed_major_version: 1, compatible_artifact_family: 'analysis-artifact', update_rights: true, fallback_major_versions: [],
      },
    } } });
    let rejectPending!: (reason: unknown) => void;
    const begin = vi.fn(() => new Promise<void>((_, reject) => { rejectPending = reject; }));
    const api = {
      readiness: vi.fn(async () => ({ schema: 'ofca-analysis-readiness/v1' as const,
        commercial_authority: 'required' as const, analysis_admission: 'blocked' as const })),
      redeem: vi.fn(),
    };
    render(<ThemeProvider theme={theme}><CommercialActivationControls api={api} onboarding={{ ...desktop.api, begin }} /></ThemeProvider>);
    fireEvent.click(await screen.findByRole('button', { name: 'Review offer' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Activate for free' }));
    expect(begin).toHaveBeenCalledExactlyOnceWith('activation.confirm');
    await act(async () => desktop.publish({ snapshot: { ...desktop.api.getState().snapshot!, revision: 1, decision: null,
      decision_status: { operation_id: operationId, state: 'accepted' }, allowed_actions: [] } }));
    expect(api.readiness).not.toHaveBeenCalled();
    await act(async () => rejectPending(new Error('Reply lost')));
    expect(begin).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'Activate for free' })).toBeNull();
    expect(screen.queryByText('Activation could not be confirmed.')).toBeNull();
    expect(screen.getByText('Checking your activation…')).toBeTruthy();
  });

  it('refreshes activation on owner commits and removes certainty when the owner disconnects', async () => {
    const desktop = desktopFixture(false);
    const api = {
      readiness: vi.fn(async () => ({ schema: 'ofca-analysis-readiness/v1' as const,
        commercial_authority: 'active' as const, analysis_admission: 'blocked' as const })),
      redeem: vi.fn(async () => ({ state: 'checking' as const })),
    };
    desktop.readiness({ schema: 'ofca-analysis-readiness/v1', commercial_authority: 'active', analysis_admission: 'blocked' });
    render(<ThemeProvider theme={theme}><CommercialActivationControls api={api} onboarding={desktop.api} /></ThemeProvider>);
    await act(async () => {});
    expect(api.readiness).not.toHaveBeenCalled();
    expect(screen.getByText("New messages aren't being analyzed")).toBeTruthy();
    await act(async () => desktop.readiness({ schema: 'ofca-analysis-readiness/v1', commercial_authority: 'active', analysis_admission: 'admitted' }));
    expect(screen.queryByText("New messages aren't being analyzed")).toBeNull();
    expect(screen.getByText('On')).toBeTruthy();
    expect(api.readiness).not.toHaveBeenCalled();
    await act(async () => desktop.publish({ connected: false }));
    expect(api.readiness).not.toHaveBeenCalled();
    expect(screen.queryByText("New messages aren't being analyzed")).toBeNull();
    expect(screen.getByText('Activation status could not be received.')).toBeTruthy();
  });

  it('settles capture from Brain-published state and ignores the retired direct extension projection', async () => {
    const api = { setCapture: vi.fn(async () => 'delivered' as const), open: vi.fn(async () => 'delivered' as const) };
    const browser = { capture: 'active' as const, site_access: 'granted' as const, history_permission: 'granted' as const,
      legal_review_required: false, reported_at: '2026-10-12T00:00:00Z' };
    const view = render(<ThemeProvider theme={theme}><BrowserExtensionControls api={api} browser={browser}
      canManage connection="connected" /></ThemeProvider>);
    const collect = () => screen.getByRole<HTMLInputElement>('switch', { name: 'Collect new messages' });
    await act(async () => fireEvent.click(collect()));
    expect(api.setCapture).toHaveBeenCalledExactlyOnceWith('pause', expect.any(AbortSignal));
    expect(screen.getByText('Pausing…')).toBeTruthy();
    expect(collect()).toHaveProperty('disabled', true);
    view.rerender(<ThemeProvider theme={theme}><BrowserExtensionControls api={api} browser={{ ...browser, capture: 'paused' }}
      canManage connection="connected" /></ThemeProvider>);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(collect().checked).toBe(false);
    expect(collect()).toHaveProperty('disabled', false);
  });

  it('never starts pairing from extension or Brain presentation updates', async () => {
    const desktop = desktopFixture();
    desktop.publish({ snapshot: { ...desktop.api.getState().snapshot!, allowed_actions: ['browser.pair'] } });
    useUserStore.getState().actions.setUserRole('operator');
    bridgeTransportStore.bindAccount('creator-1');
    bridgeTransportStore.setConnection('connected');
    const api = { pins: vi.fn(async () => []), open: vi.fn(), change: vi.fn(), get: vi.fn(),
      revoke: vi.fn(), confirmVerified: vi.fn() } as unknown as CompanionPairingApi;
    render(<ThemeProvider theme={theme}><CompanionPairingControls api={api} onboarding={desktop.api} /></ThemeProvider>);
    await act(async () => {});
    await act(async () => desktop.publish());
    expect(api.open).not.toHaveBeenCalled();
    expect(desktop.api.begin).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Connect extension' })));
    expect(desktop.api.begin).toHaveBeenCalledExactlyOnceWith('browser.pair');
    expect(api.open).not.toHaveBeenCalled();
  });
});
