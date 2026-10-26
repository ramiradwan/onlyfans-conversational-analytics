import { Box, Paper, Stack, Typography, styled } from '@mui/material';
import { useSyncExternalStore, type ComponentProps } from 'react';

import SettingsView from './SettingsView';
import { CommercialActivationControls } from '../components/CommercialActivationControls';
import { CompanionPairingControls } from '../components/CompanionPairingControls';
import { CreatorVaultControls } from '../components/CreatorVaultControls';
import { NlpDependencyControls } from '../components/NlpDependencyControls';
import { ReservedSection } from '../components/ui/ReservedRegion';
import type { CompanionPairingApi } from '../services/companionPairingApi';
import type { CreatorVaultApi } from '../services/creatorVaultApi';
import type { HistorySettingsApi } from '../services/historySettingsApi';
import type { NlpDependenciesApi } from '../services/nlpDependenciesApi';
import type { TextAnalysisClient } from '../services/textAnalysisClient';
import { bridgeTransportStore } from '../store/transportStore';

/** One card for all Settings sections; each section's own surface gives way to a divider. */
const SettingsSurface = styled(Paper)(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.paper,
  overflow: 'hidden',
  ...theme.effects.cardBorder(theme),
  '& > [data-settings-section] [data-surface-emphasis]': {
    backgroundColor: 'transparent',
    backgroundImage: 'none',
    borderRadius: 0,
    boxShadow: 'none',
  },
  '& > [data-settings-section] [data-surface-emphasis]::before': {
    display: 'none',
  },
  '& > [data-settings-section] + [data-settings-section]': {
    borderTop: `1px solid ${theme.vars.palette.divider}`,
  },
  '& .MuiStack-root > .MuiButton-root:not(.MuiButton-contained)': {
    alignSelf: 'flex-start',
  },
}));

export interface SettingsWithVaultViewProps {
  browserApi?: ComponentProps<typeof CompanionPairingControls>['browserApi'];
  onboardingApi?: ComponentProps<typeof CompanionPairingControls>['onboarding'];
  historyApi?: HistorySettingsApi;
  pairingApi?: CompanionPairingApi;
  activationApi?: ComponentProps<typeof CommercialActivationControls>['api'];
  vaultApi?: CreatorVaultApi;
  nlpApi?: NlpDependenciesApi;
  textAnalysisClient?: TextAnalysisClient;
}

/** Settings page: sections follow the setup journey from connecting the extension to managing stored messages. */
export default function SettingsWithVaultView({
  historyApi,
  pairingApi,
  activationApi,
  vaultApi,
  browserApi,
  onboardingApi,
  nlpApi,
  textAnalysisClient,
}: SettingsWithVaultViewProps = {}) {
  const transport = useSyncExternalStore(bridgeTransportStore.subscribe, bridgeTransportStore.getState, bridgeTransportStore.getState);
  return (
    <Box data-scroll-container sx={{ flex: 1, minHeight: 0, overflowY: 'auto', scrollbarGutter: 'stable', pb: 3 }}>
      <Stack data-visual="settings-frame" spacing={3} sx={{ maxWidth: 880, mx: 'auto', width: '100%' }}>
        <Typography component="h1" variant="h4">Settings</Typography>
        <SettingsSurface elevation={0}>
          <Box id="browser-extension" data-settings-section><ReservedSection grow id="settings-browser" size={{ xs: 0, sm: 0 }} label="Loading settings…"><CompanionPairingControls api={pairingApi} browserApi={browserApi} onboarding={onboardingApi} /></ReservedSection></Box>
          <Box id="full-analytics" data-settings-section><ReservedSection grow id="settings-activation" size={{ xs: 0, sm: 0 }} label="Loading settings…"><CommercialActivationControls api={activationApi} onboarding={onboardingApi} creatorAccountId={transport.creatorAccountId} /></ReservedSection></Box>
          <Box id="message-history" data-settings-section><ReservedSection grow id="settings-history" size={{ xs: 0, sm: 0 }} label="Loading settings…"><SettingsView api={historyApi} browserApi={browserApi} desktopApi={onboardingApi} /></ReservedSection></Box>
          <Box id="text-analysis" data-settings-section><ReservedSection grow id="settings-text-analysis" size={{ xs: 0, sm: 0 }} label="Loading settings…"><NlpDependencyControls api={nlpApi} client={textAnalysisClient} creatorAccountId={transport.creatorAccountId} /></ReservedSection></Box>
          <Box data-settings-section><ReservedSection grow id="settings-vault" size={{ xs: 0, sm: 0 }} label="Loading settings…"><CreatorVaultControls api={vaultApi} /></ReservedSection></Box>
        </SettingsSurface>
      </Stack>
    </Box>
  );
}
