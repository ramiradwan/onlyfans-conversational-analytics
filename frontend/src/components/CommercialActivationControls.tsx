import {
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  TextField,
  Typography,
} from '@mui/material';
import { useEffect, useId, useRef, useState, useSyncExternalStore } from 'react';

import { ProviderSignInButtons, type SignInProvider } from './ProviderSignInButtons';
import { Panel, SectionHeader, useRevealHold, type SectionStatus } from './ui';
import { StatusLine } from './ui/ReservedRegion';
import {
  capabilityLicenseApi,
  CAPABILITY_LICENSE_CONTINUATION_PATTERN,
  CapabilityLicenseApiError,
  type CapabilityLicenseApi,
} from '../services/capabilityLicenseApi';
import { desktopOnboardingApi, selectAnalysisReadiness, type DesktopOnboardingApi } from '../services/desktopOnboarding';

function safeMessage(error: unknown): string {
  return error instanceof CapabilityLicenseApiError ? error.message : "Activation couldn't be checked.";
}

/** Native offer review and explicit consent; readiness remains Brain-owned. */
export function CommercialActivationControls({
  api = capabilityLicenseApi,
  onboarding = desktopOnboardingApi,
  embedded = false,
  creatorAccountId,
}: {
  api?: CapabilityLicenseApi;
  onboarding?: DesktopOnboardingApi;
  embedded?: boolean;
  creatorAccountId?: string | null;
}) {
  const [entryError, setEntryError] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);
  const [openingAction, setOpeningAction] = useState<string | null>(null);
  const [codeOpen, setCodeOpen] = useState(false);
  const [code, setCode] = useState('');
  const [codeError, setCodeError] = useState<string | null>(null);
  const [codeBusy, setCodeBusy] = useState(false);
  const [codePending, setCodePending] = useState(false);
  const codeOperation = useRef<AbortController | null>(null);
  const codeScope = useRef<string | null>(null);
  const codeInput = useRef<HTMLInputElement | null>(null);
  const codeFormId = useId();
  const [offerOpen, setOfferOpen] = useState(true);
  const [choosingAccount, setChoosingAccount] = useState(false);
  const choicesElement = useRef<HTMLDivElement | null>(null);
  const switchElement = useRef<HTMLButtonElement | null>(null);
  const previousChoosing = useRef(false);
  const entryOperation = useRef<AbortController | null>(null);
  const desktop = useSyncExternalStore(onboarding.subscribe, onboarding.getState, onboarding.getState);
  const { value: readiness, checking, availability } = selectAnalysisReadiness(desktop, creatorAccountId);
  const error = !checking && desktop.connected && availability !== 'current' ? "Activation couldn't be checked." : null;
  const decision = desktop.snapshot?.decision?.action === 'activate-complimentary' && desktop.snapshot?.decision_status?.state !== 'accepted' ? desktop.snapshot.decision : null;
  const decisionStatus = desktop.snapshot?.decision_status?.state;
  const authentication = desktop.snapshot?.authentication;
  const allowed = desktop.snapshot?.allowed_actions || [];
  const accepted = decisionStatus === 'accepted';
  const canSelect = !accepted && authentication?.actions.select && allowed.includes('auth.select');
  const accountLabel = desktop.snapshot?.facts.authenticated_account_label;
  const canSwitch = !accepted && authentication?.state === 'admitted'
    && authentication.actions.switch_account && allowed.includes('auth.switch');
  const choosingProvider = Boolean(canSelect || (choosingAccount && canSwitch));
  const providers = (['google', 'microsoft-personal', 'default'] as const).filter(provider => authentication?.choices[provider]);
  const canConfirm = desktop.connected && decision !== null && desktop.snapshot?.allowed_actions.includes('activation.confirm');
  const activationOutcome = desktop.snapshot?.pending_operations.find(value => value.action === 'activation.review');
  const pendingActivation = desktop.snapshot?.pending_operations.some(value => value.action === 'activation.review') === true;
  const canOpen = desktop.connected && (pendingActivation || desktop.snapshot?.allowed_actions.includes('activation.review'));
  const canRedeem = desktop.connected && availability === 'current';
  const ownerScope = desktop.connected && desktop.snapshot
    ? `${desktop.snapshot.generation}:${desktop.snapshot.journey_id}:${desktop.snapshot.analysis_readiness.creator_account_id}:${creatorAccountId}` : 'unconfirmed';
  useRevealHold(checking);

  useEffect(() => () => { entryOperation.current?.abort(); codeOperation.current?.abort(); }, []);
  useEffect(() => { if (codeOpen) codeInput.current?.focus(); }, [codeOpen]);
  useEffect(() => { setChoosingAccount(false); }, [authentication?.authentication_attempt?.authentication_attempt_id]);
  useEffect(() => {
    if (choosingAccount) choicesElement.current?.querySelector('button')?.focus();
    else if (previousChoosing.current) switchElement.current?.focus();
    previousChoosing.current = choosingAccount;
  }, [choosingAccount]);

  const stopOpeningSetup = () => {
    entryOperation.current?.abort();
    entryOperation.current = null;
    setOpening(false);
    setOpeningAction(null);
    setEntryError(null);
  };

  useEffect(() => {
    if (decisionStatus === 'accepted') stopOpeningSetup();
  }, [decisionStatus]);

  const openSetup = async (action: 'activation.review' | 'activation.confirm' | 'auth.select' | 'auth.switch' = 'activation.review', provider?: SignInProvider) => {
    if (codeBusy || codePending || (entryOperation.current && !entryOperation.current.signal.aborted)) return;
    const permitted = action === 'activation.confirm' ? canConfirm : action === 'auth.select' ? canSelect
      : action === 'auth.switch' ? canSwitch : canOpen;
    if (!desktop.connected || !permitted || (provider && !providers.includes(provider))) {
      setEntryError('Full analytics setup is unavailable on this page.');
      return;
    }
    const controller = new AbortController();
    entryOperation.current = controller;
    setOpening(true);
    setOfferOpen(true);
    setOpeningAction(action);
    setEntryError(null);
    try {
      if (provider) await onboarding.begin(action, { provider });
      else await onboarding.begin(action);
      setChoosingAccount(false);
    } catch (cause) {
      if (!controller.signal.aborted) setEntryError(cause instanceof Error && cause.message === 'browser_unavailable'
        ? 'The browser action could not be confirmed.' : action === 'activation.confirm' ? 'Activation could not be confirmed.'
          : 'Sign-in could not be opened. Try again.');
    } finally {
      if (entryOperation.current === controller) {
        entryOperation.current = null;
        if (!controller.signal.aborted) { setOpening(false); setOpeningAction(null); }
      }
    }
  };

  const reopenBrowser = async () => {
    if (!desktop.connected || entryOperation.current || !authentication?.actions.open_browser || !authentication.browser_operation_id) return;
    const controller = new AbortController(); entryOperation.current = controller;
    setOpening(true); setOpeningAction('browser.open'); setEntryError(null);
    try { await onboarding.openBrowser(authentication.browser_operation_id); }
    catch { if (!controller.signal.aborted) setEntryError('Chrome could not be opened. Try again.'); }
    finally {
      if (entryOperation.current === controller) {
        entryOperation.current = null;
        if (!controller.signal.aborted) { setOpening(false); setOpeningAction(null); }
      }
    }
  };

  useEffect(() => {
    if (readiness?.commercial_authority !== 'active') return;
    stopOpeningSetup();
    codeOperation.current?.abort(); codeOperation.current = null;
    codeScope.current = null;
    setCode(''); setCodeOpen(false); setCodeBusy(false); setCodePending(false); setCodeError(null);
  }, [readiness?.commercial_authority]);

  useEffect(() => {
    if (codeScope.current === null || codeScope.current === ownerScope) return;
    codeOperation.current?.abort(); codeOperation.current = null; codeScope.current = null;
    setCodeBusy(false); setCodePending(false);
    setCodeError("Activation hasn't been confirmed yet.");
  }, [ownerScope]);

  const redeemCode = async () => {
    if (!canRedeem || codeOperation.current || opening || codePending) return;
    const value = code.trim();
    if (!CAPABILITY_LICENSE_CONTINUATION_PATTERN.test(value)) {
      setCodeError('Enter the full activation code and try again.'); return;
    }
    const controller = new AbortController(); codeOperation.current = controller;
    codeScope.current = ownerScope;
    setCodeBusy(true); setCodeError(null);
    try {
      await api.redeem(value, controller.signal);
      if (!controller.signal.aborted) setCodePending(true);
    } catch (cause) {
      if (!controller.signal.aborted) { codeScope.current = null; setCodeError(safeMessage(cause)); }
    } finally {
      if (codeOperation.current === controller) {
        codeOperation.current = null; setCodeBusy(false);
      }
    }
  };

  const fullReady = readiness?.commercial_authority === 'active'
    && readiness.analysis_admission === 'admitted';
  const activeButBlocked = readiness?.commercial_authority === 'active'
    && readiness.analysis_admission === 'blocked';
  const activationRequired = readiness?.commercial_authority === 'required';
  const activationUnavailable = readiness?.commercial_authority === 'unavailable';
  const browserLaunchError = desktop.error === 'browser_unavailable' ? 'The browser action could not be confirmed.'
    : desktop.error === 'chrome_unavailable' ? 'Chrome could not be opened. Try again.' : null;
  const noticeError = entryError ?? error ?? (!codeOpen ? codeError : null) ?? browserLaunchError;
  const status: SectionStatus | null = checking && readiness === null
    ? null
    : fullReady
      ? { label: 'On', tone: 'success' }
      : activeButBlocked
        ? { label: 'Needs attention', tone: 'warning' }
        : activationRequired
          ? { label: 'Off', tone: 'default' }
          : null;

  const offer = decision?.offer;
  const feedback = noticeError || (!desktop.connected ? desktop.error === 'session_unavailable' ? 'The setup session ended. Reconnecting…' : 'Activation status could not be received.'
    : fullReady ? null : opening ? openingAction === 'activation.confirm' ? 'Confirming activation…'
      : openingAction === 'activation.review' ? 'Preparing activation…' : 'Opening Chrome…'
        : accepted ? 'Activation accepted. Checking Full analytics…'
          : choosingProvider ? choosingAccount ? 'Choose how to sign in with another account.' : 'Choose how to sign in.'
            : activationOutcome?.state === 'waiting' && activationOutcome.reason === 'activation_unavailable' ? 'Checking the activation offer.'
              : activationOutcome?.state === 'outcome_unknown' ? 'Activation has not been confirmed.'
            : authentication?.state === 'admitted' && authentication.actions.open_browser ? 'The activation offer is unavailable.'
            : authentication?.state === 'expired' ? 'This sign-in request expired.'
              : authentication?.state === 'retired' ? 'Sign-in was cancelled.'
                : authentication?.state === 'unavailable' ? 'The sign-in request could not be confirmed.'
                  : authentication?.state === 'selected' || decisionStatus === 'awaiting-authentication' ? 'Finish signing in in Chrome.'
                    : decisionStatus === 'unavailable' || activationOutcome?.reason === 'activation_unavailable' || desktop.error === 'activation_unavailable' ? 'Full analytics activation could not be prepared. Try again.'
                      : checking && !decision ? 'Checking activation…'
              : activeButBlocked ? "New messages aren't being analyzed"
                : activationUnavailable || readiness === null ? "Activation couldn't be checked." : null);

  return <Panel emphasis={embedded ? 'quiet' : 'secondary'} sx={{ gap: 1.5, minWidth: 0, ...(embedded ? { p: 0 } : {}) }}>
    {!embedded && <SectionHeader title="Full analytics" status={status}
      sx={{ minHeight: { xs: '4rem', sm: '1.5rem' }, position: 'relative', flexDirection: { xs: 'column', sm: 'row' }, rowGap: { xs: 1, sm: 0 },
        '& > :first-child': { width: '100%' }, '& h2': { pr: { xs: 0, sm: '9rem' } },
        '& > [aria-live]': { display: 'flex', position: { xs: 'static', sm: 'absolute' }, top: 0, right: 0, m: 0, width: '100%', maxWidth: '8rem', minHeight: '1.5rem',
          '& .MuiChip-root': { width: '100%', height: 'auto', minHeight: '1.5rem' },
          '& .MuiChip-label': { width: '100%', textAlign: 'left', whiteSpace: 'normal' } } }} />}
    <Box data-journey-state="desktop.full_analytics_activation">
      {(feedback || !embedded) && <Box id="activation-notice"><StatusLine essential id="activation-notice" text={offer && offerOpen ? null : feedback ?? (!embedded ? 'Tone, reply, and topic insights.' : null)}
        tone={noticeError ? 'error' : 'secondary'} /></Box>}
      {choosingProvider && !offer && !fullReady && <Box ref={choicesElement}><ProviderSignInButtons providers={providers}
        disabled={!desktop.connected || opening || codeBusy || codePending} describedBy="activation-notice"
        onSelect={provider => void openSetup(choosingAccount ? 'auth.switch' : 'auth.select', provider)} /></Box>}
      {offer && !fullReady && <>
        <Button onClick={() => setOfferOpen(true)} aria-haspopup="dialog" variant="outlined">Review activation offer</Button>
        <Dialog open={offerOpen} onClose={() => setOfferOpen(false)} aria-labelledby="activation-offer-title" fullWidth maxWidth="sm"
          slotProps={{ backdrop: { sx: theme => ({ ...theme.effects.glassmorphism(theme) }) } }}>
        <DialogTitle id="activation-offer-title">{choosingAccount ? 'Sign in to Full analytics with another account' : offer.description}</DialogTitle>
        <DialogContent>{choosingAccount ? <Stack spacing={2}>
          <Typography id="activation-switch-note" variant="body2">Sign in again to review activation with a different account.</Typography>
          <Box ref={choicesElement}><ProviderSignInButtons providers={providers}
            disabled={!desktop.connected || opening || codeBusy || codePending} describedBy="activation-switch-note"
            onSelect={provider => void openSetup('auth.switch', provider)} /></Box>
          <Button onClick={() => setChoosingAccount(false)} disabled={opening}>Back</Button>
        </Stack> : <Stack spacing={2}>
        {accountLabel && <Typography variant="body2">Full analytics account: {accountLabel}</Typography>}
        <Box component="dl" sx={{
          display: 'grid', gridTemplateColumns: { xs: '1fr', sm: 'minmax(8rem, 1fr) minmax(0, 2fr)' },
          gap: 1, m: 0, p: 2, border: 1, borderColor: 'divider', borderRadius: 1,
          '& dt': { color: 'text.secondary' }, '& dd': { m: 0, overflowWrap: 'anywhere', mb: { xs: 1, sm: 0 } },
        }}>
          <Typography component="dt">Funding</Typography><Typography component="dd">Complimentary pilot</Typography>
          <Typography component="dt">Price</Typography><Typography component="dd">Free
            <Typography component="span" variant="body2" sx={{ display: 'block', color: 'text.secondary' }}>
              Uses {new Intl.NumberFormat().format(offer.pilot_credit_cost)} {offer.pilot_credit_cost === 1 ? 'pilot credit' : 'pilot credits'}
            </Typography>
          </Typography>
          <Typography component="dt">Licensed version</Typography><Typography component="dd">{offer.licensed_major_version}.x</Typography>
          <Typography component="dt">Update rights</Typography><Typography component="dd">{offer.update_rights ? 'Included' : 'Not included'}</Typography>
          {offer.fallback_major_versions.length > 0 && <><Typography component="dt">Earlier versions</Typography><Typography component="dd">{offer.fallback_major_versions.map(version => `${version}.x`).join(', ')}</Typography></>}
        </Box>
        <StatusLine essential id="activation-offer-feedback" text={!desktop.connected ? 'Activation status could not be received.' : noticeError || (opening ? 'Confirming activation…' : null)} tone={noticeError ? 'error' : 'secondary'} />
        {canSwitch && <Button ref={switchElement} onClick={() => setChoosingAccount(true)} disabled={!desktop.connected || opening || codeBusy || codePending}>Sign in to Full analytics with another account</Button>}
        </Stack>}</DialogContent><DialogActions><Button onClick={() => setOfferOpen(false)}>Not now</Button>
        {!choosingAccount && <Button onClick={() => void openSetup('activation.confirm')} disabled={!canConfirm || opening || codeBusy || codePending}
          aria-describedby="activation-offer-feedback" variant="contained" sx={{ alignSelf: 'flex-start' }}>Activate for free</Button>}
        </DialogActions></Dialog></>}
      {!decision && !fullReady && !choosingProvider && ((activationRequired && canOpen) || codeOpen) && !pendingActivation && <Button
        onClick={() => void openSetup()} disabled={!canOpen || !canRedeem || opening || checking || codeBusy || codePending}
        aria-describedby="activation-notice" variant="outlined">Set up Full analytics</Button>}
      {!fullReady && <Stack direction="row" useFlexGap sx={{ flexWrap: 'wrap' }} spacing={1}>
        {!accepted && !choosingProvider && authentication?.actions.open_browser && authentication.browser_operation_id
          && (authentication.state === 'admitted' || entryError !== null || browserLaunchError !== null) && <Button
          onClick={() => void reopenBrowser()} disabled={!desktop.connected || opening}>Try again</Button>}
        {canSwitch && !offer && !choosingAccount && <Button ref={switchElement} onClick={() => setChoosingAccount(true)} disabled={!desktop.connected || opening || codeBusy || codePending}>Sign in to Full analytics with another account</Button>}
        {choosingAccount && !offer && <Button onClick={() => setChoosingAccount(false)} disabled={opening}>Back</Button>}
      </Stack>}
      {!decision && !authentication && !accepted && activationRequired && !canOpen && !pendingActivation && desktop.connected && <Typography variant="body2">The activation request is unavailable.</Typography>}
      {!fullReady && (activationRequired || codeOpen) && <Box sx={{ mt: 2 }}>
        <Button type="button" aria-expanded={codeOpen} aria-controls={codeFormId}
          onClick={() => setCodeOpen(value => !value)}>Use an activation code</Button>
        {codeOpen && <Stack id={codeFormId} component="form" spacing={2} sx={{ mt: 2, maxWidth: 480 }}
          onSubmit={event => { event.preventDefault(); void redeemCode(); }}>
          <TextField inputRef={codeInput} label="Activation code" autoComplete="off" fullWidth size="small" value={code}
            disabled={!canRedeem || codeBusy || codePending || opening}
            slotProps={{ htmlInput: { spellCheck: false, maxLength: 256, 'aria-describedby': `${codeFormId}-status` } }}
            onChange={event => { setCode(event.target.value); setCodeError(null); }} />
          <Box id={`${codeFormId}-status`}><StatusLine essential id="activation-code-feedback"
            text={codeBusy || codePending ? 'Checking activation…' : codeError} tone={codeError ? 'error' : 'secondary'} /></Box>
          <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
            <Button type="submit" variant="outlined" disabled={!canRedeem || codeBusy || codePending || opening || !code.trim()}>Activate</Button>
          </Stack>
        </Stack>}
      </Box>}
    </Box>
  </Panel>;
}
