import { Box, Button, Stack, Typography } from '@mui/material';
import { type ReactNode, useRef, useSyncExternalStore } from 'react';

import { CommercialActivationControls } from './CommercialActivationControls';
import { CompanionPairingControls } from './CompanionPairingControls';
import type { ActivationReturnState } from '../services/activationReturn';
import { journeyFromHash, onboardingView, subscribeOnboarding } from '../services/onboardingSession';

/** Keep the remaining local requirements in the workspace after the app restart. */
export function OnboardingContinuation({ children, activationReturn }: {
  children: ReactNode;
  activationReturn?: { state: ActivationReturnState; check(): void };
}) {
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
  const pendingActivation = brain?.certain && brain.snapshot?.journey_id === journeyId
    && brain.snapshot?.facts.activation === 'verified'
    ? 'none' : activationReturn?.state ?? 'none';
  return (
    <Box sx={{ height: '100%', overflowY: 'auto', p: { xs: 2, sm: 3 } }}>
      <Stack spacing={3} sx={{ maxWidth: 880, mx: 'auto' }}>
        <Typography component="h1" variant="h4">Finish setup</Typography>
        <CompanionPairingControls />
        {pendingActivation === 'none' ? <CommercialActivationControls /> : (
          <Stack spacing={1}>
            <Typography role="status">
              {pendingActivation === 'waiting' ? 'Finishing setup…'
                : pendingActivation === 'checking' ? 'Completing activation…' : 'Activation couldn’t be confirmed.'}
            </Typography>
            {pendingActivation === 'unconfirmed' && <Button onClick={activationReturn?.check}>Check again</Button>}
          </Stack>
        )}
      </Stack>
    </Box>
  );
}
