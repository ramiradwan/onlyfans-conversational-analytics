import { Button, Stack, Typography } from '@mui/material';
import { type ReactNode, useRef, useSyncExternalStore } from 'react';

import { CommercialActivationControls } from './CommercialActivationControls';
import { CompanionPairingControls } from './CompanionPairingControls';
import { OnboardingWorkspace } from './OnboardingWorkspace';
import type { ActivationReturnState } from '../services/activationReturn';
import { journeyFromHash, onboardingView, subscribeOnboarding } from '../services/onboardingSession';

/** Keep the remaining local requirements in the workspace after the app restart. */
export function OnboardingContinuation({ children, activationReturn }: {
  children: ReactNode;
  activationReturn?: { state: ActivationReturnState; check(): void };
}) {
  const state = useSyncExternalStore(subscribeOnboarding, onboardingView, onboardingView);
  const complete = useRef<string | null>(null);
  const brain = state?.sources.brain;
  const journeyId = journeyFromHash(window.location.hash);
  if (complete.current !== journeyId) complete.current = null;
  if (brain?.certain && brain.snapshot && !brain.snapshot.pending_operation
    && brain.snapshot.journey_id === journeyId
    && ['installation', 'enrollment', 'pairing', 'activation'].every(
      (fact) => brain.snapshot!.facts[fact] === 'verified',
    )) complete.current = journeyId;
  if (!journeyId || complete.current === journeyId) return children;
  const pendingActivation = brain?.certain && brain.snapshot?.journey_id === journeyId
    && brain.snapshot?.facts.activation === 'verified'
    ? 'none' : activationReturn?.state ?? 'none';
  const confirmed = brain?.certain && brain.snapshot?.journey_id === journeyId && !brain.snapshot.pending_operation;
  const paired = confirmed && brain.snapshot?.facts.pairing === 'verified';
  const activated = confirmed && brain.snapshot?.facts.activation === 'verified';
  return (
    <OnboardingWorkspace steps={[
      { label: 'Connect browser', complete: Boolean(paired), current: !paired },
      { label: 'Activate Full analytics', complete: Boolean(activated), current: Boolean(paired && !activated) },
    ]}>
      <Stack spacing={3}>
        <Typography component="h1" variant="h4">{paired ? 'Turn on Full analytics' : 'Connect your browser'}</Typography>
        {!paired && <CompanionPairingControls embedded />}
        {(paired || pendingActivation !== 'none') && (pendingActivation === 'none' ? <CommercialActivationControls embedded /> : (
          <Stack spacing={1}>
            <Typography role="status">
              {pendingActivation === 'waiting' ? 'Finishing setup…'
                : pendingActivation === 'checking' ? 'Completing activation…' : 'Activation couldn’t be confirmed.'}
            </Typography>
            {pendingActivation === 'unconfirmed' && <Button onClick={activationReturn?.check}>Check again</Button>}
          </Stack>
        ))}
      </Stack>
    </OnboardingWorkspace>
  );
}
