import LockOutlinedIcon from '@mui/icons-material/LockOutlined';
import { Box, Button, Link, Stack, Typography } from '@mui/material';
import { useEffect, useState } from 'react';

import { OnboardingWorkspace } from './OnboardingWorkspace';
import type { AccessState } from '../../shared-setup/webauthn';
import { effectTokens } from '../theme';
import { StatusLine } from './ui/ReservedRegion';

// No action here can restore access. Pushed updates replace this copy.
const accessCopy: Record<AccessState, { heading: string; body: string; guidance: string }> = {
  renewing: { heading: 'Renewing access on this computer', body: 'This page updates when renewal finishes.', guidance: 'Renewal needs an internet connection.' },
  expired: { heading: 'Access on this computer expired', body: 'Access could not be renewed in time.', guidance: 'Contact support to restore access.' },
  removed: { heading: 'Access removed from this computer', body: 'This computer can no longer open analytics.', guidance: 'Contact support if you did not expect this.' },
};
const accessCopies = Object.values(accessCopy);

function Sizing({ texts }: { texts: string[] }) {
  return <>{texts.map(text => <Box component="span" key={text} aria-hidden="true" sx={{ visibility: 'hidden', gridArea: '1 / 1' }}>{text}</Box>)}</>;
}

export function OnboardingPasskeyTask({ busy, checking, enrolled, error, authenticate, statusUnavailable = false, recheck, access = null }: {
  statusUnavailable?: boolean; recheck?(): void; access?: AccessState | null;
  busy: boolean; checking: boolean; enrolled: boolean | null; error: string | null; authenticate(enroll: boolean): Promise<void>;
}) {
  const actionLabel = statusUnavailable ? 'Try again' : enrolled === false ? 'Create passkey' : 'Sign in with passkey';
  const lapsed = checking || access === null ? null : accessCopy[access];
  const [fontState, setFontState] = useState<'loading' | 'ready' | 'fallback'>(() => typeof document.fonts?.load === 'function' ? 'loading' : 'fallback');
  useEffect(() => {
    if (typeof document.fonts?.load !== 'function') return;
    let current = true;
    const finish = (result: 'ready' | 'fallback') => {
      if (!current) return;
      current = false;
      clearTimeout(deadline);
      setFontState(result);
    };
    const deadline = setTimeout(() => finish('fallback'), 5000);
    void Promise.resolve().then(() => Promise.all([
      document.fonts.load('400 16px "Inter Variable"', 'Sign in with passkey'),
      document.fonts.load('600 24px "Inter Variable"', 'Checking this computer…'),
    ])).then(groups => finish(groups.every(faces => faces.length > 0 && faces.every(face => face.status === 'loaded')) ? 'ready' : 'fallback'),
      () => finish('fallback'));
    return () => { current = false; clearTimeout(deadline); };
  }, []);
  return <>
    {fontState === 'loading' && <Typography role="status" data-surface-loading sx={{ position: 'fixed', top: 16, left: 16, fontFamily: 'Arial, sans-serif' }}>Loading…</Typography>}
    <Box data-passkey-font={fontState} aria-hidden={fontState === 'loading' ? true : undefined} inert={fontState === 'loading' ? true : undefined}
      sx={{ visibility: fontState === 'loading' ? 'hidden' : 'visible', opacity: fontState === 'loading' ? 0 : 1,
        ...(fontState === 'fallback' && { '& *': { fontFamily: 'Arial, sans-serif !important' } }) }}>
      <OnboardingWorkspace>
        <Stack spacing={2.5} data-journey-state="desktop.passkey_sign_in" data-visual="passkey-card" data-reserved-region="passkey-card" aria-busy={busy}>
          <Box data-visual="passkey-lock" aria-hidden="true" sx={{ color: 'primary.main', alignSelf: 'flex-start', p: 1.5, width: 48, height: 48, display: 'grid', placeItems: 'center', borderRadius: '12px', bgcolor: 'surface.trust.fill' }}>
            <LockOutlinedIcon sx={{ fontSize: 24 }} />
          </Box>
          <Typography component="h1" variant="h4" sx={{ minHeight: '2.6em', display: 'grid' }}>
            <Sizing texts={['Create a passkey on this computer', ...accessCopies.map(copy => copy.heading)]} />
            <Box component="span" sx={{ gridArea: '1 / 1' }}>{checking ? 'Checking this computer…' : lapsed ? lapsed.heading : statusUnavailable ? 'Passkey setup could not be checked' : enrolled === false ? 'Create a passkey on this computer' : 'Sign in on this computer'}</Box>
          </Typography>
          <Typography color="text.secondary" sx={{ display: 'grid' }}>
            <Sizing texts={['Use the passkey you created for this app.', 'Use it to open analytics on this computer.', ...accessCopies.map(copy => copy.body)]} />
            <Box component="span" sx={{ gridArea: '1 / 1', visibility: checking || statusUnavailable && !lapsed ? 'hidden' : 'visible' }}>{lapsed ? lapsed.body : enrolled === false ? 'Use it to open analytics on this computer.' : 'Use the passkey you created for this app.'}</Box>
          </Typography>
          <Button disabled={busy || checking || Boolean(lapsed)} onClick={() => statusUnavailable ? recheck?.() : void authenticate(enrolled === false)} size="large" variant="contained" sx={{ alignSelf: 'flex-start', display: 'grid', alignItems: 'start', maxWidth: '100%', visibility: lapsed ? 'hidden' : 'visible' }}>
            <Box component="span" aria-hidden="true" sx={{ visibility: 'hidden', gridArea: '1 / 1' }}>Sign in with passkey</Box>
            <Box component="span" key={actionLabel} sx={{ gridArea: '1 / 1' }}>{actionLabel}</Box>
          </Button>
          <Typography variant="body2" color="text.secondary" sx={{ visibility: !checking && !statusUnavailable && !lapsed && enrolled === null ? 'visible' : 'hidden' }}>First time on this computer?{' '}
            <Link component="button" type="button" disabled={busy} onClick={() => void authenticate(true)} sx={{ font: 'inherit', verticalAlign: 'baseline',
              '&:focus-visible': { outline: `${effectTokens.focus.width} solid`, outlineColor: 'primary.main', outlineOffset: effectTokens.focus.offset } }}>
              Set up a passkey
            </Link>
          </Typography>
          <Typography role="status" sx={{ minHeight: '1.5rem', visibility: busy ? 'visible' : 'hidden' }}>Waiting for your passkey…</Typography>
          <StatusLine essential id="passkey-feedback" text={lapsed ? lapsed.guidance : error} tone={lapsed ? 'secondary' : 'error'} />
        </Stack>
      </OnboardingWorkspace>
    </Box>
  </>;
}
