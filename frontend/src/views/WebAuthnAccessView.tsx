import { Alert, Box, Button, Paper, Stack, Typography } from '@mui/material';
import { useState } from 'react';

import { BrandMark } from '../layouts/BrandMark';
import { webauthnApi, type WebAuthnApi } from '../services/webauthnApi';

/** Browser ceremony outcomes where the person closed the prompt or let it time out. */
const CANCELLED_CEREMONIES = new Set(['NotAllowedError', 'AbortError']);

function failureMessage(cause: unknown, enroll: boolean): string {
  const name = typeof cause === 'object' && cause !== null && 'name' in cause ? cause.name : null;
  if (typeof name === 'string' && CANCELLED_CEREMONIES.has(name)) {
    return enroll
      ? 'Passkey setup was cancelled or timed out. Try again.'
      : 'Sign-in was cancelled or timed out. Try again.';
  }
  return enroll ? "Couldn't set up a passkey. Try again." : "Sign-in didn't finish. Try again.";
}

interface WebAuthnAccessViewProps {
  api?: WebAuthnApi;
  onAuthenticated?: () => void;
}

export function WebAuthnAccessView({
  api = webauthnApi,
  onAuthenticated = () => window.location.reload(),
}: WebAuthnAccessViewProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const authenticate = async (enroll: boolean) => {
    setBusy(true);
    setError(null);
    try {
      if (enroll) await api.enroll();
      await api.login();
      onAuthenticated();
    } catch (cause) {
      setError(failureMessage(cause, enroll));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box component="main" sx={{ display: 'grid', minHeight: '100%', placeItems: 'center', p: { xs: 2, sm: 4 } }}>
      <Paper
        sx={(theme) => ({
          maxWidth: 520,
          p: { xs: 4, sm: 5 },
          width: '100%',
          ...theme.effects.cardBorder(theme),
          ...theme.effects.ambientGlow(theme),
        })}
      >
        <Stack spacing={3}>
          <BrandMark />
          <Box>
            <Typography component="h1" variant="h4">Sign in to Conversation Analytics</Typography>
            <Typography sx={{ color: 'text.secondary', mt: 1 }}>
              Your conversation data stays locked until you confirm it&apos;s you with a passkey, such as
              your fingerprint, face, or device PIN.
            </Typography>
          </Box>
          {error && <Alert severity="error">{error}</Alert>}
          <Box>
            <Button disabled={busy} onClick={() => void authenticate(false)} size="large" variant="contained">
              Sign in with passkey
            </Button>
          </Box>
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>
            First time on this computer?{' '}
            <Button disabled={busy} onClick={() => void authenticate(true)} size="small" variant="text">
              Set up a passkey
            </Button>
          </Typography>
        </Stack>
      </Paper>
    </Box>
  );
}
