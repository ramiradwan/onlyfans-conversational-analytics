import { Box } from '@mui/material';

import SettingsView from './SettingsView';
import { CommercialActivationControls } from '../components/CommercialActivationControls';
import { CompanionPairingControls } from '../components/CompanionPairingControls';
import { CreatorVaultControls } from '../components/CreatorVaultControls';

export default function SettingsWithVaultView() {
  return (
    <>
      <SettingsView />
      <Box sx={{ maxWidth: 960, mx: 'auto', mt: 3, width: '100%' }}>
        <CompanionPairingControls />
      </Box>
      <Box sx={{ maxWidth: 960, mx: 'auto', mt: 3, width: '100%' }}>
        <CommercialActivationControls />
      </Box>
      <Box sx={{ maxWidth: 960, mx: 'auto', mt: 3, width: '100%' }}>
        <CreatorVaultControls />
      </Box>
    </>
  );
}
