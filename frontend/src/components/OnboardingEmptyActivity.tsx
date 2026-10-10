import { Box, Button, Chip, Stack, Typography } from '@mui/material';
import { Link as RouterLink } from 'react-router-dom';

import { Panel } from './ui';
import type { BrowserSurfacePayload } from '../protocol';
import { extensionLabel, type ExtensionConnection } from '../utils/statusCopy';

/** An empty received snapshot has no distribution or activity chart to draw. */
export function OnboardingEmptyActivity({ browser, connection, desktopConnected }: {
  browser: BrowserSurfacePayload | null;
  connection: ExtensionConnection;
  desktopConnected: boolean;
}) {
  const browserLabel = connection !== 'connected' ? extensionLabel(connection)
    : browser === null ? 'Not checked'
      : browser.legal_review_required || browser.site_access !== 'granted' ? 'Needs attention'
        : browser.capture === 'paused' ? 'Paused' : browser.capture === 'off' ? 'Off' : 'Connected';
  return (
    <Stack spacing={3}>
      <Panel component="section" emphasis="dominant" aria-labelledby="empty-activity-title">
        <Typography component="h2" id="empty-activity-title" variant="h5">No new activity yet</Typography>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>No conversations have been received.</Typography>
      </Panel>
      <Box sx={{ display: 'grid', gap: 3, gridTemplateColumns: { xs: 'minmax(0, 1fr)', md: 'minmax(0, 1fr) minmax(0, 1fr)' } }}>
        <Panel component="section" aria-labelledby="empty-connection-title">
          <Typography component="h2" id="empty-connection-title" variant="h6">Connection</Typography>
          <Stack direction="row" spacing={2} sx={{ justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap' }}>
            <Typography variant="body2">Desktop app</Typography><Chip size="small" variant="outlined" label={desktopConnected ? 'Connected' : 'Not connected'} />
          </Stack>
          <Stack direction="row" spacing={2} sx={{ justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap' }}>
            <Typography variant="body2">Browser extension</Typography><Chip size="small" variant="outlined" label={browserLabel} />
          </Stack>
        </Panel>
        <Panel component="section" emphasis="quiet" aria-labelledby="empty-history-title">
          <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
            <Typography component="h2" id="empty-history-title" variant="h6">Add older conversations</Typography>
            <Chip size="small" variant="outlined" label="Optional" />
          </Stack>
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>Choose which conversations to add in Settings.</Typography>
          <Button component={RouterLink} to="/settings#message-history" variant="outlined" sx={{ alignSelf: 'flex-start' }}>Review</Button>
        </Panel>
      </Box>
    </Stack>
  );
}
