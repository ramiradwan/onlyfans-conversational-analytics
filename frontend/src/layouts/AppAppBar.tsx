import MenuIcon from '@mui/icons-material/Menu';
import { AppBar, Box, IconButton, Toolbar, Typography } from '@mui/material';
import { useSyncExternalStore } from 'react';

import { ThemeToggle } from '@/components/ThemeToggle';
import { FreshnessStatus } from '@/components/ui/FreshnessStatus';
import { componentTokens } from '@/theme';
import { connectionGrace } from '@/utils/connectionGrace';
import { coverageProgressLabel } from '@/utils/dataReadiness';
import { freshnessRows, presentFreshness, viewerTimeZone } from '@/utils/freshnessPresentation';
import { protocolErrorText } from '@/utils/statusCopy';
import { bridgeTransportStore, type BridgeTransportState } from '@store/transportStore';

import { BRAND_INSET, BrandMark } from './BrandMark';

export function getStatusPresentation(state: Readonly<BridgeTransportState>) {
  if (state.protocolError?.fatal) return { label: 'Action needed', detail: protocolErrorText(state.protocolError), color: 'error' as const };
  const presentation = presentFreshness({ freshness: state.catchupFreshness, bridge: state.connection,
    snapshotUsable: state.viewRevision !== null && state.readModelState === 'realtime', now: new Date(), timeZone: viewerTimeZone() });
  return { label: presentation.label, detail: presentation.sentence, color: presentation.tone === 'settled' ? 'success' as const : 'warning' as const };
}

export function getStatusRows(state: Readonly<BridgeTransportState>) {
  return [...freshnessRows(state.catchupFreshness, new Date(), viewerTimeZone()), { label: 'Message history', value: coverageProgressLabel(state.coverage) }];
}

export function AppAppBar({ headerHeight = componentTokens.shell.headerHeight, onDrawerToggle }: { headerHeight?: number; onDrawerToggle: () => void }) {
  const state = useSyncExternalStore(bridgeTransportStore.subscribe, bridgeTransportStore.getState);
  const phase = useSyncExternalStore(connectionGrace.subscribe, connectionGrace.getSnapshot);
  const slot = <FreshnessStatus freshness={state.catchupFreshness} bridge={state.connection} snapshotUsable={state.viewRevision !== null && state.readModelState === 'realtime'}
    override={state.protocolError?.fatal ? { label: 'Action needed', title: 'Action needed', sentence: protocolErrorText(state.protocolError), icon: 'dot', tone: 'error', action: { label: 'Reload page', destination: 'reload' } } : undefined} />;
  return <>
    <AppBar component="header" position="fixed" color="inherit" elevation={0} sx={(theme) => ({ ...theme.effects.headerBorder(theme), bgcolor: 'background.default', color: 'text.primary',
      height: { xs: headerHeight + componentTokens.FreshnessStatus.narrowRowHeight, sm: headerHeight } })}>
      <Toolbar disableGutters sx={{ gap: 1.5, minHeight: `${headerHeight}px !important`, pl: { xs: 2, sm: `${BRAND_INSET}px` }, pr: { xs: 2, sm: 3, lg: 4 } }}>
        <IconButton aria-label="Open navigation" aria-controls="mobile-navigation" edge="start" onClick={onDrawerToggle} sx={{ display: { sm: 'none' } }}><MenuIcon /></IconButton>
        <Box sx={{ flex: 1, minWidth: 0 }}><BrandMark /></Box>
        <Box sx={{ display: { xs: 'none', sm: 'block' } }}>{slot}</Box><ThemeToggle />
      </Toolbar>
      <Box data-reserved-region="freshness-row" sx={{ display: { xs: 'flex', sm: 'none' }, height: componentTokens.FreshnessStatus.narrowRowHeight, alignItems: 'center', px: 2 }}>{slot}</Box>
    </AppBar>
    <Box data-reserved-region="issue-band" data-region-role="overlay" sx={(theme) => ({ position: 'fixed', top: { xs: headerHeight + componentTokens.FreshnessStatus.narrowRowHeight, sm: headerHeight },
      left: { xs: 0, sm: componentTokens.shell.desktopRailWidth + componentTokens.shell.railInset }, right: 0, height: { xs: componentTokens.reserved.issueBand.narrow, sm: componentTokens.reserved.issueBand.wide },
      zIndex: theme.zIndex.appBar - 1, p: '12px 16px', backgroundColor: `color-mix(in oklch, ${theme.vars.palette.background.paper} 85%, transparent)`, backdropFilter: 'blur(16px)',
      visibility: phase === 'interrupted' ? 'visible' : 'hidden', opacity: phase === 'interrupted' ? 1 : 0, transform: phase === 'interrupted' ? 'translateY(0)' : 'translateY(-8px)',
      transition: 'transform 200ms ease-out, opacity 200ms ease-out', '@media (prefers-reduced-motion: reduce)': { transition: 'none' }, pointerEvents: phase === 'interrupted' ? 'auto' : 'none' })}>
      <Box data-region-content role={phase === 'interrupted' ? 'status' : undefined}>
        <Typography variant="subtitle2" sx={{ lineHeight: '20px' }}>Connection interrupted</Typography>
        <Typography variant="body2" sx={{ lineHeight: '20px' }}>New messages are paused. Keep your browser open while the extension reconnects.</Typography>
      </Box>
    </Box>
  </>;
}
