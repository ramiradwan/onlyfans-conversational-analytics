import { Box, Stack, Typography } from '@mui/material';
import { type ReactNode, useRef, useSyncExternalStore } from 'react';

import { CommercialActivationControls } from './CommercialActivationControls';
import { CompanionPairingControls } from './CompanionPairingControls';
import { journeyFromHash, onboardingView, subscribeOnboarding } from '../services/onboardingSession';

/** Keep the remaining local requirements in the workspace after the app restart. */
export function OnboardingContinuation({ children }: { children: ReactNode }) {
  const state = useSyncExternalStore(subscribeOnboarding, onboardingView, onboardingView);
  const complete = useRef(false);
  const brain = state?.sources.brain;
  const journeyId = journeyFromHash(window.location.hash);
  if (brain?.certain && brain.snapshot && !brain.snapshot.pending_operation
    && brain.snapshot.journey_id === journeyId
    && ['installation', 'enrollment', 'pairing', 'activation'].every(
      (fact) => brain.snapshot!.facts[fact] === 'verified',
    )) complete.current = true;
  if (!journeyId || complete.current) return children;
  return (
    <Box sx={{ height: '100%', overflowY: 'auto', p: { xs: 2, sm: 3 } }}>
      <Stack spacing={3} sx={{ maxWidth: 880, mx: 'auto' }}>
        <Typography component="h1" variant="h4">Finish setup</Typography>
        <CompanionPairingControls />
        <CommercialActivationControls />
      </Stack>
    </Box>
  );
}
