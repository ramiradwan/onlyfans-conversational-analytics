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
      setError(cause instanceof Error ? cause.message : "Sign-in didn't finish. Try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box component="main" sx={{ display: 'grid', minHeight: '100%', placeItems: 'center', p: 3 }}>
      <Stack spacing={2} sx={{ maxWidth: 480, width: '100%' }}>
        <Typography component="h1" variant="h4">Sign in to Conversation Analytics</Typography>
        <Typography sx={{ color: 'text.secondary' }}>
          Your conversation data stays locked until you confirm it&apos;s you with a passkey, such as
          your fingerprint, face, or device PIN.
        </Typography>
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
    </Box>
  );
}
