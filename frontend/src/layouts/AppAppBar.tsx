import MenuIcon from '@mui/icons-material/Menu';
import { AppBar, Box, IconButton, Toolbar, Typography } from '@mui/material';
import { useSyncExternalStore } from 'react';

import { ThemeToggle } from '@/components/ThemeToggle';
import { FreshnessStatus } from '@/components/ui/FreshnessStatus';
import { componentTokens } from '@/theme';
import { connectionGrace } from '@/utils/connectionGrace';
import { coverageProgressLabel } from '@/utils/dataReadiness';
import { collectionRestriction, freshnessRows, presentFreshness, viewerTimeZone, type FreshnessPresentation } from '@/utils/freshnessPresentation';
import { extensionConnection, protocolErrorText } from '@/utils/statusCopy';
import { bridgeTransportStore, type BridgeTransportState } from '@store/transportStore';

import { BRAND_INSET, BrandMark } from './BrandMark';
import { DesktopWindowControls, hasDesktopWindowControls } from './DesktopWindowControls';

// The pill's padding and glyph gutter inset its glyph, so the narrow row pulls the pill back to the content edge.
const { dotSize, iconGutter, paddingInline } = componentTokens.FreshnessStatus;
const STATUS_GLYPH_INSET = paddingInline + (iconGutter - dotSize) / 2;

function appBarFreshness(state: Readonly<BridgeTransportState>): FreshnessPresentation {
  const input = { freshness: state.catchupFreshness, bridge: state.connection,
    snapshotUsable: state.viewRevision !== null && state.readModelState === 'realtime', now: new Date(), timeZone: viewerTimeZone() };
  if (state.protocolError?.fatal) return { label: 'Action needed', title: 'Action needed', sentence: protocolErrorText(state.protocolError),
    icon: 'dot', tone: 'error' };
  // A saved catch-up result describes past observation. Current, account-
  // matched collection restrictions take precedence over a settled result.
  const agent = state.agent;
  const browser = input.bridge === 'connected' && input.snapshotUsable && state.creatorAccountId
    && agent?.creator_account_id === state.creatorAccountId && extensionConnection(agent) === 'connected' ? agent.browser : null;
  if (browser) {
    if (browser.legal_review_required) return {
      label: 'Collection consent needed', title: 'Consent needed', sentence: 'Review the updated terms to resume collection.',
      icon: 'dot', tone: 'attention', action: { label: 'Open settings', destination: 'settings' },
    };
    if (browser.site_access !== 'granted') return {
      label: 'Needs attention', title: 'Site access', sentence: 'Site access needs attention.',
      icon: 'dot', tone: 'attention', action: { label: 'Open settings', destination: 'settings' },
    };
    const reason = browser.capture === 'off' ? 'capture_off' : browser.capture === 'paused' ? 'user_paused' : null;
    if (reason) return collectionRestriction(reason);
  }
  return presentFreshness(input);
}

export function getStatusPresentation(state: Readonly<BridgeTransportState>) {
  const presentation = appBarFreshness(state);
  return { label: presentation.label, detail: presentation.sentence, color: presentation.tone === 'error' ? 'error' as const : presentation.tone === 'settled' ? 'success' as const : 'warning' as const };
}

export function getStatusRows(state: Readonly<BridgeTransportState>) {
  return [...freshnessRows(state.catchupFreshness, new Date(), viewerTimeZone()), { label: 'Message history', value: coverageProgressLabel(state.coverage) }];
}

export function AppAppBar({ headerHeight = componentTokens.shell.headerHeight, onDrawerToggle }: { headerHeight?: number; onDrawerToggle: () => void }) {
  const statusBreakpoint = hasDesktopWindowControls() ? 'md' : 'sm';
  const state = useSyncExternalStore(bridgeTransportStore.subscribe, bridgeTransportStore.getState);
  const phase = useSyncExternalStore(connectionGrace.subscribe, connectionGrace.getSnapshot);
  const slot = <FreshnessStatus freshness={state.catchupFreshness} bridge={state.connection} snapshotUsable={state.viewRevision !== null && state.readModelState === 'realtime'}
    override={appBarFreshness(state)} />;
  return <>
    <AppBar component="header" position="fixed" color="inherit" elevation={0} sx={(theme) => ({ ...theme.effects.headerBorder(theme), bgcolor: 'background.default', color: 'text.primary',
      height: { xs: headerHeight + componentTokens.FreshnessStatus.narrowRowHeight, [statusBreakpoint]: headerHeight } })}>
      <Toolbar data-desktop-drag-region disableGutters sx={{ gap: 1.5, minHeight: `${headerHeight}px !important`, pl: { xs: 2, sm: `${BRAND_INSET}px` }, pr: { xs: 2, sm: 3, lg: 4 } }}>
        <IconButton aria-label="Open navigation" aria-controls="mobile-navigation" edge="start" onClick={onDrawerToggle} sx={{ display: { sm: 'none' } }}><MenuIcon /></IconButton>
        <Box sx={{ flex: 1, minWidth: 0 }}><BrandMark /></Box>
        <Box sx={{ display: { xs: 'none', [statusBreakpoint]: 'block' } }}>{slot}</Box><ThemeToggle />
        <DesktopWindowControls />
      </Toolbar>
      <Box data-reserved-region="freshness-row" sx={{ display: { xs: 'flex', [statusBreakpoint]: 'none' }, height: componentTokens.FreshnessStatus.narrowRowHeight, alignItems: 'center', px: 2,
        '& [data-reserved-region="freshness-status"]': { ml: `-${STATUS_GLYPH_INSET}px` } }}>{slot}</Box>
    </AppBar>
    {/* The band sits under the open narrow drawer, and its text follows the page gutter. */}
    <Box data-reserved-region="issue-band" data-region-role="overlay" sx={(theme) => ({ position: 'fixed', top: { xs: headerHeight + componentTokens.FreshnessStatus.narrowRowHeight, [statusBreakpoint]: headerHeight },
      left: { xs: 0, sm: componentTokens.shell.desktopRailWidth }, right: 0, height: { xs: componentTokens.reserved.issueBand.narrow, sm: componentTokens.reserved.issueBand.wide },
      zIndex: theme.zIndex.drawer - 1, py: 1.5, px: { xs: 2, sm: 3, lg: 4 }, backgroundColor: `color-mix(in oklch, ${theme.vars.palette.background.paper} 85%, transparent)`, backdropFilter: 'blur(16px)',
      visibility: phase === 'interrupted' ? 'visible' : 'hidden', opacity: phase === 'interrupted' ? 1 : 0, transform: phase === 'interrupted' ? 'translateY(0)' : 'translateY(-8px)',
      transition: 'transform 200ms ease-out, opacity 200ms ease-out', '@media (prefers-reduced-motion: reduce)': { transition: 'none' }, pointerEvents: phase === 'interrupted' ? 'auto' : 'none' })}>
      <Box data-region-content role={phase === 'interrupted' ? 'status' : undefined}>
        <Typography variant="subtitle2" sx={{ lineHeight: '20px' }}>Connection interrupted</Typography>
        <Typography variant="body2" sx={{ lineHeight: '20px' }}>The connection to the local service was interrupted. Reconnecting…</Typography>
      </Box>
    </Box>
  </>;
}
