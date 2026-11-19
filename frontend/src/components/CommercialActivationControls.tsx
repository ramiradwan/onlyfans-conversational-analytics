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
import type { FullPresentationIntent } from '../protocol';
import { StatusLine } from './ui/ReservedRegion';
import {
  capabilityLicenseApi,
  CAPABILITY_LICENSE_CONTINUATION_PATTERN,
  CapabilityLicenseApiError,
  type CapabilityLicenseApi,
} from '../services/capabilityLicenseApi';
import { desktopOnboardingApi, selectAnalysisReadiness, type DesktopOnboardingApi } from '../services/desktopOnboarding';
import { componentTokens } from '../theme';

const dismissedOfferScopes = new Set<string>();
const DISMISSED_OFFER_KEY = 'full-analytics-review-dismissals-v1';
function dismissedIntent(scope: string) {
  if (dismissedOfferScopes.has(scope)) return true;
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(DISMISSED_OFFER_KEY) ?? '[]');
    return Array.isArray(saved) && saved.includes(scope);
  } catch { return false; }
}
function dismissIntent(scope: string) {
  dismissedOfferScopes.add(scope);
  if (dismissedOfferScopes.size > 64) dismissedOfferScopes.delete(dismissedOfferScopes.values().next().value!);
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(DISMISSED_OFFER_KEY) ?? '[]');
    const scopes = Array.isArray(saved) ? saved.filter((value): value is string => typeof value === 'string' && value !== scope) : [];
    localStorage.setItem(DISMISSED_OFFER_KEY, JSON.stringify([...scopes, scope].slice(-64)));
  } catch { /* The current presentation still remembers dismissal. */ }
}

function safeMessage(error: unknown): string {
  return error instanceof CapabilityLicenseApiError ? error.message : "Activation couldn't be checked.";
}

/** Native offer review and explicit consent; readiness remains Brain-owned. */
export function CommercialActivationControls({
  api = capabilityLicenseApi,
  onboarding = desktopOnboardingApi,
  embedded = false,
  creatorAccountId,
  presentationIntent,
}: {
  api?: CapabilityLicenseApi;
  onboarding?: DesktopOnboardingApi;
  embedded?: boolean;
  creatorAccountId?: string | null;
  presentationIntent?: FullPresentationIntent;
}) {
  const [entryError, setEntryError] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);
  const [openingAction, setOpeningAction] = useState<string | null>(null);
  const [codeOpen, setCodeOpen] = useState(false);
  const [code, setCode] = useState('');
  const [codeError, setCodeError] = useState<string | null>(null);
  const [codeBusy, setCodeBusy] = useState(false);
  const [codePending, setCodePending] = useState(false);
  const [codeUnconfirmed, setCodeUnconfirmed] = useState(false);
  const [refusedCode, setRefusedCode] = useState<string | null>(null);
  const attemptedCode = useRef<string | null>(null);
  const codePresentation = useRef<string | null>(null);
  const codeOperation = useRef<AbortController | null>(null);
  const codeScope = useRef<string | null>(null);
  const codeCreator = useRef<string | null>(null);
  const codeInput = useRef<HTMLInputElement | null>(null);
  const codeFormId = useId();
  // Only an action in this presentation opens a review. A pending offer can
  // be reviewed again explicitly after navigation or dismissal.
  const [openReviewScope, setOpenReviewScope] = useState<string | null>(null);
  const [unconfirmedOffer, setUnconfirmedOffer] = useState<string | null>(null);
  const [choosingAccount, setChoosingAccount] = useState(false);
  const choicesElement = useRef<HTMLDivElement | null>(null);
  const switchElement = useRef<HTMLButtonElement | null>(null);
  const previousChoosing = useRef(false);
  const entryOperation = useRef<AbortController | null>(null);
  const entryScope = useRef<string | null>(null);
  const previousReviewScope = useRef<string | null>(null);
  const desktop = useSyncExternalStore(onboarding.subscribe, onboarding.getState, onboarding.getState);
  const { value: readiness, checking, availability } = selectAnalysisReadiness(desktop, creatorAccountId);
  const error = !checking && desktop.connected && availability !== 'current' ? "Activation couldn't be checked." : null;
  const snapshot = desktop.snapshot;
  const ownerCreator = snapshot?.analysis_readiness.creator_account_id;
  const sameCreator = Boolean(ownerCreator && (creatorAccountId === undefined || ownerCreator === creatorAccountId));
  const currentAuthority = desktop.connected && sameCreator && availability === 'current';
  const decision = sameCreator && readiness?.commercial_authority !== 'active' && snapshot?.decision?.action === 'activate-complimentary'
    && snapshot.decision_status?.state !== 'accepted' ? snapshot.decision : null;
  const decisionStatus = sameCreator ? snapshot?.decision_status?.state : undefined;
  const authentication = sameCreator ? snapshot?.authentication : null;
  const allowed = currentAuthority ? snapshot?.allowed_actions || [] : [];
  const reviewScope = sameCreator && snapshot ? JSON.stringify([snapshot.generation, snapshot.journey_id, ownerCreator]) : null;
  const offerOpen = reviewScope !== null && openReviewScope === reviewScope;
  const setOfferOpen = (open: boolean) => setOpenReviewScope(open ? reviewScope : null);
  const ownerScope = sameCreator && snapshot ? JSON.stringify([snapshot.journey_id, ownerCreator]) : null;
  // Renewal changes the presentation, not the original commercial decision.
  const offerIdentity = decision && ownerScope ? JSON.stringify([ownerScope, decision.operation_id, decision.offer.offer_version_id]) : null;
  // A rendered offer must not confirm a newer decision that arrived before React
  // could paint it. The command owner independently rechecks admission.
  const currentPresentation = () => {
    const current = onboarding.getState();
    return current.connected && current.snapshot === snapshot && current.pending === desktop.pending
      && current.snapshot?.analysis_readiness.creator_account_id === ownerCreator;
  };
  const accepted = decisionStatus === 'accepted';
  const unprojectedOperation = Boolean(desktop.pending && !snapshot?.pending_operations.some(item => item.operation_id === desktop.pending));
  const pendingConfirmation = Boolean(snapshot?.pending_operations.some(item => item.action === 'activation.confirm'));
  const unresolvedCommand = unprojectedOperation || pendingConfirmation;
  const codeInProgress = codeBusy || codePending || codeUnconfirmed;
  const canSelect = !accepted && !unresolvedCommand && !codeInProgress && authentication?.actions.select && allowed.includes('auth.select');
  const accountLabel = sameCreator ? snapshot?.facts.authenticated_account_label : null;
  const canSwitch = !accepted && !unresolvedCommand && !codeInProgress && authentication?.state === 'admitted'
    && authentication.actions.switch_account && allowed.includes('auth.switch');
  const choosingProvider = Boolean(canSelect || (choosingAccount && canSwitch));
  const providers = (['google', 'microsoft-personal', 'telegram', 'tiktok', 'default'] as const).filter(provider => authentication?.choices[provider]);
  const canConfirm = currentAuthority && decision !== null && allowed.includes('activation.confirm')
    && unconfirmedOffer !== offerIdentity && !unresolvedCommand && !codeInProgress;
  const activationOutcome = sameCreator ? snapshot?.pending_operations.find(value => value.action === 'activation.review') : undefined;
  const pendingActivation = Boolean(activationOutcome);
  const canOpen = currentAuthority && !pendingActivation && !unresolvedCommand && !codeInProgress && allowed.includes('activation.review');
  // Code recovery may continue an accepted activation, not compete with an
  // outstanding confirmation or an unacknowledged desktop command.
  // Intent can display an already-admitted review. It cannot prepare a
  // ceremony, open Chrome or execute activation in an effect.
  const intentScope = currentAuthority && snapshot?.local_ready && presentationIntent?.journey_id === snapshot.journey_id
    ? JSON.stringify([ownerCreator, snapshot.journey_id, presentationIntent.disclosure_bundle_id]) : null;
  const promotedIntent = useRef<string | null>(null);
  useEffect(() => {
    if (promotedIntent.current && promotedIntent.current !== intentScope) {
      promotedIntent.current = null;
      setOpenReviewScope(null);
      return;
    }
    if (intentScope && reviewScope && decision && canConfirm && !codeOpen && !offerOpen
      && !opening && !dismissedIntent(intentScope) && currentPresentation()) {
      promotedIntent.current = intentScope;
      setOpenReviewScope(reviewScope);
    }
  }, [intentScope, reviewScope, decision, canConfirm, codeOpen, offerOpen, opening]);
  const dismissOffer = () => {
    if (intentScope) dismissIntent(intentScope);
    promotedIntent.current = null;
    setOfferOpen(false);
  };
  const canRedeem = currentAuthority && !unresolvedCommand && (!pendingActivation || accepted || decision !== null)
    && (!unconfirmedOffer || unconfirmedOffer !== offerIdentity);
  useRevealHold(checking);

  useEffect(() => () => { entryOperation.current?.abort(); codeOperation.current?.abort(); }, []);
  useEffect(() => { if (codeOpen) codeInput.current?.focus(); }, [codeOpen]);
  useEffect(() => { setChoosingAccount(false); }, [authentication?.authentication_attempt?.authentication_attempt_id]);
  useEffect(() => {
    const previous = previousReviewScope.current;
    previousReviewScope.current = reviewScope;
    if (previous === null || previous === reviewScope) return;
    entryOperation.current?.abort();
    entryOperation.current = null;
    entryScope.current = null;
    setOpening(false); setOpeningAction(null); setEntryError(null);
    setOpenReviewScope(null); setChoosingAccount(false);
  }, [reviewScope]);
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
    if (codeOperation.current || codeInProgress || unresolvedCommand || (entryOperation.current && !entryOperation.current.signal.aborted)) return;
    const permitted = action === 'activation.confirm' ? canConfirm : action === 'auth.select' ? canSelect
      : action === 'auth.switch' ? canSwitch : canOpen;
    if (!currentPresentation()) return;
    if (!currentAuthority || !permitted || (provider && !providers.includes(provider))) {
      entryScope.current = reviewScope;
      setEntryError('Full analytics setup is unavailable on this page.');
      return;
    }
    const controller = new AbortController();
    entryOperation.current = controller;
    entryScope.current = reviewScope;
    setOpening(true);
    setOfferOpen(true);
    setOpeningAction(action);
    if (action === 'activation.confirm') {
      if (intentScope) dismissIntent(intentScope);
      setUnconfirmedOffer(offerIdentity);
    }
    setEntryError(null);
    try {
      if (provider) await onboarding.begin(action, { provider });
      else await onboarding.begin(action);
      if (!controller.signal.aborted) setChoosingAccount(false);
    } catch (cause) {
      if (!controller.signal.aborted) {
        setEntryError(cause instanceof Error && cause.message === 'browser_unavailable'
          ? 'The browser action could not be confirmed.' : action === 'activation.confirm' ? 'Activation could not be confirmed.'
            : 'Sign-in could not be opened.');
      }
    } finally {
      if (entryOperation.current === controller) {
        entryOperation.current = null;
        if (!controller.signal.aborted) { setOpening(false); setOpeningAction(null); }
      }
    }
  };

  const reopenBrowser = async () => {
    if (!currentAuthority || !currentPresentation() || codeInProgress || unresolvedCommand || entryOperation.current || !authentication?.actions.open_browser || !authentication.browser_operation_id) return;
    const controller = new AbortController(); entryOperation.current = controller; entryScope.current = reviewScope;
    setOpening(true); setOpeningAction('browser.open'); setEntryError(null);
    try { await onboarding.openBrowser(authentication.browser_operation_id); }
    catch { if (!controller.signal.aborted) setEntryError('Chrome could not be opened.'); }
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
    codeScope.current = null; codeCreator.current = null; codePresentation.current = null; attemptedCode.current = null;
    setCode(''); setCodeOpen(false); setCodeBusy(false); setCodePending(false); setCodeUnconfirmed(false); setRefusedCode(null); setCodeError(null);
  }, [readiness?.commercial_authority]);

  useEffect(() => {
    // Losing the presentation is not evidence of a different owner.
    if (codeScope.current === null) return;
    const changedOwner = creatorAccountId !== undefined && creatorAccountId !== codeCreator.current
      || ownerCreator != null && ownerCreator !== codeCreator.current
      || ownerScope !== null && codeScope.current !== ownerScope;
    if (changedOwner) {
      codeOperation.current?.abort(); codeOperation.current = null;
      codeScope.current = null; codeCreator.current = null; codePresentation.current = null; attemptedCode.current = null;
      setCode(''); setCodeOpen(false); setCodeBusy(false); setCodePending(false);
      setCodeUnconfirmed(false); setRefusedCode(null); setCodeError(null);
      return;
    }
    if (codeOperation.current && (!desktop.connected || codePresentation.current !== reviewScope)) {
      codeOperation.current.abort(); codeOperation.current = null;
      setCodeBusy(false); setCodeUnconfirmed(true);
      setCodeError("Activation couldn't be confirmed.");
    }
  }, [ownerScope, reviewScope, desktop.connected, ownerCreator, creatorAccountId]);

  const redeemCode = async () => {
    if (!canRedeem || !currentPresentation() || codeOperation.current || entryOperation.current || opening || codePending || ownerScope === null) return;
    const value = code.trim();
    if (refusedCode === value || codeUnconfirmed && (attemptedCode.current !== value || codeScope.current !== ownerScope)) return;
    if (!CAPABILITY_LICENSE_CONTINUATION_PATTERN.test(value)) {
      setCodeError('Enter the full activation code.'); return;
    }
    const controller = new AbortController(); codeOperation.current = controller;
    codeScope.current = ownerScope;
    codeCreator.current = ownerCreator ?? null;
    codePresentation.current = reviewScope;
    attemptedCode.current = value;
    setCodeBusy(true); setCodeError(null);
    try {
      await api.redeem(value, controller.signal);
      if (!controller.signal.aborted) { setCodePending(true); setCodeUnconfirmed(false); setRefusedCode(null); }
    } catch (cause) {
      if (!controller.signal.aborted) {
        const outcome = cause instanceof CapabilityLicenseApiError ? cause.outcome : 'unknown';
        setCodeUnconfirmed(outcome === 'unknown' || codeUnconfirmed && outcome === 'not_sent');
        setRefusedCode(outcome === 'refused' ? value : null);
        setCodeError(safeMessage(cause));
      }
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
    : desktop.error === 'chrome_unavailable' ? 'Chrome could not be opened.' : null;
  const noticeError = (entryScope.current === reviewScope ? entryError : null) ?? error ?? (!codeOpen ? codeError : null) ?? browserLaunchError;
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
          : unresolvedCommand ? 'Activation has not been confirmed.'
          : choosingProvider ? choosingAccount ? 'Choose how to sign in with another account.' : 'Choose how to sign in.'
            : activationOutcome?.state === 'waiting' && activationOutcome.reason === 'activation_unavailable' ? 'Checking the activation offer.'
              : activationOutcome?.state === 'outcome_unknown' ? 'Activation has not been confirmed.'
            : authentication?.state === 'admitted' && authentication.actions.open_browser ? 'The activation offer is unavailable.'
            : authentication?.state === 'expired' ? 'This sign-in request expired.'
              : authentication?.state === 'retired' ? 'Sign-in was cancelled.'
                : authentication?.state === 'unavailable' ? 'The sign-in request could not be confirmed.'
                  : authentication?.state === 'selected' || decisionStatus === 'awaiting-authentication' ? 'Finish signing in in Chrome.'
                    : decisionStatus === 'unavailable' || activationOutcome?.reason === 'activation_unavailable' || desktop.error === 'activation_unavailable' ? 'Full analytics activation could not be prepared.'
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
        <Button onClick={() => { if (currentPresentation()) setOfferOpen(true); }} disabled={!currentAuthority} aria-haspopup="dialog" variant="outlined">Review activation offer</Button>
        <Dialog open={offerOpen && sameCreator} onClose={dismissOffer} aria-labelledby="activation-offer-title" fullWidth maxWidth="sm"
          slotProps={{ backdrop: { sx: theme => ({ ...theme.effects.glassmorphism(theme) }) } }}>
        <DialogTitle id="activation-offer-title">{choosingAccount ? 'Sign in to Full analytics with another account' : offer.description}</DialogTitle>
        <DialogContent>{choosingAccount ? <Stack spacing={2}>
          <Typography id="activation-switch-note" variant="body2">Sign in again to review activation with a different account.</Typography>
          <Box ref={choicesElement}><ProviderSignInButtons providers={providers}
            disabled={!desktop.connected || opening || codeBusy || codePending} describedBy="activation-switch-note"
            onSelect={provider => void openSetup('auth.switch', provider)} /></Box>
          <Button onClick={() => setChoosingAccount(false)} disabled={opening}>Back</Button>
        </Stack> : <Stack spacing={2} useFlexGap>
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
        <Box id="activation-offer-feedback"><StatusLine essential id="activation-offer-feedback" text={!desktop.connected ? 'Activation status could not be received.' : noticeError || (opening ? 'Confirming activation…' : null)} tone={noticeError ? 'error' : 'secondary'} /></Box>
        {canSwitch && <Button ref={switchElement} onClick={() => setChoosingAccount(true)} disabled={!desktop.connected || opening || codeBusy || codePending}
          sx={{ justifyContent: 'flex-start', minHeight: componentTokens.staticUi.controlHeight, mx: -1, textAlign: 'start', '&, &:hover': { textDecoration: 'underline' } }}>Sign in to Full analytics with another account</Button>}
        <Button onClick={() => {
          if (currentPresentation() && canRedeem && !codeInProgress) { dismissOffer(); setCodeOpen(true); }
        }} disabled={opening || !canRedeem || codeInProgress} sx={{ justifyContent: 'flex-start', alignSelf: 'flex-start', visibility: canRedeem && !codeInProgress ? 'visible' : 'hidden' }}>Other activation options</Button>
        </Stack>}</DialogContent><DialogActions><Button onClick={dismissOffer}>Not now</Button>
        {!choosingAccount && <Button onClick={() => void openSetup('activation.confirm')} disabled={!canConfirm || opening || codeBusy || codePending}
          aria-describedby="activation-offer-feedback" variant="contained" sx={{ alignSelf: 'flex-start' }}>Activate for free</Button>}
        </DialogActions></Dialog></>}
      {!decision && !fullReady && !choosingProvider && ((activationRequired && canOpen) || codeOpen) && !pendingActivation && <Button
        onClick={() => void openSetup()} disabled={!canOpen || !canRedeem || opening || checking || codeBusy || codePending}
        aria-describedby="activation-notice" variant="outlined">Set up Full analytics</Button>}
      {!fullReady && <Stack direction="row" useFlexGap sx={{ flexWrap: 'wrap', ml: -1 }} spacing={1}>
        {!accepted && !unresolvedCommand && !codeInProgress && !choosingProvider && authentication?.actions.open_browser && authentication.browser_operation_id
          && currentAuthority && !desktop.browserLaunchPending
          && (authentication.state === 'admitted' || browserLaunchError !== null) && <Button
          onClick={() => void reopenBrowser()} disabled={!currentAuthority || opening}>Open Chrome</Button>}
        {canSwitch && !offer && !choosingAccount && <Button ref={switchElement} onClick={() => setChoosingAccount(true)} disabled={!desktop.connected || opening || codeBusy || codePending}>Sign in to Full analytics with another account</Button>}
        {choosingAccount && !offer && <Button onClick={() => setChoosingAccount(false)} disabled={opening}>Back</Button>}
      </Stack>}
      {!decision && !authentication && !accepted && activationRequired && !allowed.includes('activation.review') && !pendingActivation && desktop.connected && <Typography variant="body2">The activation request is unavailable.</Typography>}
      {!fullReady && ((!offer && activationRequired) || codeOpen || codeInProgress) && <Box sx={{ mt: 2 }}>
        <Button type="button" aria-expanded={codeOpen} aria-controls={codeFormId}
          onClick={() => setCodeOpen(value => !value)} sx={{ ml: -1 }}>Use an activation code</Button>
        {codeOpen && <Stack id={codeFormId} component="form" spacing={2} sx={{ mt: 2, maxWidth: 480 }}
          onSubmit={event => { event.preventDefault(); void redeemCode(); }}>
          <TextField inputRef={codeInput} label="Activation code" autoComplete="off" fullWidth size="small" value={code}
            disabled={!canRedeem || codeBusy || codePending || codeUnconfirmed || opening}
            slotProps={{ htmlInput: { spellCheck: false, maxLength: 256, 'aria-describedby': `${codeFormId}-status` } }}
            onChange={event => {
              if (!codeUnconfirmed && ownerScope !== null) {
                codeScope.current = ownerScope;
                codeCreator.current = ownerCreator ?? null;
                setCode(event.target.value); setCodeError(null);
              }
            }} />
          <Box id={`${codeFormId}-status`}><StatusLine essential id="activation-code-feedback"
            text={codeBusy || codePending ? 'Checking activation…' : codeError} tone={codeError ? 'error' : 'secondary'} /></Box>
          <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
            <Button type="submit" variant="outlined" disabled={!canRedeem || codeBusy || codePending || opening || !code.trim() || refusedCode === code.trim()
              || codeUnconfirmed && (codeScope.current !== ownerScope || attemptedCode.current !== code.trim())}>
              {codeUnconfirmed ? 'Resume activation' : 'Activate'}
            </Button>
          </Stack>
        </Stack>}
      </Box>}
    </Box>
  </Panel>;
}
