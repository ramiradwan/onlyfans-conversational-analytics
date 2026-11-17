import { Box, Chip, Stack, Typography } from '@mui/material';
import type { ReactNode } from 'react';

import { Panel } from './ui';
import type { BrowserSurfacePayload } from '../protocol';
import { extensionLabel, type ExtensionConnection } from '../utils/statusCopy';

/** An empty received snapshot has no distribution or activity chart to draw. */
export function OnboardingEmptyActivity({ browser, connection, desktopConnected, historyReview }: {
  browser: BrowserSurfacePayload | null;
  connection: ExtensionConnection;
  desktopConnected: boolean;
  historyReview?: ReactNode;
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
        {historyReview}
      </Box>
    </Stack>
  );
}
