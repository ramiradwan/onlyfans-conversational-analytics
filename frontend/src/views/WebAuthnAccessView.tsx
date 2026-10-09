import LockOutlinedIcon from '@mui/icons-material/LockOutlined';
import { Box, Button, Link, Paper, Stack, Typography } from '@mui/material';
import { useState } from 'react';

import { StatusLine } from '../components/ui/ReservedRegion';

import { BRAND_INSET, BrandMark } from '../layouts/BrandMark';
import { EnrollmentOutcomeError, webauthnApi, type WebAuthnApi } from '../services/webauthnApi';
import { componentTokens, effectTokens } from '../theme';
import { surfaceArrival } from '../theme/presentationMotion';

function failureMessage(enroll: boolean): string {
  // NotAllowedError does not uniquely establish cancellation, expiry or why
  // the browser refused a ceremony. State only the observed incomplete step.
  return enroll
    ? 'Passkey setup could not be confirmed.'
    : 'Sign-in did not finish.';
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
    try {
      if (enroll) await api.enroll();
      else await api.login();
      onAuthenticated();
    } catch (cause) {
      setError(cause instanceof EnrollmentOutcomeError && cause.enrolled
        ? 'Passkey created. Sign in to continue.' : failureMessage(enroll));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box sx={{ color: 'text.primary', display: 'flex', flexDirection: 'column', minHeight: '100dvh', minWidth: 0, width: '100%' }}>
      <Box component="header" sx={{
        alignItems: 'center', display: { xs: 'none', sm: 'flex' }, flexShrink: 0,
        height: componentTokens.shell.headerHeight,
        pl: `${BRAND_INSET}px`, pr: 2,
      }}>
        <BrandMark />
      </Box>
      {/* Feedback renders in its own row beneath the card, so the card keeps its position. */}
      <Box
        component="main"
        data-journey-state="desktop.passkey_sign_in"
        sx={{
          display: 'grid', flex: 1, gridTemplateColumns: 'minmax(0, 1fr)', gridTemplateRows: 'minmax(0, 1fr) auto minmax(0, 1fr)', justifyItems: 'center',
          px: { xs: 2, sm: 4 }, pt: { xs: 2, sm: 0 },
          pb: { xs: 2, sm: `${componentTokens.shell.headerHeight}px` },
        }}
      >
        <Paper
          data-visual="passkey-card"
          data-reserved-region="passkey-card"
          sx={(theme) => ({
            gridRow: 2,
            maxWidth: 520,
            minWidth: 0,
            p: { xs: 4, sm: 5 },
            width: '100%',
            ...theme.effects.cardBorder(theme),
            ...surfaceArrival(),
            transform: 'none !important',
            ...theme.effects.ambientGlow(theme),
          })}
        >
          <Stack data-region-content spacing={3} sx={{ alignItems: { sm: 'center' }, textAlign: { sm: 'center' } }}>
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
<Typography variant="body2" sx={{ color: 'text.secondary' }}>Use the browser profile where you set up this app, and choose the passkey you created for it.</Typography>
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
            <Box
              component="footer"
              data-visual="passkey-brand"
              sx={(theme) => ({
                borderTop: `${effectTokens.borders.thin} solid ${theme.vars.palette.divider}`,
                display: { sm: 'none' }, pt: 3,
                '& [data-visual="brand-tile"] + *': { display: 'block' },
                '& > .MuiStack-root': { flexWrap: 'wrap', gap: 1.25 },
                '& .MuiTypography-noWrap': { overflow: 'visible', whiteSpace: 'normal', minWidth: 'min-content', marginLeft: '0 !important', flex: '1 1 min-content' },
              })}
            >
              <BrandMark />
            </Box>
          </Stack>
        </Paper>
        {/* Keep the card centered; recovery can extend below this row into page scrolling. */}
        <Box sx={{ alignSelf: 'start', gridRow: 3, height: 0, minHeight: 0, maxWidth: 520, width: '100%', mt: 2 }}>
          <StatusLine essential id="passkey-feedback" size={{ xs: componentTokens.Passkey.feedback.narrow, sm: componentTokens.Passkey.feedback.wide }} text={error} tone="error" />
        </Box>
      </Box>
    </Box>
  );
}
