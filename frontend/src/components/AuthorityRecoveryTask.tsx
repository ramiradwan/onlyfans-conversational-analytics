import LockOutlinedIcon from '@mui/icons-material/LockOutlined';
import { Box, Button, Stack, Typography } from '@mui/material';
import { OnboardingWorkspace } from './OnboardingWorkspace';
import { StatusLine } from './ui/ReservedRegion';
import type { RecoveryAction, RecoveryPresentation } from '../../shared-setup/authority-recovery.js';

export function AuthorityRecoveryTask({ view, act }: { view: RecoveryPresentation; act(action: RecoveryAction): void }) {
  return <OnboardingWorkspace>
    <Stack spacing={2.5} data-journey-state="desktop.authority_recovery" aria-busy={view.tone === 'progress'}>
      <Box aria-hidden="true" sx={{ color: 'primary.main', alignSelf: 'flex-start', p: 1.5, width: 48, height: 48,
        display: 'grid', placeItems: 'center', borderRadius: '12px', bgcolor: 'surface.trust.fill' }}>
        <LockOutlinedIcon sx={{ fontSize: 24 }} />
      </Box>
      <Typography component="h1" variant="h4" sx={{ minHeight: '2.6em' }}>{view.title}</Typography>
      <Typography color="text.secondary" sx={{ minHeight: '3em', visibility: view.description ? 'visible' : 'hidden' }}>{view.description || '\u00a0'}</Typography>
      <StatusLine essential id="authority-recovery-status" text={view.status || null} tone={view.tone === 'warning' ? 'error' : 'secondary'} />
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5} sx={{ alignItems: { xs: 'stretch', sm: 'flex-start' }, minHeight: 48 }}>
        {view.action && <Button variant="contained" size="large" disabled={view.disabled}
          onClick={() => act(view.action!)}>{view.label}</Button>}
        {view.cancel && <Button variant="outlined" size="large" disabled={view.disabled}
          onClick={() => act('auth.cancel')}>Cancel sign-in</Button>}
      </Stack>
    </Stack>
  </OnboardingWorkspace>;
}
