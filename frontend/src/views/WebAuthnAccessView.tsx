import { Alert, Box, Button, Stack, Typography } from '@mui/material';
import { useState } from 'react';

import { webauthnApi, type WebAuthnApi } from '../services/webauthnApi';

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
      setError(cause instanceof Error ? cause.message : 'Passkey authentication could not be completed.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box component="main" sx={{ display: 'grid', minHeight: '100%', placeItems: 'center', p: 3 }}>
      <Stack spacing={2} sx={{ maxWidth: 480, width: '100%' }}>
        <Typography component="h1" variant="h4">Secure your Bridge</Typography>
        <Typography color="text.secondary">
          Use a passkey on this device to enroll or sign in to your verified Bridge account.
        </Typography>
        {error && <Alert severity="error">{error}</Alert>}
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5}>
          <Button disabled={busy} onClick={() => void authenticate(true)} variant="contained">
            Enroll this device
          </Button>
          <Button disabled={busy} onClick={() => void authenticate(false)} variant="outlined">
            Sign in with passkey
          </Button>
        </Stack>
      </Stack>
    </Box>
  );
}
