import { Box, Stack, Typography } from '@mui/material';

import { Panel } from './ui';
import type { BrowserSurfacePayload } from '../protocol';
import { extensionLabel, type ExtensionConnection } from '../utils/statusCopy';
import { OnboardingCard, OnboardingPill, OnboardingSpot } from './ui/OnboardingCard';

/** An empty received snapshot has no distribution or activity chart to draw. */
export function DashboardConnection({ browser, connection, desktopConnected }: {
  browser: BrowserSurfacePayload | null;
  connection: ExtensionConnection;
  desktopConnected: boolean;
}) {
  const browserLabel = connection !== 'connected' ? extensionLabel(connection)
    : browser === null ? 'Not checked'
      : browser.legal_review_required || browser.site_access !== 'granted' ? 'Needs attention'
        : browser.capture === 'paused' ? 'Paused' : browser.capture === 'off' ? 'Off' : 'Connected';
  return <Panel component="section" aria-labelledby="empty-connection-title">
    <Typography component="h2" id="empty-connection-title" variant="h6">Connection</Typography>
    {[
      { label: 'Desktop app', status: desktopConnected ? 'Connected' : 'Not connected' },
      { label: 'Browser extension', status: browserLabel },
    ].map(({ label, status }) => <Stack key={label} direction="row" useFlexGap
      sx={{ gap: 1, justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap' }}>
      <Typography variant="body2">{label}</Typography>
      <OnboardingPill tone={status === 'Connected' ? 'progress' : 'neutral'}>{status}</OnboardingPill>
    </Stack>)}
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
