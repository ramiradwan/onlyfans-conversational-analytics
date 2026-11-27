import DesktopWindowsOutlined from '@mui/icons-material/DesktopWindowsOutlined';
import ExtensionOutlined from '@mui/icons-material/ExtensionOutlined';
import { Box, Button, Stack, Typography } from '@mui/material';
import { Link as RouterLink } from 'react-router-dom';

import { Panel } from './ui';
import type { BrowserSurfacePayload } from '../protocol';
import { componentTokens } from '../theme';
import { EXTENSION_LABELS, extensionLabel, type ExtensionConnection } from '../utils/statusCopy';
import { OnboardingCard, OnboardingPill, OnboardingSpot } from './ui/OnboardingCard';

const textInset = componentTokens.MuiButton.textInset.small;
// Both rows reserve every label either can show, so their statuses share one width.
const CONNECTION_LABELS = [...EXTENSION_LABELS, 'Not checked', 'Paused', 'Off'] as const;

/** Connection summary for the dashboard. */
export function DashboardConnection({ browser, connection, desktopConnected, canViewSettings }: {
  browser: BrowserSurfacePayload | null;
  connection: ExtensionConnection;
  desktopConnected: boolean;
  canViewSettings: boolean;
}) {
  const browserLabel: (typeof CONNECTION_LABELS)[number] = connection !== 'connected' ? extensionLabel(connection)
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
        { label: 'Desktop app', status: desktopConnected ? 'Connected' : 'Not connected', Icon: DesktopWindowsOutlined },
        { label: 'Browser extension', status: browserLabel, Icon: ExtensionOutlined },
      ].map(({ label, status, Icon }) => <Stack key={label} direction="row" useFlexGap
        sx={{ gap: 1, justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap' }}>
        <Stack direction="row" useFlexGap sx={{ gap: 0.75, alignItems: 'center', color: 'text.secondary' }}>
          <Icon aria-hidden focusable="false" sx={{ fontSize: '1.125rem' }} />
          <Typography variant="body2" sx={{ color: 'inherit' }}>{label}</Typography>
        </Stack>
        <OnboardingPill labels={CONNECTION_LABELS} tone={status === 'Connected' ? 'progress' : 'neutral'}>{status}</OnboardingPill>
      </Stack>)}
      {/* The label rather than the button padding lines up with the text, at the start or the end of the row. */}
      {canViewSettings && <Button component={RouterLink} to="/settings#browser-extension" size="small"
        aria-label="Manage browser extension" sx={{ alignSelf: 'flex-start',
          marginInlineStart: { xs: `-${textInset}px`, sm: 0 }, marginInlineEnd: { sm: `-${textInset}px` } }}>Manage</Button>}
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
