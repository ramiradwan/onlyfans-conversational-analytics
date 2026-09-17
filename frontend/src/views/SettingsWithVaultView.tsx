import { Box, Paper, Stack, Typography, styled } from '@mui/material';
import type { ComponentProps } from 'react';

import SettingsView from './SettingsView';
import { CommercialActivationControls } from '../components/CommercialActivationControls';
import { CompanionPairingControls } from '../components/CompanionPairingControls';
import { CreatorVaultControls } from '../components/CreatorVaultControls';
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
  return (
    <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto', pb: 3 }}>
      <Stack spacing={3} sx={{ maxWidth: 880, width: '100%' }}>
        <Typography component="h1" variant="h4">Settings</Typography>
        <SettingsSurface elevation={0}>
          <CompanionPairingControls api={pairingApi} />
          <SettingsView api={historyApi} />
          <CommercialActivationControls api={activationApi} />
          <CreatorVaultControls api={vaultApi} />
        </SettingsSurface>
      </Stack>
    </Box>
  );
}
