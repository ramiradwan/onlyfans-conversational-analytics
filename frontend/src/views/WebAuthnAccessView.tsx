import LockOutlinedIcon from '@mui/icons-material/LockOutlined';
import { Alert, Box, Button, Link, Paper, Stack, Typography } from '@mui/material';
import { useState } from 'react';

import { BRAND_INSET, BrandMark } from '../layouts/BrandMark';
import { webauthnApi, type WebAuthnApi } from '../services/webauthnApi';
import { componentTokens, effectTokens } from '../theme';
import { surfaceArrival } from '../theme/presentationMotion';

/** Browser ceremony outcomes where the person closed the prompt or let it time out. */
const CANCELLED_CEREMONIES = new Set(['NotAllowedError', 'AbortError']);

function failureMessage(cause: unknown, enroll: boolean): string {
  const name = typeof cause === 'object' && cause !== null && 'name' in cause ? cause.name : null;
  if (typeof name === 'string' && CANCELLED_CEREMONIES.has(name)) {
    return enroll
      ? 'Passkey setup was cancelled or timed out. Try again.'
      : 'Sign-in was cancelled or timed out. Try again.';
  }
  return enroll
    ? "Couldn't set up a passkey. Try again, or sign in if you've already set one up on this computer."
    : "Sign-in didn't finish. Try again, or set up a passkey if this is your first time on this computer.";
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
    if (busy) return;
    setBusy(true);
    setError(null);
    let enrolling = enroll;
    try {
      if (enroll) await api.enroll();
      enrolling = false;
      await api.login();
      onAuthenticated();
    } catch (cause) {
      setError(failureMessage(cause, enrolling));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box sx={{ color: 'text.primary', display: 'flex', flexDirection: 'column', minHeight: '100dvh', width: '100%' }}>
      <Box component="header" sx={{
        alignItems: 'center', display: 'flex', flexShrink: 0,
        height: componentTokens.shell.headerHeight,
        pl: { xs: 2, sm: `${BRAND_INSET}px` }, pr: 2,
      }}>
        <BrandMark />
      </Box>
      <Box
        component="main"
        data-journey-state="desktop.passkey_sign_in"
        sx={{
          display: 'grid', flex: 1, placeItems: { xs: 'start center', sm: 'center' },
          px: { xs: 2, sm: 4 }, pt: { xs: 2, sm: 0 },
          pb: { xs: 2, sm: `${componentTokens.shell.headerHeight}px` },
        }}
      >
        <Paper
          data-visual="passkey-card"
          sx={(theme) => ({
            maxWidth: 520,
            p: { xs: 4, sm: 5 },
            width: '100%',
            ...theme.effects.cardBorder(theme),
            ...surfaceArrival(),
            ...theme.effects.ambientGlow(theme),
          })}
        >
          <Stack spacing={3} sx={{ alignItems: { sm: 'center' }, textAlign: { sm: 'center' } }}>
            <Box aria-hidden="true" data-visual="passkey-lock" sx={{
              alignItems: 'center', display: 'flex', justifyContent: 'center',
              width: componentTokens.Passkey.tileSize, height: componentTokens.Passkey.tileSize,
              borderRadius: `${componentTokens.Passkey.tileRadius}px`,
              bgcolor: 'surface.trust.fill', color: 'surface.trust.ink',
              border: '1px solid', borderColor: 'surface.trust.border',
            }}>
              <LockOutlinedIcon sx={{ fontSize: componentTokens.Passkey.iconSize }} />
            </Box>
            <Box>
              <Typography component="h1" variant="passkeyTitle">Protect access to your messages</Typography>
              <Typography sx={{ color: 'text.secondary', mt: 1 }}>
                Use a passkey to unlock synced message history in this app. You can use your fingerprint,
                face, or device PIN.
              </Typography>
            </Box>
            {error && <Alert severity="error">{error}</Alert>}
            <Button disabled={busy} onClick={() => void authenticate(false)} size="large" variant="contained" sx={{ alignSelf: { xs: 'flex-start', sm: 'center' } }}>
              Sign in with passkey
            </Button>
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              First time on this computer?{' '}
              <Link component="button" type="button" disabled={busy} onClick={() => void authenticate(true)}
                sx={(theme) => ({
                  font: 'inherit', verticalAlign: 'baseline',
                  '&:disabled': { color: theme.vars.palette.text.disabled, cursor: 'default', textDecoration: 'none' },
                  '&:focus-visible': {
                    outline: `${effectTokens.focus.width} solid ${theme.vars.palette.primary.main}`,
                    outlineOffset: effectTokens.focus.offset,
                  },
                })}
              >
                Set up a passkey
              </Link>
            </Typography>
          </Stack>
        </Paper>
      </Box>
    </Box>
  );
}
