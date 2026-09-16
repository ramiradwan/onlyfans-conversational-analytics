import { Box } from '@mui/material';

import SettingsView from './SettingsView';
import { CommercialActivationControls } from '../components/CommercialActivationControls';
import { CompanionPairingControls } from '../components/CompanionPairingControls';
import { CreatorVaultControls } from '../components/CreatorVaultControls';
import type { CapabilityLicenseApi } from '../services/capabilityLicenseApi';
import type { CompanionPairingApi } from '../services/companionPairingApi';
import type { CreatorVaultApi } from '../services/creatorVaultApi';
import type { HistorySettingsApi } from '../services/historySettingsApi';

export interface SettingsWithVaultViewProps {
  historyApi?: HistorySettingsApi;
  pairingApi?: CompanionPairingApi;
  activationApi?: CapabilityLicenseApi;
  vaultApi?: CreatorVaultApi;
}

export default function SettingsWithVaultView({
  historyApi,
  pairingApi,
  activationApi,
  vaultApi,
}: SettingsWithVaultViewProps = {}) {
  return (
    <>
      <SettingsView api={historyApi} />
      <Box sx={{ maxWidth: 960, mx: 'auto', mt: 3, width: '100%' }}>
        <CompanionPairingControls api={pairingApi} />
      </Box>
      <Box sx={{ maxWidth: 960, mx: 'auto', mt: 3, width: '100%' }}>
        <CommercialActivationControls api={activationApi} />
      </Box>
      <Box sx={{ maxWidth: 960, mx: 'auto', mt: 3, width: '100%' }}>
        <CreatorVaultControls api={vaultApi} />
      </Box>
    </>
  );
}
