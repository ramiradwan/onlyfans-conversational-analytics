import { Box, Paper, Stack, Typography, styled } from '@mui/material';
import type { ComponentProps } from 'react';

import SettingsView from './SettingsView';
import { CommercialActivationControls } from '../components/CommercialActivationControls';
import { CompanionPairingControls } from '../components/CompanionPairingControls';
import { CreatorVaultControls } from '../components/CreatorVaultControls';
import { ReservedSection } from '../components/ui/ReservedRegion';
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
  browserApi?: ComponentProps<typeof CompanionPairingControls>['browserApi'];
  port?: ComponentProps<typeof CompanionPairingControls>['port'];
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
  browserApi,
  port,
}: SettingsWithVaultViewProps = {}) {
  return (
    <Box data-scroll-container sx={{ flex: 1, minHeight: 0, overflowY: 'auto', scrollbarGutter: 'stable', pb: 3 }}>
      <Stack data-visual="settings-frame" spacing={3} sx={{ maxWidth: 880, mx: 'auto', width: '100%' }}>
        <Typography component="h1" variant="h4">Settings</Typography>
        <SettingsSurface elevation={0}>
          <Box id="browser-extension" data-settings-section><ReservedSection grow id="settings-browser" size={{ xs: 960, sm: 720 }} label="Loading settings…"><CompanionPairingControls api={pairingApi} browserApi={browserApi} port={port} /></ReservedSection></Box>
          <Box id="full-analytics" data-settings-section><ReservedSection grow id="settings-activation" size={{ xs: 352, sm: 240 }} label="Loading settings…"><CommercialActivationControls api={activationApi} /></ReservedSection></Box>
          <Box id="message-history" data-settings-section><ReservedSection id="settings-history" size={{ xs: 600, sm: 480 }} label="Loading settings…"><SettingsView api={historyApi} /></ReservedSection></Box>
          <Box data-settings-section><ReservedSection id="settings-vault" size={{ xs: 640, sm: 520 }} label="Loading settings…"><CreatorVaultControls api={vaultApi} /></ReservedSection></Box>
        </SettingsSurface>
      </Stack>
    </Box>
  );
}
