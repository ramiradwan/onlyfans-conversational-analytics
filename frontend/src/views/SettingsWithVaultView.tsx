import { Box, Stack, Typography } from '@mui/material';
import type { ComponentProps } from 'react';

import SettingsView from './SettingsView';
import { CommercialActivationControls } from '../components/CommercialActivationControls';
import { CompanionPairingControls } from '../components/CompanionPairingControls';
import { CreatorVaultControls } from '../components/CreatorVaultControls';
import type { CompanionPairingApi } from '../services/companionPairingApi';
import type { CreatorVaultApi } from '../services/creatorVaultApi';
import type { HistorySettingsApi } from '../services/historySettingsApi';

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
    <Box sx={{ maxWidth: 880, pb: 3, width: '100%' }}>
      <Stack spacing={2.5}>
        <Typography component="h1" variant="h4">Settings</Typography>
        <CompanionPairingControls api={pairingApi} />
        <SettingsView api={historyApi} />
        <CommercialActivationControls api={activationApi} />
        <CreatorVaultControls api={vaultApi} />
      </Stack>
    </Box>
  );
}
