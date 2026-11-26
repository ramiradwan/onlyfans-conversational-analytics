import { Box, Button, Link, Typography } from '@mui/material';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';

import { OnboardingWorkspace, TrustTask } from './OnboardingWorkspace';
import { OnboardingAction, OnboardingStatus } from './ui/OnboardingCard';
import { openPasskeySettings } from '../../shared-setup/passkey-window.js';
import { expiredAccessDescription, expiredAccessTitle } from '../../shared-setup/saved-data-entry';
import type { AccessState } from '../../shared-setup/webauthn';
import { componentTokens, effectTokens } from '../theme';

const accessCopy = {
  renewing: { title: 'Renewing access on this computer', body: 'This page updates when renewal finishes.', status: 'Renewal needs an internet connection.' },
  expired: { title: expiredAccessTitle, body: expiredAccessDescription, status: null },
  removed: { title: 'Access removed from this computer', body: 'This computer can no longer open analytics.', status: null },
};

export function OnboardingPasskeyTask({
  busy, checking, enrolled, error, authenticate, statusUnavailable = false,
  readingStatus = false, access = null, sessionNotice, nativeRequired = false, nativePresentation = false, passkeyPermission = false,
  savedDataAvailable,
}: {
  savedDataAvailable?: boolean;
  nativeRequired?: boolean; nativePresentation?: boolean; passkeyPermission?: boolean;
  statusUnavailable?: boolean; readingStatus?: boolean; access?: AccessState | null;
  busy: boolean; checking: boolean; enrolled: boolean | null;
  error: string | null; sessionNotice?: string; authenticate(enroll: boolean): Promise<void>;
}) {
  const lapsed = checking || access === null ? null : accessCopy[access];
  const create = enrolled === false && !nativeRequired;
  const needsDesktop = nativeRequired && (!nativePresentation || enrolled === false);
  const canAct = !checking && !statusUnavailable && !lapsed && !needsDesktop;
  const family = checking ? 'checking' : access ?? (statusUnavailable ? 'unavailable' : create ? 'create' : 'sign-in');
  const title = lapsed?.title ?? (checking ? 'Checking this computer…' : statusUnavailable ? 'Can’t check your passkey right now'
    : needsDesktop ? (nativePresentation ? 'Finish passkey setup' : 'Open Dipsy')
      : create ? 'Protect your analytics on this computer' : 'Confirm it’s you');
  const body = lapsed?.body ?? (checking || statusUnavailable ? undefined : needsDesktop
    ? enrolled === false ? 'Finish setting up your passkey in the Dipsy desktop app.' : 'Unlock Dipsy in the desktop app.'
    : create ? 'Create a passkey. Your computer will ask you to confirm it’s you, so only you can open your analytics here.'
      : 'Your computer will ask you to confirm it’s you.');
  const status = lapsed ? lapsed.status : error ?? (statusUnavailable ? 'This page updates by itself.' : sessionNotice);
  const success = status === 'Passkey created. Sign in to continue.';
  const failed = Boolean(error && !lapsed && !success);
  const statusTone = success ? 'done' : statusUnavailable ? (failed ? 'error' : 'attention') : failed ? 'attention' : 'neutral';
  const action = useRef<HTMLButtonElement>(null);
  const pendingHeight = useRef<number | null>(null);
  const returnFocus = useRef(false);
  const begin = (enroll: boolean) => {
    pendingHeight.current = action.current?.getBoundingClientRect().height ?? null;
    void authenticate(enroll);
  };
  useLayoutEffect(() => {
    if (busy || !canAct || !returnFocus.current) return;
    returnFocus.current = false;
    if (document.activeElement === document.body) action.current?.focus();
  }, [busy, canAct]);

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
      document.fonts.load('600 24px "Inter Variable"', 'Confirm it’s you'),
    ])).then(groups => finish(groups.every(faces => faces.length > 0 && faces.every(face => face.status === 'loaded')) ? 'ready' : 'fallback'),
      () => finish('fallback'));
    return () => { current = false; clearTimeout(deadline); };
  }, []);
  return <>
    {fontState === 'loading' && <Typography role="status" data-surface-loading sx={{ position: 'fixed', top: 16, left: 16, fontFamily: 'Arial, sans-serif' }}>Loading…</Typography>}
    <Box data-passkey-font={fontState} data-passkey-task={fontState === 'loading' ? undefined : `passkey:${family}${status ? ':feedback' : ''}`}
      hidden={fontState === 'loading'}
      sx={fontState === 'fallback' ? { '& *': { fontFamily: 'Arial, sans-serif !important' } } : undefined}>
      {fontState !== 'loading' && <OnboardingWorkspace trust>
        <TrustTask data-journey-state="desktop.passkey_sign_in" spot="passkey" visual="passkey-card" family={family} busy={busy || checking || readingStatus}
          title={title} body={body} placeholder={checking} savedDataAvailable={savedDataAvailable}
          actions={canAct && <>
            <OnboardingAction swap={busy ? 'pending' : undefined}>
              {busy ? <Box role="status" sx={{
                display: 'flex', alignItems: 'center', width: '100%',
                minBlockSize: pendingHeight.current ? `${pendingHeight.current}px` : 'var(--onboarding-control-height)',
              }}>
                <OnboardingStatus tone="pending" live={false}>Waiting for your passkey…</OnboardingStatus>
              </Box>
              : <Button ref={action} disabled={readingStatus} size="large" variant="contained" sx={{
                minHeight: componentTokens.staticUi.largeControlHeight, maxWidth: '100%', overflowWrap: 'anywhere',
              }} onClick={() => {
                returnFocus.current = document.activeElement === action.current;
                begin(create);
              }}>{create ? 'Create passkey' : nativeRequired ? 'Unlock' : 'Sign in with passkey'}</Button>}
              {!busy && nativePresentation && passkeyPermission && <Button disabled={readingStatus}
                onClick={() => void openPasskeySettings()}>Open Windows settings</Button>}
            </OnboardingAction>
            {enrolled === null && !nativeRequired && <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>First time on this computer?{' '}
              <Link component="button" type="button" disabled={busy || readingStatus} onClick={() => begin(true)} sx={{
                font: 'inherit', verticalAlign: 'baseline', maxWidth: '100%', overflowWrap: 'anywhere',
                '&:focus-visible': { outline: `${effectTokens.focus.width} solid`, outlineColor: 'primary.main', outlineOffset: effectTokens.focus.offset },
              }}>Set up a passkey</Link>
            </Typography>}
          </>}
          status={<Box data-visual="passkey-feedback" data-reserved-region="passkey-feedback">
            {status && <div data-region-content style={{ paddingBlockStart: 16 }} role={failed ? 'alert' : undefined}><OnboardingStatus
              tone={statusTone} live={!failed} animate>{status}</OnboardingStatus></div>}
          </Box>} />
      </OnboardingWorkspace>}
    </Box>
  </>;
}
