import { Box } from '@mui/material';

import SettingsView from './SettingsView';
import { CreatorVaultControls } from '../components/CreatorVaultControls';

export default function SettingsWithVaultView() {
  return (
    <>
      <SettingsView />
      <Box sx={{ maxWidth: 960, mx: 'auto', mt: 3, width: '100%' }}>
        <CreatorVaultControls />
      </Box>
    </>
  );
}
