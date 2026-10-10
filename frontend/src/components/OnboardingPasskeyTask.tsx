import LockOutlinedIcon from '@mui/icons-material/LockOutlined';
import { Box, Button, Link, Stack, Typography } from '@mui/material';

import { OnboardingWorkspace } from './OnboardingWorkspace';
import { StatusLine } from './ui/ReservedRegion';

export function OnboardingPasskeyTask({ busy, checking, enrolled, error, authenticate }: {
  busy: boolean; checking: boolean; enrolled: boolean | null; error: string | null; authenticate(enroll: boolean): Promise<void>;
}) {
  return <OnboardingWorkspace>
    <Stack spacing={2.5} data-journey-state="desktop.passkey_sign_in" data-visual="passkey-card" aria-busy={busy}>
      <Box aria-hidden="true" sx={{ color: 'primary.main', alignSelf: 'flex-start', p: 1.5, borderRadius: '12px', bgcolor: 'surface.trust.fill' }}>
        <LockOutlinedIcon />
      </Box>
      <Typography component="h1" variant="h4">{checking ? 'Checking this computer…' : enrolled === false ? 'Create a passkey on this computer' : 'Sign in on this computer'}</Typography>
      {!checking && <Typography color="text.secondary">{enrolled === false ? 'Use it to open analytics on this computer.' : 'Use the passkey you created for this app.'}</Typography>}
      <Button disabled={busy || checking} onClick={() => void authenticate(enrolled === false)} size="large" variant="contained" sx={{ alignSelf: 'flex-start' }}>
        {enrolled === false ? 'Create passkey' : 'Sign in with passkey'}
      </Button>
      {!checking && enrolled === null && <Typography variant="body2" color="text.secondary">First time on this computer?{' '}
        <Link component="button" type="button" disabled={busy} onClick={() => void authenticate(true)} sx={{ font: 'inherit', verticalAlign: 'baseline' }}>
          Set up a passkey
        </Link>
      </Typography>}
      {busy && <Typography role="status">Waiting for your passkey…</Typography>}
      <StatusLine essential id="passkey-feedback" text={error} tone="error" />
    </Stack>
  </OnboardingWorkspace>;
}
