import { Box, Button, Paper, Stack, Typography, styled } from '@mui/material';
import { useState, type ComponentProps } from 'react';

import SettingsView from './SettingsView';
import { CommercialActivationControls } from '../components/CommercialActivationControls';
import { CompanionPairingControls } from '../components/CompanionPairingControls';
import { CreatorVaultControls } from '../components/CreatorVaultControls';
import { Panel, SectionHeader } from '../components/ui';
import type { CompanionPairingApi } from '../services/companionPairingApi';
import type { CreatorVaultApi } from '../services/creatorVaultApi';
import type { HistorySettingsApi } from '../services/historySettingsApi';

/** One card for all Settings sections; each section's own surface gives way to a divider. */
const SettingsSurface = styled(Paper)(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.paper,
  overflow: 'hidden',
  ...theme.effects.cardBorder(theme),
  '& > .MuiPaper-root': {
    backgroundColor: 'transparent',
    backgroundImage: 'none',
    borderRadius: 0,
    boxShadow: 'none',
  },
  '& > .MuiPaper-root::before': {
    display: 'none',
  },
  '& > .MuiPaper-root + .MuiPaper-root': {
    borderTop: `1px solid ${theme.vars.palette.divider}`,
  },
  '& .MuiStack-root > .MuiButton-root:not(.MuiButton-contained)': {
    alignSelf: 'flex-start',
  },
  '&:has([data-pairing-active="true"]) > .MuiPaper-root:not(:has([data-pairing-active="true"]))': {
    display: 'none',
  },
}));

export interface SettingsWithVaultViewProps {
  historyApi?: HistorySettingsApi;
  pairingApi?: CompanionPairingApi;
  activationApi?: ComponentProps<typeof CommercialActivationControls>['api'];
  vaultApi?: CreatorVaultApi;
}

/** Settings page: sections follow the setup journey from connecting the extension to managing stored messages. */
export default function SettingsWithVaultView({
  historyApi,
  pairingApi,
  activationApi,
  vaultApi,
}: SettingsWithVaultViewProps = {}) {
  const [showStoredMessages, setShowStoredMessages] = useState(false);
  return (
    <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto', pb: 3 }}>
      <Stack data-visual="settings-frame" spacing={3} sx={{ maxWidth: 880, mx: 'auto', width: '100%' }}>
        <Typography component="h1" variant="h4">Settings</Typography>
        <SettingsSurface elevation={0}>
          <CompanionPairingControls api={pairingApi} />
          <SettingsView api={historyApi} />
          <CommercialActivationControls api={activationApi} />
          {showStoredMessages ? (
            <CreatorVaultControls api={vaultApi} />
          ) : (
            <Panel>
              <Stack spacing={2}>
                <SectionHeader
                  summary="Archive, download, or delete messages when you need to."
                  title="Stored messages"
                />
                <Box>
                  <Button onClick={() => setShowStoredMessages(true)} variant="outlined">
                    Manage stored messages
                  </Button>
                </Box>
              </Stack>
            </Panel>
          )}
        </SettingsSurface>
      </Stack>
    </Box>
  );
}
