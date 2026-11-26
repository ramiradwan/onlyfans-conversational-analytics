import { Box, Button, Stack, Typography } from '@mui/material';
import { Link as RouterLink } from 'react-router-dom';

import { Panel } from './ui';
import type { BrowserSurfacePayload } from '../protocol';
import { extensionLabel, type ExtensionConnection } from '../utils/statusCopy';
import { OnboardingCard, OnboardingPill, OnboardingSpot } from './ui/OnboardingCard';

/** Connection summary for the dashboard. */
export function DashboardConnection({ browser, connection, desktopConnected, canViewSettings }: {
  browser: BrowserSurfacePayload | null;
  connection: ExtensionConnection;
  desktopConnected: boolean;
  canViewSettings: boolean;
}) {
  const browserLabel = connection !== 'connected' ? extensionLabel(connection)
    : browser === null ? 'Not checked'
      : browser.legal_review_required || browser.site_access !== 'granted' ? 'Needs attention'
        : browser.capture === 'paused' ? 'Paused' : browser.capture === 'off' ? 'Off' : 'Connected';
  return <Panel component="section" aria-labelledby="empty-connection-title"
    sx={{ flexDirection: { xs: 'column', md: 'row' }, alignItems: { md: 'center' },
      justifyContent: 'space-between', gap: { xs: 1.5, md: 3 }, px: { xs: 2.5, md: 3 }, py: 2 }}>
    <Typography component="h2" id="empty-connection-title" variant="subtitle1">Connection</Typography>
    <Stack direction={{ xs: 'column', sm: 'row' }} useFlexGap
      sx={{ gap: { xs: 1, sm: 3 }, flexWrap: 'wrap', alignItems: { sm: 'center' } }}>
      {[
        { label: 'Desktop app', status: desktopConnected ? 'Connected' : 'Not connected' },
        { label: 'Browser extension', status: browserLabel },
      ].map(({ label, status }) => <Stack key={label} direction="row" useFlexGap
        sx={{ gap: 1, justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap' }}>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>{label}</Typography>
        <OnboardingPill tone={status === 'Connected' ? 'progress' : 'neutral'}>{status}</OnboardingPill>
      </Stack>)}
      {canViewSettings && <Button component={RouterLink} to="/settings#browser-extension" size="small"
        aria-label="Manage browser extension" sx={{ alignSelf: 'flex-start' }}>Manage</Button>}
    </Stack>
  </Panel>;
}

export function OnboardingEmptyActivity({ waitingForMessages = false, collectionOn = false }: {
  waitingForMessages?: boolean;
  collectionOn?: boolean;
}) {
  return <Box data-journey-state={waitingForMessages ? 'desktop.waiting_for_messages' : 'desktop.no_messages_yet'} sx={{ maxWidth: 760 }}>
    <OnboardingCard title={waitingForMessages ? 'Waiting for messages' : 'No messages to show yet'}
      spot={<OnboardingSpot icon="history" />}
      body={collectionOn && 'Results will appear when Dipsy receives messages.'} />
  </Box>;
}
