import { Box, Button, Skeleton } from '@mui/material';
import { visuallyHidden } from '@mui/utils';

import { OnboardingWorkspace, TrustTask } from './OnboardingWorkspace';
import { OnboardingAction, OnboardingStatus } from './ui/OnboardingCard';
import type { RecoveryAction, RecoveryPresentation } from '../../shared-setup/authority-recovery.js';
import { componentTokens } from '../theme';

export function AuthorityRecoveryTask({ view, act, savedDataAvailable }: {
  view: RecoveryPresentation; act(action: RecoveryAction): void; savedDataAvailable?: boolean;
}) {
  const { title, description, status, tone, action, label, cancel, cancelPrimary, disabled } = view;
  const placeholder = !title;
  const progress = tone === 'progress';
  const pending = progress && Boolean(status);
  const statusLine = status && <OnboardingStatus tone={progress ? 'pending' : tone === 'warning' ? 'attention' : tone}
    animate>{status}</OnboardingStatus>;
  const control = { minHeight: componentTokens.staticUi.largeControlHeight, maxWidth: '100%', overflowWrap: 'anywhere' };
  const family = [title, description, (action || pending) && label, cancel, cancelPrimary].join('\n');
  return <OnboardingWorkspace trust>
    <TrustTask data-journey-state="desktop.authority_recovery" visual="authority-recovery-card"
      spot="lock" family={family} busy={progress} placeholder={placeholder} arrive={!placeholder} savedDataAvailable={savedDataAvailable}
      title={placeholder ? <><span style={visuallyHidden}>Checking access</span>
        <Skeleton aria-hidden="true" animation={false} width="min(100%, 20ch)" /></> : title}
      body={placeholder ? <Skeleton aria-hidden="true" animation={false} width="min(100%, 28ch)" /> : description}
      actions={(action || cancel || pending) && <OnboardingAction>
        {pending && <Box sx={{ ...control, display: 'flex', alignItems: 'center', flex: '1 1 14rem', minWidth: 0 }}>{statusLine}</Box>}
        {action && <Button variant="contained" size="large" disabled={disabled} sx={control}
          onClick={() => act(action)}>{label}</Button>}
        {cancel && <Button variant={cancelPrimary ? 'contained' : 'text'} color={cancelPrimary ? 'primary' : 'inherit'}
          size="large" disabled={disabled} sx={cancelPrimary ? control : { ...control, color: 'text.primary' }}
          onClick={() => act('auth.cancel')}>Cancel sign-in</Button>}
      </OnboardingAction>}
      status={!pending && statusLine} />
  </OnboardingWorkspace>;
}
